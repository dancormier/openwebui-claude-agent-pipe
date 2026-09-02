#!/usr/bin/env python3
"""Assemble claude_agent_pipe.py from src/.

Open WebUI stores a function as one self-contained body, and three consumers
(the test suites, redact_stdin.py, and the deny-secret-egress hook) slice that
body at the `from claude_agent_sdk import (` line to reach the pure helpers
above it. So the modules under src/ are concatenated verbatim, in filename
order, into the committed claude_agent_pipe.py; edit src/, never the output.

    python3 build.py           # rewrite claude_agent_pipe.py
    python3 build.py --check   # exit 1 if the committed file is stale (CI)
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
OUT = HERE / "claude_agent_pipe.py"


def assemble() -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.py")))


def main() -> int:
    built = assemble()
    if "--check" in sys.argv[1:]:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current == built:
            print("ok — claude_agent_pipe.py matches src/")
            return 0
        print("STALE — claude_agent_pipe.py differs from src/; run python3 build.py")
        return 1
    OUT.write_text(built, encoding="utf-8")
    print(f"wrote {OUT.name} ({len(built.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
