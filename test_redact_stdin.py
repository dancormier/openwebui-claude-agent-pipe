#!/usr/bin/env python3
"""Tests for redact_stdin.py — the async worker's redaction boundary.

Run: python3 hub/pipe/test_redact_stdin.py

The worker is a separate process from the pipe, so it reaches the redactor by
slicing the pipe's head. These tests drive the helper exactly as the worker
does: subprocess, text on stdin, redacted text on stdout.
"""

import pathlib
import subprocess
import sys
import tempfile

HELPER = pathlib.Path(__file__).with_name("redact_stdin.py")

# Synthetic values only — shaped like the patterns, never live credentials.
ANTHROPIC = "sk-ant-oat01-" + "A1b2C3d4E5" * 9
GITHUB = "ghp_" + "z9y8x7w6v5" * 3

fails = []


def run(stdin_text, *args):
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=stdin_text, capture_output=True, text=True,
    )


def check(name, got, want):
    if got != want:
        fails.append(f"{name}\n    got:  {got!r}\n    want: {want!r}")


# Secrets are scrubbed, exit 0, labels reported on stderr.
r = run(f"result: {ANTHROPIC} and {GITHUB} done")
check("secret run exits 0", r.returncode, 0)
for needle in (ANTHROPIC, GITHUB):
    if needle in r.stdout:
        fails.append(f"leaked {needle[:12]}… into stdout")
if "«redacted:anthropic-token»" not in r.stdout:
    fails.append(f"anthropic not replaced: {r.stdout!r}")
if "anthropic-token" not in r.stderr or "github-token" not in r.stderr:
    fails.append(f"hit labels missing from stderr: {r.stderr!r}")

# Ordinary markdown passes through byte-for-byte — the worker delivers this
# text verbatim into a chat, so mangling it is a real bug.
prose = "## Result\n\nSee https://example.com/x — the `sk-` prefix is fine.\n"
r = run(prose)
check("prose untouched", r.stdout, prose)
check("prose exits 0", r.returncode, 0)
check("prose reports no hits", r.stderr, "")

# Empty input is a no-op, not a crash.
r = run("")
check("empty exits 0", r.returncode, 0)
check("empty stdout", r.stdout, "")

# Fail closed: pointed at a file with no split marker, it must exit non-zero
# and write NOTHING to stdout. The worker keys its "never deliver unredacted
# output" guarantee on exactly this.
with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
    fh.write("x = 1\n")
    bogus = fh.name
r = run(f"secret {ANTHROPIC}", bogus)
check("bogus pipe exits 1", r.returncode, 1)
check("bogus pipe writes no stdout", r.stdout, "")
if ANTHROPIC in r.stderr:
    fails.append("leaked the secret into stderr on the failure path")

# A missing pipe file is the same failure mode.
r = run("hello", "/nonexistent/claude_agent_pipe.py")
check("missing pipe exits 1", r.returncode, 1)
check("missing pipe writes no stdout", r.stdout, "")

# ── known-value redaction ──────────────────────────────────────────────────
# Pattern matching cannot know a uuid token or an ntfy topic; the worker names
# them from secrets.tpl and the values come from the environment it sources in
# a subshell. Synthetic values only.
import base64
import os

UUID = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"
TOPIC = "topic with/slash+plus"          # URL-encoding and base64 both differ from the text
PASSWORD = "p@ss>w0rd?~~!!"              # urlsafe and standard base64 differ
ENV = dict(os.environ, FAKE_UUID=UUID, FAKE_TOPIC=TOPIC, FAKE_PW=PASSWORD, FAKE_SHORT="abc")

with tempfile.NamedTemporaryFile("w", suffix=".tpl", delete=False) as fh:
    fh.write("# comment\nexport FAKE_UUID=op://v/i/f\nexport FAKE_TOPIC=op://v/i/f\n"
             "export FAKE_PW=op://v/i/f\nexport FAKE_SHORT=op://v/i/f\nexport FAKE_ABSENT=op://v/i/f\n")
    tpl = fh.name


def run_env(stdin_text, *args):
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=stdin_text, capture_output=True, text=True, env=ENV,
    )


def b64(v, urlsafe=False):
    f = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return f(v.encode()).decode()


forms = {
    "uuid exact": UUID,
    "uuid hex": UUID.encode().hex(),
    "uuid base64": b64(UUID),
    "uuid base64 unpadded": b64(UUID).rstrip("="),
    "topic exact": TOPIC,
    "topic url-encoded": "topic%20with%2Fslash%2Bplus",
    "topic base64": b64(TOPIC),
    "pw exact": PASSWORD,
    "pw base64": b64(PASSWORD),
    "pw urlsafe base64": b64(PASSWORD, urlsafe=True),
    "pw hex upper": PASSWORD.encode().hex().upper(),
}
for name, form in forms.items():
    r = run_env(f"result: {form} done", "--known-from-tpl", tpl)
    check(f"{name} exits 0", r.returncode, 0)
    if form in r.stdout:
        fails.append(f"{name}: leaked {form[:10]}… into stdout")
    if "«redacted:known:FAKE_" not in r.stdout:
        fails.append(f"{name}: not replaced, got {r.stdout!r}")
    if "known:FAKE_" not in r.stderr:
        fails.append(f"{name}: hit label missing from stderr: {r.stderr!r}")

# Labels name the variable, never the value.
r = run_env(f"x {UUID} y", "--known-from-tpl", tpl)
check("uuid labelled by name", "redacted: known:FAKE_UUID" in r.stderr, True)
if UUID in r.stderr:
    fails.append("known value leaked into stderr")

# Pattern and known-value hits compose in one pass.
r = run_env(f"{ANTHROPIC} then {UUID}", "--known-from-tpl", tpl)
for needle in (ANTHROPIC, UUID):
    if needle in r.stdout:
        fails.append(f"composed: leaked {needle[:10]}…")
check("composed labels", "anthropic-token" in r.stderr and "known:FAKE_UUID" in r.stderr, True)

# A value under the length floor is NOT scrubbed — it would eat prose — and an
# exported name with no value is reported by name, not treated as fatal.
r = run_env("abc is short; the absent one is fine", "--known-from-tpl", tpl)
check("short value left alone", r.stdout, "abc is short; the absent one is fine")
check("short+absent exit 0", r.returncode, 0)
check("missing names reported", "known-values: no value for FAKE_SHORT, FAKE_ABSENT" in r.stderr, True)

# Prose is byte-for-byte intact with every known value loaded.
r = run_env(prose, "--known-from-tpl", tpl)
check("prose untouched with known values", r.stdout, prose)

# --known-env names variables directly, and composes with the template.
r = run_env(f"a {UUID} b", "--known-env", "FAKE_UUID")
if UUID in r.stdout:
    fails.append("--known-env: uuid leaked")
r = run_env(f"a {UUID} b {PASSWORD}", "--known-env", "FAKE_PW", "--known-from-tpl", tpl)
for needle in (UUID, PASSWORD):
    if needle in r.stdout:
        fails.append(f"--known-env + tpl: leaked {needle[:6]}…")

# A template the caller named but that does not exist is a configuration
# error of the same class as a missing pipe: fail closed, write nothing.
r = run_env(f"secret {UUID}", "--known-from-tpl", "/nonexistent/secrets.tpl")
check("missing tpl exits 1", r.returncode, 1)
check("missing tpl writes no stdout", r.stdout, "")

if fails:
    print(f"FAIL ({len(fails)})")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("ok — redact_stdin tests passed")
