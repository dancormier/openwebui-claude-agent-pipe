#!/usr/bin/env python3
"""Apply the pipe's output-boundary redaction to stdin.

One redaction implementation, both boundaries. The pipe scrubs its own
streamed output in-process; async job results are produced by a detached
worker that never imports the pipe, so it slices this file's sibling
`claude_agent_pipe.py` at the SDK import and reuses `_redact_secrets`. Same
mechanism as `test_redaction.py`: the head is exec'd with pydantic stubbed, so
this runs with zero packages installed.

  python3 redact_stdin.py [path-to-pipe.py] < raw > clean

Exit 0: redacted text on stdout, hit labels (if any) on stderr.
Exit 1: the redactor could not be loaded — nothing is written to stdout and
        the caller MUST fail closed rather than deliver unredacted output.
"""

import pathlib
import sys
import types

SPLIT = "from claude_agent_sdk import ("


def load_redactor(pipe_path):
    """Return the pipe's `_redact_secrets`, without importing the SDK."""
    if "pydantic" not in sys.modules:
        stub = types.ModuleType("pydantic")
        stub.BaseModel = type("BaseModel", (), {})
        stub.Field = lambda *a, **k: None
        sys.modules["pydantic"] = stub
    src = pipe_path.read_text(encoding="utf-8")
    if SPLIT not in src:
        raise RuntimeError(f"{pipe_path}: split marker not found")
    mod = types.ModuleType("pipe_head")
    mod.__dict__["__name__"] = "pipe_head"
    exec(compile(src.split(SPLIT, 1)[0], str(pipe_path), "exec"), mod.__dict__)
    return mod._redact_secrets


def main(argv):
    pipe_path = pathlib.Path(
        argv[1] if len(argv) > 1
        else pathlib.Path(__file__).with_name("claude_agent_pipe.py")
    )
    try:
        redact = load_redactor(pipe_path)
    except Exception as exc:  # noqa: BLE001 — any failure here must fail closed
        print(f"redact_stdin: redactor unavailable: {exc}", file=sys.stderr)
        return 1
    clean, hits = redact(sys.stdin.read())
    sys.stdout.write(clean)
    if hits:
        print("redacted: " + ", ".join(sorted(set(hits))), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
