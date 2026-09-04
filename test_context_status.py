#!/usr/bin/env python3
"""Tests for the pipe's context-usage status helpers.

Run: python3 hub/pipe/test_context_status.py
     python3 hub/pipe/test_context_status.py <path-to-pipe.py>

Same standalone pattern as test_sessions.py: slice the module at the SDK
import, stub pydantic, exec the head. Runs against the deployed copy too.
"""

import pathlib
import sys
import time
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

NOW = time.mktime((2026, 9, 4, 12, 0, 0, 0, 0, -1))
IN_4H = NOW + 4 * 3600
IN_3D = NOW + 3 * 86400
check("resets_in under a minute", mod._fmt_resets_in(NOW + 59, NOW), "<1m")
check("resets_in minutes only", mod._fmt_resets_in(NOW + 14 * 60, NOW), "14m")
check("resets_in hours and minutes", mod._fmt_resets_in(NOW + 74 * 60, NOW), "1h 14m")
check("resets_in days", mod._fmt_resets_in(NOW + 26 * 3600 + 4 * 60, NOW), "1d 2h 4m")
check("resets_in exact hours drop zero minutes", mod._fmt_resets_in(NOW + 2 * 3600, NOW), "2h")
check("resets_in skips a zero middle unit", mod._fmt_resets_in(NOW + 86400 + 4 * 60, NOW), "1d 4m")

check("model short name: fable", mod._model_short_name("claude-fable-5-1"), "fable")
check("model short name: haiku", mod._model_short_name("claude-haiku-4-5-20260101"), "haiku")
check("model short name: unknown id", mod._model_short_name("gpt-5"), "model")
check("model short name: empty", mod._model_short_name(None), "model")

HAIKU_EVENT = {
    "status": "allowed", "resetsAt": IN_4H, "rateLimitType": "five_hour",
    "overageStatus": "allowed", "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.55, "resetsAt": IN_4H},
        "seven_day": {"utilization": 0.49, "resetsAt": IN_3D},
    },
}
cache = {}
check("no limits → empty", mod._usage_limits(cache, NOW), {})
mod._note_rate_limit(cache, None, "haiku")
mod._note_rate_limit(cache, {"rateLimitType": None}, "haiku")
check("event without a type is ignored", mod._usage_limits(cache, NOW), {})
mod._note_rate_limit(cache, HAIKU_EVENT, "haiku")
check(
    "both unified windows land",
    mod._usage_limits(cache, NOW),
    {
        "session_used": 55, "session_resets_in": "4h",
        "weekly_used": 49, "weekly_resets_in": "3d",
    },
)
mod._note_rate_limit(cache, {
    "rateLimitType": "seven_day_overage_included", "utilization": 0.84, "resetsAt": IN_3D,
    "unifiedWindows": {"five_hour": {"utilization": 0.6, "resetsAt": IN_4H}},
}, "fable")
later = mod._usage_limits(cache, NOW)
check("model window keyed by the turn's model", (later["fable_used"], later["fable_resets_in"]), (84, "3d"))
check("later turn: latest per window, others kept", (later["session_used"], later["weekly_used"]), (60, 49))
mod._note_rate_limit(cache, {"rateLimitType": "seven_day", "utilization": 0.5, "resetsAt": IN_3D})
check("top-level plain window without unifiedWindows", mod._usage_limits(cache, NOW)["weekly_used"], 50)
mod._note_rate_limit(cache, {"rateLimitType": "seven_day_sonnet", "utilization": 0.1})
check("model window without reset", ("model_used" in mod._usage_limits(cache, NOW), "model_resets_in" in mod._usage_limits(cache, NOW)), (True, False))
mod._note_rate_limit(cache, {"rateLimitType": "overage", "utilization": 0.2, "resetsAt": IN_3D, "isUsingOverage": True}, "fable")
with_overage = mod._usage_limits(cache, NOW)
check("overage → extra_usage", (with_overage["extra_usage_used"], with_overage["extra_usage_in_use"]), (20, True))
check(
    "key order: session, weekly, models, extra_usage",
    list(with_overage),
    [
        "session_used", "session_resets_in", "weekly_used", "weekly_resets_in",
        "fable_used", "fable_resets_in", "model_used",
        "extra_usage_used", "extra_usage_resets_in", "extra_usage_in_use",
    ],
)
mod._note_rate_limit(cache, {"rateLimitType": "five_hour", "utilization": 0.6, "resetsAt": IN_4H, "isUsingOverage": False}, "fable")
check("in_use cleared when overage stops", "extra_usage_in_use" in mod._usage_limits(cache, NOW), False)
mod._note_rate_limit(cache, {"rateLimitType": "overage", "utilization": 0.9, "resetsAt": NOW - 60}, "fable")
expired = mod._usage_limits(cache, NOW)
check("expired window renders nothing", [k for k in expired if k.startswith("extra_usage")], [])
check("expired window dropped from cache", "extra_usage" in cache, False)
check("future window still renders after expiry sweep", expired["session_used"], 60)
check("window without reset survives sweep", "model" in cache, True)

full = owui_usage(USAGE, 80_000, 4, {"session_used": 37, "session_resets_in": "3h 2m"})
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
        "session_resets_in": "3h 2m",
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
