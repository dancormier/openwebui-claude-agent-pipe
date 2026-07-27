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

if fails:
    print(f"FAIL ({len(fails)})")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("ok — redact_stdin tests passed")
