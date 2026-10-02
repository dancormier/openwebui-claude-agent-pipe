#!/usr/bin/env python3
"""Tests for the cold-resume guard (src/32_cold_resume.py and its use in
`Pipe._pipe_stream`).

Run: python3 test_cold_resume.py [<path-to-pipe.py>]

The turn loop runs against a scripted fake client, as in test_pipe.py; a
held message is one where no client is ever constructed.
"""

import asyncio
import atexit
import base64
import shutil
import sys
import tempfile
import time
import types

from _loader import check, check_eq, load_full, report

mod = load_full()
mod.log.disabled = True

for name in ("open_webui", "open_webui.models", "open_webui.models.files", "open_webui.storage", "open_webui.storage.provider"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["open_webui.models.files"].FileForm = sys.modules["open_webui.models.files"].Files = object()
sys.modules["open_webui.storage.provider"].Storage = object()
USER = {"id": "u1"}


class FakeClient:
    instances = []
    fail_before_init = False

    def __init__(self, options=None):
        self.options = options.__dict__
        self.queries = []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def interrupt(self):
        pass

    async def get_context_usage(self):
        return {"totalTokens": 50_000, "rawMaxTokens": 200_000}

    async def receive_response(self):
        sid = self.options.get("resume") or "sess-new"
        if FakeClient.fail_before_init:
            raise RuntimeError("resume failed")
        yield mod.SystemMessage(subtype="init", data={"session_id": sid})
        yield mod.ResultMessage(subtype="success", duration_ms=100, is_error=False, result="", num_turns=1)


mod.ClaudeSDKClient = FakeClient

root = tempfile.mkdtemp()
atexit.register(shutil.rmtree, root, True)
pipe = mod.Pipe()
pipe.valves.WORKDIR_ROOT = root
pipe.valves.INLINE_TOOL_DETAILS = False
pipe.valves.MODEL = "claude-opus-5-5"


async def collect(agen):
    return "".join([chunk async for chunk in agen])


def turn(chat_id, content):
    FakeClient.instances = []
    body = {"messages": [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": content},
    ]}
    out = asyncio.run(collect(pipe._pipe_stream(body, chat_id, None, None, USER)))
    client = FakeClient.instances[0] if FakeClient.instances else None
    return out, client


def seed(chat_id, idle_seconds=2 * 3600, tokens=265_000, session=True, **extra):
    mod._chat_sessions.pop(chat_id, None)
    meta = {"cwd": f"{root}/{chat_id}", "last_turn_at": int(time.time() - idle_seconds), "last_context_tokens": tokens, **extra}
    if session:
        meta["session_id"] = f"sess-{chat_id}"
    mod._save_session_meta(root, chat_id, meta)


def meta(chat_id):
    return mod._load_session_meta(root, chat_id)


# ---- helpers ----
for text in ("continue", "Continue.", "  ok!", "GO", "yes", "ok..."):
    check(f"ack: {text!r}", mod._is_cold_ack(text))
for text in ("continue with the plan", "okay", "no", "", "go ahead"):
    check(f"not ack: {text!r}", not mod._is_cold_ack(text))
check_eq("resume prefix stripped", mod._extract_resume_prefix("  /resume do it"), (True, "do it"))
check_eq("resume prefix alone", mod._extract_resume_prefix("/resume"), (True, ""))
check_eq("resume prefix case-insensitive", mod._extract_resume_prefix("/RESUME x"), (True, "x"))
check_eq("resume needs a word boundary", mod._extract_resume_prefix("/resumed x"), (False, "/resumed x"))
check_eq("resume only at the start", mod._extract_resume_prefix("hi /resume"), (False, "hi /resume"))
check_eq("cost opus-5-5 at 265k", round(mod._cold_resume_cost("claude-opus-5-5", 265_000), 2), 2.12)
check_eq("cost unknown model", mod._cold_resume_cost("claude-mystery-9", 265_000), None)
check_eq("cost 1m-suffixed id", round(mod._cold_resume_cost("claude-opus-5-5[1m]", 265_000), 2), 2.12)
check_eq("cost dated haiku id", round(mod._cold_resume_cost("claude-haiku-4-5-20251001", 1_000_000), 2), 2.0)
check("fence outgrows backticks inside", mod._code_fence("a ```b``` c").startswith("````\n"), mod._code_fence("a ```b``` c"))
check("pending within ttl", mod._cold_warning_pending({"cold_warned_at": 1000}, 1000 + 60))
check("not pending past ttl", not mod._cold_warning_pending({"cold_warned_at": 1000}, 1000 + 31 * 60))
check("not pending without a warning", not mod._cold_warning_pending({}, 1000))
check("cold+large needs the tracking fields", not mod._is_cold_and_large({"session_id": "s"}, time.time(), 60, 0))

# ---- meta merge keeps other writers' keys ----
mod._save_session_meta(root, "merge", {"session_id": "s1", "cwd": "/w"})
mod._update_session_meta(root, "merge", {"last_turn_at": 5, "last_context_tokens": 7})
check_eq("merge keeps session_id and cwd", meta("merge"), {"session_id": "s1", "cwd": "/w", "last_turn_at": 5, "last_context_tokens": 7})
mod._update_session_meta(root, "merge", {"session_id": "s2", "cwd": "/w", "last_turn_at": None})
check_eq("None removes a key, others survive", meta("merge"), {"session_id": "s2", "cwd": "/w", "last_context_tokens": 7})

# ---- off by default ----
defaults = mod.Pipe.Valves()
check_eq("guard defaults off", defaults.COLD_RESUME_GUARD, False)
check_eq("idle default", defaults.COLD_RESUME_IDLE_MINUTES, 60)
check_eq("min-context default", defaults.COLD_RESUME_MIN_CONTEXT_TOKENS, 150_000)

seed("off")
out, client = turn("off", "hello")
check("disabled: agent runs on a cold, large chat", client is not None and client.queries == ["hello"], out)
check("disabled: still resumes", client and client.options.get("resume") == "sess-off")
m = meta("off")
check("disabled: no warning recorded", "cold_warned_at" not in m and "cold_warned_prompt" not in m, m)
check("turn end records context and time, session kept",
      m.get("last_context_tokens") == 50_000 and time.time() - m.get("last_turn_at", 0) < 60
      and m.get("session_id") == "sess-off" and m.get("cwd"), m)

pipe.valves.COLD_RESUME_GUARD = True

# ---- cold and large: held ----
seed("cold")
pipe.valves.PUBLIC_BASE_URL = "https://chat.example.com/"
out, client = turn("cold", "/effort max ship the fix")
pipe.valves.PUBLIC_BASE_URL = ""
check("held: no agent run", client is None)
check("held: names the context size and idle time", "265k" in out and "idle 2h00m" in out, out)
check("held: prices the cache write", "$2.12 on claude-opus-5-5" in out, out)
check("held: pickup note names the chat, links it, carries the message",
      "Continuing from chat cold (https://chat.example.com/c/cold) — read it for context first. ship the fix" in out, out)
check("held: says how to go ahead", "`continue`" in out, out)
m = meta("cold")
check("held: warning recorded with the message as typed",
      m.get("cold_warned_prompt") == "/effort max ship the fix" and isinstance(m.get("cold_warned_at"), (int, float)), m)
check("held: session and cwd survive", m.get("session_id") == "sess-cold" and m.get("cwd"), m)

# ---- acknowledgement sends the held message ----
out, client = turn("cold", "Continue!")
check("ack: agent runs on the held message", client is not None and client.queries == ["ship the fix"], client and client.queries)
check("ack: held /effort applies", client and client.options.get("effort") == "max", client and client.options.get("effort"))
check("ack: resumes the session", client and client.options.get("resume") == "sess-cold")
m = meta("cold")
check("ack: warning cleared", "cold_warned_at" not in m and "cold_warned_prompt" not in m, m)

# ---- any other reply goes ahead as typed ----
seed("other")
turn("other", "first try")
out, client = turn("other", "actually, do this instead")
check("non-ack: sent as-is", client is not None and client.queries == ["actually, do this instead"], client and client.queries)
check("non-ack: warning cleared", "cold_warned_at" not in meta("other"))

# ---- a stale warning does not release the next message ----
seed("stale", cold_warned_at=time.time() - 31 * 60, cold_warned_prompt="old")
out, client = turn("stale", "continue")
check("stale warning: held again", client is None and meta("stale").get("cold_warned_prompt") == "continue", meta("stale"))

# ---- not held ----
seed("warm", idle_seconds=10 * 60)
_, client = turn("warm", "hi")
check("warm chat runs", client is not None and client.queries == ["hi"])
seed("small", tokens=100_000)
_, client = turn("small", "hi")
check("small chat runs", client is not None and client.queries == ["hi"])
seed("fresh", session=False)
_, client = turn("fresh", "hi")
check("no session to resume: runs", client is not None and "resume" not in client.options)
seed("legacy")
mod._save_session_meta(root, "legacy", {"session_id": "s", "cwd": f"{root}/legacy"})
_, client = turn("legacy", "hi")
check("meta from before tracking: runs", client is not None)
seed("bypass")
_, client = turn("bypass", "/resume do it")
check("/resume bypass: runs with the prefix stripped", client is not None and client.queries == ["do it"], client and client.queries)
check("/resume bypass: nothing recorded", "cold_warned_at" not in meta("bypass"))
seed("unknown")
pipe.valves.MODEL = "claude-mystery-9"
out, _ = turn("unknown", "hi")
pipe.valves.MODEL = "claude-opus-5-5"
check("unknown model: held without a price", "This chat has gone cold" in out and "$" not in out, out)

# ---- an image on the held message comes back with it ----
seed("img")
png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
_, client = turn("img", [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": png}}])
held = meta("img").get("cold_warned_prompt", "")
check("image: held prompt carries the attachment note", client is None and held.startswith("what is this\n\n") and "attachments" in held, held)
_, client = turn("img", "ok")
check("image: ack sends text and attachment note", client is not None and client.queries == [held], client and client.queries)

# ---- /resume is left alone when the guard is off ----
saved = pipe.valves.COLD_RESUME_GUARD
pipe.valves.COLD_RESUME_GUARD = False
seed("off-resume", idle_seconds=10)
_, client = turn("off-resume", "/resume what did we decide?")
check("disabled: /resume reaches the agent as typed",
      client is not None and client.queries == ["/resume what did we decide?"], client and client.queries)
pipe.valves.COLD_RESUME_GUARD = True

# ---- bare /resume with the guard on asks for a message ----
seed("bare")
out, client = turn("bare", "/resume")
check("bare /resume: agent not run", client is None, out)
check("bare /resume: says what to add", "/resume" in out, out)

# ---- a failed resume keeps the warning so the retry still swaps in the held message ----
seed("retry")
turn("retry", "the held question")
FakeClient.fail_before_init = True
try:
    asyncio.run(collect(pipe._pipe_stream({"messages": [{"role": "user", "content": "continue"}]}, "retry", None, None, USER)))
except Exception:
    pass
FakeClient.fail_before_init = False
check("failed resume: warning still pending", "cold_warned_prompt" in meta("retry"), meta("retry"))
_, client = turn("retry", "continue")
check("after a failed resume, continue still sends the held message",
      client is not None and client.queries[-1].endswith("\n\nthe held question"), client and client.queries)
pipe.valves.COLD_RESUME_GUARD = saved

# ---- a warning shown before the guard was switched off still releases ----
seed("switch")
turn("switch", "the real question")
pipe.valves.COLD_RESUME_GUARD = False
_, client = turn("switch", "yes")
check("guard turned off after a warning: ack still sends the held message",
      client is not None and client.queries == ["the real question"], client and client.queries)

report("cold-resume guard tests passed")
