#!/usr/bin/env python3
"""Tests for the per-message turn handlers (src/35_turn.py).

Run: python3 test_turn.py [<path-to-pipe.py>]

Same standalone pattern as test_sessions.py: slice the module at the SDK
import, stub pydantic, exec the head. The handlers take plain values and
return (chunks, status), so no SDK types are needed.
"""

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

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


def ev(etype, **kw):
    return {"type": etype, **kw}


# ---- stream events: text ----
st = mod._TurnState()
chunks, status = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=0), st, True)
check("first text block: no separator", chunks == [] and status is None)
chunks, _ = mod._on_stream_event(ev("content_block_delta", delta={"type": "text_delta", "text": "hi"}, index=0), st, True)
check("text delta streamed", chunks == ["hi"] and st.text_emitted)
chunks, _ = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=1), st, True)
check("second text block: paragraph break", chunks == ["\n\n"])
chunks, _ = mod._on_stream_event(ev("content_block_delta", delta={"type": "text_delta", "text": ""}, index=1), st, True)
check("empty delta yields nothing", chunks == [])

# ---- stream events: text after tool activity gets a rule ----
st = mod._TurnState()
mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=0), st, True)
mod._on_stream_event(ev("content_block_delta", delta={"type": "text_delta", "text": "looking"}, index=0), st, True)
st.note_assistant(True, None, 2)
chunks, _ = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=0), st, True)
check("text after tools: rule separator", chunks == [mod._BEAT_SEPARATOR])
check("rule resets the tool counter", st.tools_since_text == 0)
chunks, _ = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=1), st, True)
check("text after text: plain paragraph break", chunks == ["\n\n"])
st = mod._TurnState()
st.note_assistant(True, None, 1)
chunks, _ = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=0), st, True)
check("tools before any text: no rule", chunks == [])
st.note_assistant(False, None, 3)
mod._on_stream_event(ev("content_block_delta", delta={"type": "text_delta", "text": "x"}, index=0), st, True)
chunks, _ = mod._on_stream_event(ev("content_block_start", content_block={"type": "text"}, index=1), st, True)
check("subagent tools do not count", chunks == ["\n\n"])

# ---- stream events: thinking, inline ----
st = mod._TurnState()
chunks, status = mod._on_stream_event(ev("content_block_start", content_block={"type": "thinking"}, index=0), st, True)
check("thinking start: status only", chunks == [] and status == "💭 Thinking…")
mod._on_stream_event(ev("content_block_delta", delta={"type": "thinking_delta", "thinking": "a\n\nb"}, index=0), st, True)
chunks, status = mod._on_stream_event(ev("content_block_stop", index=0), st, True)
check("thinking stop inline: one details block", len(chunks) == 1 and "<details>" in chunks[0] and "a\n\nb" in chunks[0] and status is None)
check("thinking buffer released", st.thinking_buffers == {})

# ---- stream events: thinking, not inline ----
st = mod._TurnState()
mod._on_stream_event(ev("content_block_start", content_block={"type": "thinking"}, index=2), st, False)
mod._on_stream_event(ev("content_block_delta", delta={"type": "thinking_delta", "thinking": "x"}, index=2), st, False)
chunks, status = mod._on_stream_event(ev("content_block_stop", index=2), st, False)
check("thinking stop not inline: status, no chunk", chunks == [] and status == "💭 thinking")
chunks, status = mod._on_stream_event(ev("content_block_stop", index=9), st, False)
check("stop for unknown index: no-op", chunks == [] and status is None)
st.thinking_buffers[0] = "stale"
mod._on_stream_event(ev("message_start"), st, False)
check("message_start clears thinking buffers", st.thinking_buffers == {})
chunks, status = mod._on_stream_event(ev("content_block_delta", delta={"type": "input_json_delta", "partial_json": "{"}, index=0), st, True)
check("input_json_delta ignored", chunks == [] and status is None)

# ---- tool use ----
st = mod._TurnState()
chunks, status = mod._on_tool_use("Bash", {"command": "ls -la"}, "t1", st, True, 100.0)
check("tool use: status carries preview", status == "🔧 Bash: ls -la", status)
check("tool use: registered for heartbeat", st.active_tools["t1"]["started"] == 100.0 and st.active_tools["t1"]["label"] == "Bash: ls -la")
check("tool use inline: details block with fenced input", len(chunks) == 1 and "<summary>🔧 Bash · ls -la</summary>" in chunks[0] and "ls -la" in chunks[0])
chunks, status = mod._on_tool_use("Bash", {"command": "pwd"}, "t2", st, False, 101.0)
check("tool use not inline: status only", chunks == [] and status == "🔧 Bash: pwd")
chunks, status = mod._on_tool_use("Mystery", {}, "t3", st, True, 102.0)
check("unknown tool, no input: bare name", status == "🔧 Mystery" and "<summary>🔧 Mystery</summary>" in chunks[0], status)

# ---- heartbeat label ----
label, elapsed = mod._heartbeat_label(st.active_tools, 110.0)
check("heartbeat: multiple tools names count and oldest", label == "3 tools · longest Bash: ls -la" and elapsed == 10, label)
one = {"t9": {"label": "Read: a.txt", "started": 5.0}}
check("heartbeat: single tool label", mod._heartbeat_label(one, 8.9) == ("Read: a.txt", 3))

# ---- tool result ----
chunks, status = mod._on_tool_result("t1", False, "ok", st, True)
check("tool result: releases heartbeat entry", "t1" not in st.active_tools and chunks == [] and status is None)
chunks, status = mod._on_tool_result("t2", True, "Traceback: boom", st, True)
check("tool error inline: collapsed hiccup block", len(chunks) == 1 and "tool hiccup" in chunks[0] and "boom" in chunks[0] and status is None)
chunks, status = mod._on_tool_result("t3", True, [{"type": "text", "text": "x" * 2000}], st, False)
check("tool error not inline: status only", chunks == [] and status == "⚙️ tool hiccup (retrying)")
chunks, status = mod._on_tool_result("t3", True, "y" * 2000, st, True)
fenced = chunks[0].split("```")[1]
check("tool error text truncated to 800", len(chunks) == 1 and fenced.count("y") == 800, str(fenced.count("y")))
chunks, status = mod._on_tool_result("never-seen", False, None, st, True)
check("unknown tool_use_id: no-op", chunks == [] and status is None)

# ---- assistant bookkeeping ----
st = mod._TurnState()
st.note_assistant(True, {"input_tokens": 5}, 2)
st.note_assistant(False, {"input_tokens": 999}, 7)
st.note_assistant(True, None, 1)
check("note_assistant: subagent ignored, usage kept from last main call", st.last_usage == {"input_tokens": 5} and st.tool_count == 3)

# ---- session status ----
check("session status: resumed wins", mod._session_status(True, [{"role": "user", "content": "x"}]) == "Session: resumed")
check("session status: history present → cold start", mod._session_status(False, [{"role": "user", "content": "x"}]) == "Session: cold start — history replayed")
check("session status: empty history → new chat", mod._session_status(False, []) == "Session: new chat")

# ---- context + done line ----
check("context from usage", mod._context_from_usage({"totalTokens": 396000, "rawMaxTokens": 1000000}) == "396k/1M (40%)", mod._context_from_usage({"totalTokens": 396000, "rawMaxTokens": 1000000}))
check("context from usage: missing window → empty", mod._context_from_usage({"totalTokens": 5}) == "")
check("done line: context before tools", mod._done_line(102000, 12, "1k/2k (50%)") == "Done · 1m42s · 1k/2k (50%) · 12 tools", mod._done_line(102000, 12, "1k/2k (50%)"))
check("done line: all parts in order", mod._done_line(80000, 3, "74k/200k (37%)", 2) == "Done · 1m20s · 74k/200k (37%) · 3 tools · 2 subagents", mod._done_line(80000, 3, "74k/200k (37%)", 2))
check("done line: no duration keeps context first", mod._done_line(None, 1, "1k/2k (50%)") == "Done · 1k/2k (50%) · 1 tool", mod._done_line(None, 1, "1k/2k (50%)"))
check("done line: singular tool", mod._done_line(0, 1, "") == "Done · 1 tool")
check("done line: nothing", mod._done_line(None, 0, "") == "Done.")

# ---- subagent grouping ----
st = mod._TurnState()
chunks, status = mod._on_tool_use("Task", {"subagent_type": "researcher", "description": "Survey X", "prompt": "..."}, "task1", st, False, 10.0)
check("task start: registers subagent with label", status == "🔧 🤖 researcher: Survey X" and st.agents["task1"]["label"] == "researcher: Survey X" and st.agent_count == 1, status)
chunks, status = mod._on_tool_use("Bash", {"command": "ls"}, "c1", st, True, 11.0, parent_id="task1")
check("subagent tool: labeled under its parent", status == "🔧 ↳ researcher: Survey X · Bash: ls", status)
check("subagent tool: details summary prefixed", "<summary>↳ researcher: Survey X · 🔧 Bash · ls</summary>" in chunks[0], chunks[0][:80])
check("subagent tool counted on the agent", st.agents["task1"]["tools"] == 1)
label, _ = mod._heartbeat_label(st.active_tools, 12.0)
check("heartbeat: the Task itself is the oldest running tool", label == "2 tools · longest 🤖 researcher: Survey X", label)
chunks, status = mod._on_tool_result("c1", False, "ok", st, True, 12.0)
check("subagent tool result: silent release", chunks == [] and status is None and "c1" not in st.active_tools)
chunks, status = mod._on_tool_use("Read", {"file_path": "/a"}, "c2", st, False, 13.0, parent_id="task1")
mod._on_tool_result("c2", False, "x", st, False, 14.0)
chunks, status = mod._on_tool_result("task1", False, "report", st, False, 25.5)
check("task end: summary status with tools and elapsed", status == "✅ researcher: Survey X · 2 tools · 15s" and chunks == [], status)
check("task end: agent released, count kept", st.agents == {} and st.agent_count == 1)
chunks, status = mod._on_tool_use("Bash", {"command": "pwd"}, "c3", st, False, 30.0, parent_id="unknown-parent")
check("tool with unknown parent: plain label", status == "🔧 Bash: pwd")
chunks, status = mod._on_tool_use("Task", {"subagent_type": "coder"}, "task2", st, False, 31.0)
check("task with no description: type only", status == "🔧 🤖 coder", status)
chunks, status = mod._on_tool_result("task2", True, "boom", st, False, 40.0)
check("task error: hiccup status, agent released", status == "⚙️ tool hiccup (retrying)" and "task2" not in st.agents)
st = mod._TurnState()
chunks, status = mod._on_tool_use("Agent", {"subagent_type": "researcher", "description": "Look up X", "prompt": "..."}, "a1", st, False, 0.0)
check("Agent (renamed Task) registers a subagent", status == "🔧 🤖 researcher: Look up X" and "a1" in st.agents, status)
chunks, status = mod._on_tool_use("Read", {"file_path": "/x"}, "r1", st, False, 1.0, parent_id="a1")
check("tool under Agent labeled", status == "🔧 ↳ researcher: Look up X · Read: /x", status)
mod._on_tool_result("r1", False, "", st, False, 2.0)
chunks, status = mod._on_tool_result("a1", False, "done", st, False, 9.0)
check("Agent result closes with summary", status == "✅ researcher: Look up X · 1 tool · 9s", status)

check("done line counts subagents", mod._done_line(5000, 3, "", 2) == "Done · 5s · 3 tools · 2 subagents", mod._done_line(5000, 3, "", 2))
check("done line: one subagent singular", mod._done_line(None, 0, "", 1) == "Done · 1 subagent")

if fails:
    print(f"\nFAILED: {len(fails)} — " + ", ".join(fails))
    sys.exit(1)
print("ok — turn handler tests passed")
