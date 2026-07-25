#!/usr/bin/env python3
"""Tests for the pipe's output-boundary secret redaction.

Run: python3 hub/pipe/test_redaction.py
     python3 hub/pipe/test_redaction.py <path-to-pipe.py>

Imports the redaction helpers without importing the whole pipe module, which
needs claude_agent_sdk / pydantic that aren't installed outside Open WebUI's
backend. The module is sliced at the SDK import, pydantic is stubbed, and the
result is exec'd standalone — so this also runs against the *deployed* copy
pulled out of webui.db on a host with no Python dependencies installed, which
is the copy that actually matters.
"""

import pathlib
import sys
import types

PIPE = pathlib.Path(
    sys.argv[1] if len(sys.argv) > 1
    else pathlib.Path(__file__).with_name("claude_agent_pipe.py")
)
SPLIT = "from claude_agent_sdk import ("

# The sliced head still imports pydantic for the Valves model. Stub it: the
# redaction helpers under test don't touch it, and requiring the real package
# would keep this suite from running where it's most useful.
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

_redact_secrets = mod._redact_secrets
_redact_event = mod._redact_event
_StreamRedactor = mod._StreamRedactor

# Synthetic values only. None of these is or ever was a live credential; they
# are shaped to match the patterns and nothing more.
ANTHROPIC = "sk-ant-oat01-" + "A1b2C3d4E5" * 9          # 108 chars, real length
GITHUB = "ghp_" + "z9y8x7w6v5" * 3
SLACK = "xoxb-1234567890-1234567890-" + "abcdefghij" * 2
AWS = "AKIA" + "ABCDEFGH12345678"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}\n    got:  {got!r}\n    want: {want!r}")


def contains_none_of(name, text, needles):
    for n in needles:
        if n in text:
            fails.append(f"{name}: leaked {n[:12]}… into output")


# ── whole-string redaction ─────────────────────────────────────────────────
clean, hits = _redact_secrets(f"token is {ANTHROPIC} ok")
check("anthropic replaced", clean, "token is «redacted:anthropic-token» ok")
check("anthropic labelled", hits, ["anthropic-token"])

for label, value in [
    ("github-token", GITHUB),
    ("slack-token", SLACK),
    ("aws-access-key", AWS),
    ("jwt", JWT),
]:
    clean, hits = _redact_secrets(f"x {value} y")
    contains_none_of(f"{label} scrubbed", clean, [value])
    check(f"{label} labelled", hits, [label])

clean, _ = _redact_secrets("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
contains_none_of("private key header scrubbed", clean, ["BEGIN OPENSSH PRIVATE KEY"])

# Ordinary prose must survive untouched.
prose = "The sk- prefix identifies these keys; see docs at https://example.com/x."
clean, hits = _redact_secrets(prose)
check("prose untouched", clean, prose)
check("prose no hits", hits, [])

# ── chunk-boundary safety ──────────────────────────────────────────────────
# The real leak mode: a secret split across streaming deltas.
for split in range(1, len(ANTHROPIC)):
    r = _StreamRedactor()
    out = r.feed("here: " + ANTHROPIC[:split])
    out += r.feed(ANTHROPIC[split:] + " done")
    out += r.flush()
    contains_none_of(f"split at {split}", out, [ANTHROPIC])
    if "«redacted:anthropic-token»" not in out:
        fails.append(f"split at {split}: secret not replaced, got {out!r}")
    if not out.endswith(" done"):
        fails.append(f"split at {split}: trailing text lost, got {out!r}")

# Many tiny chunks, token-at-a-time, worst case for a per-chunk regex.
r = _StreamRedactor()
out = "".join(r.feed(c) for c in ("prefix " + ANTHROPIC + " suffix")) + r.flush()
contains_none_of("char-by-char", out, [ANTHROPIC])
check("char-by-char result", out, "prefix «redacted:anthropic-token» suffix")

# A held tail must still be released when the stream ends on it.
r = _StreamRedactor()
out = r.feed("trailing sk-") + r.flush()
check("held tail flushed", out, "trailing sk-")

# Prose streams through without the held tail eating text.
r = _StreamRedactor()
out = "".join(r.feed(w) for w in ["Kubernetes ", "clusters ", "scale"]) + r.flush()
check("prose streams intact", out, "Kubernetes clusters scale")

# ── event payloads ─────────────────────────────────────────────────────────
hits = []
ev = _redact_event(
    {"type": "status", "data": {"description": f"Bash: echo {ANTHROPIC}", "done": False}},
    hits,
)
contains_none_of("event scrubbed", ev["data"]["description"], [ANTHROPIC])
check("event non-string preserved", ev["data"]["done"], False)
check("event labelled", hits, ["anthropic-token"])

hits = []
ev = _redact_event({"a": [{"b": GITHUB}]}, hits)
contains_none_of("nested event scrubbed", str(ev), [GITHUB])
check("nested event labelled", hits, ["github-token"])

# ── report ─────────────────────────────────────────────────────────────────
if fails:
    print(f"FAIL ({len(fails)})")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("ok — redaction tests passed")
