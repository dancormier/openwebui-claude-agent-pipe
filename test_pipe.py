#!/usr/bin/env python3
"""Tests for the turn loop in `class Pipe` (src/80_pipe.py).

Run: python3 test_pipe.py [<path-to-pipe.py>]

The whole module is exec'd against a stub SDK and `ClaudeSDKClient` is
replaced with a scripted fake, so the loop runs its real dispatch, status,
session bookkeeping and interrupt handling with no CLI behind it.
"""

import asyncio
import atexit
import pathlib
import shutil
import sys
import tempfile
import types

from _loader import check, load_full, report

mod = load_full()
mod.log.disabled = True

# The artifact inliner runs on every result; with a user and a file store it
# yields nothing when the workdir has no new files, which is this suite's case.
for name in ("open_webui", "open_webui.models", "open_webui.models.files", "open_webui.storage", "open_webui.storage.provider"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["open_webui.models.files"].FileForm = sys.modules["open_webui.models.files"].Files = object()
sys.modules["open_webui.storage.provider"].Storage = object()
USER = {"id": "u1"}

WAIT_FOR_INTERRUPT = object()


class FakeClient:
    """Each instance plays the next script in `scripts`: messages to yield,
    an Exception to raise in place, or WAIT_FOR_INTERRUPT to park until the
    pipe calls `interrupt()`, as a new message in the same chat does."""

    scripts = []
    instances = []

    def __init__(self, options=None):
        self.options = options.__dict__
        self.script = FakeClient.scripts.pop(0)
        self.queries = []
        self.interrupts = 0
        self.interrupted = asyncio.Event()
        self.parked = asyncio.Event()
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def interrupt(self):
        self.interrupts += 1
        self.interrupted.set()

    async def get_context_usage(self):
        return {"totalTokens": 50_000, "rawMaxTokens": 200_000}

    async def receive_response(self):
        for item in self.script:
            if item is WAIT_FOR_INTERRUPT:
                self.parked.set()
                await self.interrupted.wait()
            elif isinstance(item, Exception):
                raise item
            else:
                yield item


mod.ClaudeSDKClient = FakeClient


def init(session_id):
    return mod.SystemMessage(subtype="init", data={"session_id": session_id})


def text(*deltas):
    events = [{"type": "content_block_start", "content_block": {"type": "text"}, "index": 0}]
    events += [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": d}, "index": 0} for d in deltas]
    return [mod.StreamEvent(event=e, parent_tool_use_id=None) for e in events]


def assistant(*blocks):
    return mod.AssistantMessage(content=list(blocks), usage={"input_tokens": 10, "output_tokens": 5}, parent_tool_use_id=None)


def tool_use(name, tool_input):
    return assistant(mod.ToolUseBlock(id="t1", name=name, input=tool_input))


def result(subtype="success", duration_ms=1200):
    return mod.ResultMessage(subtype=subtype, duration_ms=duration_ms, is_error=False, result="", num_turns=1)


def body(*texts):
    roles = ["user", "assistant"]
    msgs = [{"role": roles[(len(texts) - 1 - i) % 2], "content": t} for i, t in enumerate(texts)]
    return {"messages": msgs}


def recorder():
    events = []

    async def emit(event):
        events.append(event)

    return events, emit


def statuses(events):
    return [e["data"]["description"] for e in events if e["type"] == "status"]


def last_status(events):
    return [e["data"] for e in events if e["type"] == "status"][-1]


async def collect(agen):
    return "".join([chunk async for chunk in agen])


root = tempfile.mkdtemp()
atexit.register(shutil.rmtree, root, True)
pipe = mod.Pipe()
pipe.valves.WORKDIR_ROOT = root
pipe.valves.INLINE_TOOL_DETAILS = False


def stream(chat_id, body, emit):
    turn_info = {}
    out = asyncio.run(collect(pipe._pipe_stream(body, chat_id, emit, None, USER, turn_info=turn_info)))
    return out, turn_info


# ---- a turn that streams text and ends ----
FakeClient.scripts = [[init("sess-1"), *text("Hello", " world"), assistant(), result()]]
FakeClient.instances = []
events, emit = recorder()
out, turn_info = stream("chat-1", body("hello"), emit)
client = FakeClient.instances[0]
check("text deltas reach the reply in order and nothing else does", out == "Hello world", out)
check("first status announces the start", statuses(events)[0] == "Starting Claude Code…", statuses(events))
check("init with no history reports a new chat", "Session: new chat" in statuses(events), statuses(events))
check("final status is the done line with the live context, done=True",
      last_status(events) == {"description": "Done · 1s · 50k/200k (25%)", "done": True}, last_status(events))
completion = [e["data"]["usage"] for e in events if e["type"] == "chat:completion"]
check("usage from the last assistant message is published as a completion event",
      len(completion) == 1 and completion[0]["completion_tokens"] == 5 and completion[0]["duration_ms"] == 1200, completion)
check("session id is cached, persisted and handed back", mod._chat_sessions["chat-1"] == "sess-1"
      and mod._load_session_meta(root, "chat-1")["session_id"] == "sess-1" and turn_info["session_id"] == "sess-1", turn_info)
check("first turn sends the bare prompt with no resume", client.queries == ["hello"] and "resume" not in client.options, client.queries)
check("agent runs in the chat's own workdir", client.options["cwd"] == str(pathlib.Path(root) / "chat-1"), client.options["cwd"])

# ---- a turn interrupted mid-tool by a newer message in the same chat ----
FakeClient.scripts = [
    [init("sess-2"), *text("Checking…"), tool_use("Bash", {"command": "sleep 60"}), WAIT_FOR_INTERRUPT, result(duration_ms=500)],
    [init("sess-2"), *text("Second"), result()],
]
FakeClient.instances = []
events1, emit1 = recorder()
events2, emit2 = recorder()


async def overlap():
    first = asyncio.ensure_future(collect(pipe.pipe(body("first"), "chat-2", emit1, __user__=USER)))
    # Bounded: a first turn that dies before it parks must surface as a FAIL,
    # not as CI's job timeout with no line naming the test.
    for _ in range(1000):
        if FakeClient.instances and FakeClient.instances[0].parked.is_set():
            break
        if first.done():
            break
        await asyncio.sleep(0.005)
    else:
        first.cancel()
        raise AssertionError("first turn never parked at the interrupt point")
    second = asyncio.ensure_future(collect(pipe.pipe(body("first", "…", "second"), "chat-2", emit2, __user__=USER)))
    return await asyncio.wait_for(first, 5), await asyncio.wait_for(second, 5)


out1, out2 = asyncio.run(overlap())
c1, c2 = FakeClient.instances
check("the running turn's client is interrupted exactly once", c1.interrupts == 1, c1.interrupts)
check("interrupted turn keeps its partial text and ends with the stopped line",
      out1 == "Checking…\n\n_Stopped: a newer message arrived in this chat._\n", out1)
check("tool status was shown before the interrupt", "🔧 Bash: sleep 60" in statuses(events1), statuses(events1))
check("interrupted turn still closes its status with a done line",
      last_status(events1)["done"] is True and last_status(events1)["description"].endswith("· 1 tool"), last_status(events1))
check("new turn's first status says what it stopped", statuses(events2)[0] == "Stopped the turn still running in this chat", statuses(events2))
check("new turn resumes the chat's session and reports it", c2.options.get("resume") == "sess-2" and "Session: resumed" in statuses(events2), (c2.options.get("resume"), statuses(events2)))
check("new turn's prompt carries the overlap note", c2.queries == [mod._with_overlap_note("second")], c2.queries)
check("new turn streams normally", out2 == "Second", out2)
check("both turns released the in-flight registry", mod._inflight == {}, mod._inflight)

# ---- the client fails with no session to fall back to ----
FakeClient.scripts = [[init("sess-3"), *text("partial"), RuntimeError("boom")]]
FakeClient.instances = []
events, emit = recorder()
out, _ = stream("chat-3", body("hello"), emit)
check("error after init: partial text kept, error appended, no raise",
      out == "partial\n\n**Claude Code error:** `RuntimeError: boom`\n", out)
check("error closes the status line", last_status(events) == {"description": "Error: boom", "done": True}, last_status(events))

# ---- a stale session id: the resume fails before init and the turn reruns cold ----
mod._chat_sessions["chat-4"] = "stale-sess"
FakeClient.scripts = [[RuntimeError("No conversation found")], [init("sess-4"), *text("Fresh"), result()]]
FakeClient.instances = []
events, emit = recorder()
out, turn_info = stream("chat-4", body("earlier", "reply", "hello"), emit)
c1, c2 = FakeClient.instances
check("first attempt tried the stale resume", c1.options.get("resume") == "stale-sess", c1.options)
check("retry runs cold and replays the history", "resume" not in c2.options and c2.queries[0].startswith("<conversation_history>") and c2.queries[0].endswith("hello"), c2.queries)
check("user is told why, then the reply streams", "Session expired — replaying history…" in statuses(events) and out == "Fresh", (statuses(events), out))
check("new session id replaces the stale one", mod._chat_sessions["chat-4"] == "sess-4" and turn_info["session_id"] == "sess-4", turn_info)

report("pipe turn-loop tests passed")
