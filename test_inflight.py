#!/usr/bin/env python3
"""Tests for the per-chat in-flight turn guard (src/36_inflight.py).

Run: python3 test_inflight.py [<path-to-pipe.py>]

Same standalone pattern as test_turn.py: slice the module at the SDK import,
stub pydantic, exec the head.
"""

import asyncio
import pathlib
import sys
import types

PIPE = pathlib.Path(
    sys.argv[1] if len(sys.argv) > 1
    else pathlib.Path(__file__).with_name("claude_agent_pipe.py")
)
SPLIT = "from claude_agent_sdk import ("

if "pydantic" not in sys.modules:
    _stub = types.ModuleType("pydantic")
    _stub.BaseModel = type("BaseModel", (), {})
    _stub.Field = lambda *a, **k: None
    sys.modules["pydantic"] = _stub

src = PIPE.read_text(encoding="utf-8")
head = src.split(SPLIT, 1)[0]
mod = types.ModuleType("pipe_head")
mod.__dict__["__name__"] = "pipe_head"
exec(compile(head, str(PIPE), "exec"), mod.__dict__)

claim, release, registry = mod._claim_chat, mod._release_chat, mod._inflight

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        got:  {got!r}\n        want: {want!r}"))


# ---- first turn in a chat claims without stopping anything ----
async def first_turn():
    entry, superseded = await claim("chat-a")
    result = (superseded, registry.get("chat-a") is entry, entry.done.is_set())
    release("chat-a", entry)
    return result + (registry.get("chat-a"), entry.done.is_set())

check("first claim is not superseded, registered, not done",
      asyncio.run(first_turn())[:3], (False, True, False))
check("release clears the registry and marks done",
      asyncio.run(first_turn())[3:], (None, True))


# ---- second turn interrupts the first and waits for it to exit ----
async def overlap():
    calls = []
    first, _ = await claim("chat-b")

    async def interrupt():
        calls.append("interrupt")
        asyncio.get_running_loop().call_later(0.05, release, "chat-b", first)

    first.interrupt = interrupt
    second, superseded = await claim("chat-b", wait_s=2.0)
    out = (calls, superseded, first.superseded, first.done.is_set(),
           registry.get("chat-b") is second)
    release("chat-b", second)
    return out

check("second claim interrupts the first, waits for it, then owns the chat",
      asyncio.run(overlap()), (["interrupt"], True, True, True, True))


# ---- a first turn that never exits does not block the second forever ----
async def stuck():
    first, _ = await claim("chat-c")
    first.interrupt = None
    second, superseded = await claim("chat-c", wait_s=0.05)
    out = (superseded, registry.get("chat-c") is second, first.done.is_set())
    release("chat-c", second)
    release("chat-c", first)
    return out + (registry.get("chat-c"),)

check("stuck first turn: second proceeds after the grace, late release is harmless",
      asyncio.run(stuck()), (True, True, False, None))


# ---- a finished turn left in the registry is not treated as live ----
async def finished():
    first, _ = await claim("chat-d")
    first.done.set()
    second, superseded = await claim("chat-d")
    release("chat-d", second)
    return superseded

check("done entry is not superseded", asyncio.run(finished()), False)


# ---- the winning turn knows it stopped something ----
async def note_flag():
    first, _ = await claim("chat-e")
    first.interrupt = None
    second, _ = await claim("chat-e", wait_s=0.01)
    out = (first.stopped_previous, second.stopped_previous)
    release("chat-e", second)
    release("chat-e", first)
    return out

check("stopped_previous set only on the turn that stopped one",
      asyncio.run(note_flag()), (False, True))
check("overlap note is appended after the prompt",
      mod._with_overlap_note("hi").startswith("hi\n\n[Gateway note:"), True)


# ---- a typed message answers a pending form instead of interrupting ----
deliver = mod._deliver_typed_answer


async def typed_answer():
    no_turn = deliver("chat-f", "1b")
    entry, _ = await claim("chat-f")
    no_form = deliver("chat-f", "1b")
    fut = asyncio.get_running_loop().create_future()
    entry.pending_ask = fut
    blank = deliver("chat-f", "   ")
    delivered = deliver("chat-f", "  1b, 2a ")
    again = deliver("chat-f", "more")
    out = (no_turn, no_form, blank, delivered, fut.result(), again,
           entry.superseded, entry.interrupt)
    release("chat-f", entry)
    return out

check("typed answer: no turn / no form / blank → False; set → True with stripped text; second → False; guard untouched",
      asyncio.run(typed_answer()), (False, False, False, True, "1b, 2a", False, False, None))


async def typed_after_done():
    entry, _ = await claim("chat-g")
    entry.pending_ask = asyncio.get_running_loop().create_future()
    release("chat-g", entry)
    return deliver("chat-g", "1b")

check("typed answer: a finished turn takes nothing", asyncio.run(typed_after_done()), False)


async def regenerate_is_not_an_answer():
    entry, _ = await claim("chat-h", prompt="  plan the trip ")
    fut = asyncio.get_running_loop().create_future()
    entry.pending_ask = fut
    same = deliver("chat-h", "plan the trip")
    other = deliver("chat-h", "1b")
    out = (entry.prompt, same, fut.done() and fut.result() == "1b", other)
    release("chat-h", entry)
    return out

check("typed answer: the turn's own prompt re-sent (regenerate) is refused, a different text is delivered",
      asyncio.run(regenerate_is_not_an_answer()), ("plan the trip", False, True, True))

if failures:
    sys.exit(f"{failures} failure(s)")
print("ok — in-flight guard tests passed")
