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
    "context 74k/200k (37%)",
)
check("status no usage", ctx_status(None, 200_000), "")
check("status zero usage", ctx_status({}, 200_000), "")
check("status disabled", ctx_status(USAGE, 0), "")
check(
    "status over window still renders",
    ctx_status({"input_tokens": 300_000}, 200_000),
    "context 300k/200k (150%)",
)

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
