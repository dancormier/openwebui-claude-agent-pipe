#!/usr/bin/env python3
"""Tests for the pipe's context-usage status helpers.

Run: python3 hub/pipe/test_context_status.py
     python3 hub/pipe/test_context_status.py <path-to-pipe.py>

Same standalone pattern as test_sessions.py: slice the module at the SDK
import, stub pydantic, exec the head. Runs against the deployed copy too.
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

fmt = mod._fmt_tokens
ctx_tokens = mod._context_tokens
ctx_status = mod._context_status
owui_usage = mod._owui_usage
fmt_dur = mod._fmt_duration
effort_prefix = mod._extract_effort_prefix

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


check("fmt small", fmt(999), "999")
check("fmt k", fmt(74_400), "74k")
check("fmt round k", fmt(199_600), "200k")
check("fmt 1M", fmt(1_000_000), "1M")
check("fmt fractional M", fmt(1_250_000), "1.25M")

USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 476,
    "cache_read_input_tokens": 62028,
    "output_tokens": 825,
}
check("ctx sum", ctx_tokens(USAGE), 2 + 476 + 62028 + 825)
check("ctx none", ctx_tokens(None), 0)
check("ctx empty", ctx_tokens({}), 0)
check("ctx null field", ctx_tokens({"input_tokens": None, "output_tokens": 5}), 5)

check(
    "status basic",
    ctx_status({"input_tokens": 74_000, "output_tokens": 0}, 200_000),
    "74k/200k (37%)",
)
check("status no usage", ctx_status(None, 200_000), "")
check("status zero usage", ctx_status({}, 200_000), "")
check("status disabled", ctx_status(USAGE, 0), "")
check(
    "status over window still renders",
    ctx_status({"input_tokens": 300_000}, 200_000),
    "300k/200k (150%)",
)

check(
    "owui usage normalization",
    owui_usage(USAGE),
    {
        "prompt_tokens": 2 + 476 + 62028,
        "completion_tokens": 825,
        "total_tokens": 2 + 476 + 62028 + 825,
        "cache_read_input_tokens": 62028,
        "cache_creation_input_tokens": 476,
    },
)
check(
    "owui usage null fields",
    owui_usage({"input_tokens": 10, "output_tokens": None}),
    {
        "prompt_tokens": 10,
        "completion_tokens": 0,
        "total_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
)

import time

NOW = time.mktime((2026, 9, 4, 12, 0, 0, 0, 0, -1))
IN_4H = NOW + 4 * 3600
IN_3D = NOW + 3 * 86400
check("reset today → time only", mod._fmt_reset(IN_4H, NOW), time.strftime("%H:%M", time.localtime(IN_4H)))
check("reset another day → weekday + time", mod._fmt_reset(IN_3D, NOW), time.strftime("%a %H:%M", time.localtime(IN_3D)))
check("reset crosses midnight → weekday", mod._fmt_reset(NOW + 13 * 3600, NOW), time.strftime("%a %H:%M", time.localtime(NOW + 13 * 3600)))

check("rate limit key: five_hour", mod._rate_limit_key("five_hour"), "session")
check("rate limit key: seven_day", mod._rate_limit_key("seven_day"), "weekly")
check("rate limit key: model window", mod._rate_limit_key("seven_day_opus"), "opus")
check("rate limit key: unknown passes through", mod._rate_limit_key("overage"), "overage")

cache = {}
check("no limits → empty", mod._usage_limits(cache, NOW), {})
mod._note_rate_limit(cache, "five_hour", 0.37, IN_4H)
mod._note_rate_limit(cache, "seven_day", 0.128, IN_3D)
mod._note_rate_limit(cache, None, 0.5, IN_4H)
check(
    "limits from cache",
    mod._usage_limits(cache, NOW),
    {
        "session_used": 37,
        "session_resets": time.strftime("%H:%M", time.localtime(IN_4H)),
        "weekly_used": 13,
        "weekly_resets": time.strftime("%a %H:%M", time.localtime(IN_3D)),
    },
)
mod._note_rate_limit(cache, "five_hour", 0.6, IN_4H)
mod._note_rate_limit(cache, "seven_day_opus", 0.05, None)
later = mod._usage_limits(cache, NOW)
check("later turn: latest per type, others kept", later["session_used"], 60)
check("later turn: weekly survives", later["weekly_used"], 13)
check("later turn: model window without reset", (later["opus_used"], "opus_resets" in later), (5, False))

full = owui_usage(USAGE, 80_000, 4, {"session_used": 37, "session_resets": "16:00"})
check(
    "owui usage with turn data",
    full,
    {
        "prompt_tokens": 2 + 476 + 62028,
        "completion_tokens": 825,
        "total_tokens": 2 + 476 + 62028 + 825,
        "cache_read_input_tokens": 62028,
        "cache_creation_input_tokens": 476,
        "duration": "1m20s",
        "duration_ms": 80_000,
        "num_turns": 4,
        "session_used": 37,
        "session_resets": "16:00",
    },
)
check("owui usage keeps token keys first", list(full)[:5], ["prompt_tokens", "completion_tokens", "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
check("owui usage omits absent turn data", set(owui_usage(USAGE, None, None, {})) & {"duration", "duration_ms", "num_turns"}, set())

check("dur seconds", fmt_dur(4_200), "4s")
check("dur minutes", fmt_dur(102_000), "1m42s")
check("dur hours", fmt_dur(3_725_000), "1h02m")
check("dur zero", fmt_dur(0), "0s")

check("effort none", effort_prefix("hello there"), (None, "hello there"))
check("effort space", effort_prefix("/effort low do the thing"), ("low", "do the thing"))
check("effort colon", effort_prefix("/effort: xhigh go"), ("xhigh", "go"))
check("effort case", effort_prefix("/EFFORT MAX go"), ("max", "go"))
check("effort bogus level", effort_prefix("/effort turbo go"), (None, "/effort turbo go"))
check("effort mid-message ignored", effort_prefix("try /effort low"), (None, "try /effort low"))

if fails:
    print("FAIL")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print(f"ok — {PIPE}")
