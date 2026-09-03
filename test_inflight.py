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

if failures:
    sys.exit(f"{failures} failure(s)")
print("ok — in-flight guard tests passed")
