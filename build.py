#!/usr/bin/env python3
"""Assemble claude_agent_pipe.py from src/, and docs/valves.md from its Valves.

Open WebUI stores a function as one self-contained body, and three consumers
(the test suites, redact_stdin.py, and the deny-secret-egress hook) slice that
body at the `from claude_agent_sdk import (` line to reach the pure helpers
above it. So the modules under src/ are concatenated verbatim, in filename
order, into the committed claude_agent_pipe.py; edit src/, never the output.

The valves reference is generated from the `Valves` class by parsing the
source (no pydantic or SDK needed), so the table cannot drift from the code.

    python3 build.py           # rewrite claude_agent_pipe.py and docs/valves.md
    python3 build.py --check   # exit 1 if either committed file is stale (CI)
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
OUT = HERE / "claude_agent_pipe.py"
VALVES_DOC = HERE / "docs" / "valves.md"


def assemble() -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.py")))


def _valves(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Valves":
            break
    else:
        raise SystemExit("no Valves class found")
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        call = item.value
        if not isinstance(call, ast.Call):
            continue
        kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
        yield item.target.id, ast.unparse(item.annotation), kwargs.get("default"), kwargs.get("description", "")


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def valves_doc(source: str) -> str:
    lines = [
        "# Valves",
        "",
        "Generated from the `Valves` class by `python3 build.py`; edit the",
        "descriptions in `src/80_pipe.py`, not here. Set them in Open WebUI under",
        "Admin → Functions → the pipe → Valves. Valves are global to the function,",
        "not per user.",
        "",
        "| Valve | Type | Default | What it does |",
        "|---|---|---|---|",
    ]
    for name, typ, default, desc in _valves(source):
        shown = "*(empty)*" if default == "" else f"`{default}`"
        lines.append(f"| `{name}` | {typ} | {shown} | {_cell(desc)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    built = assemble()
    doc = valves_doc(built)
    if "--check" in sys.argv[1:]:
        stale = []
        if (OUT.read_text(encoding="utf-8") if OUT.exists() else "") != built:
            stale.append(OUT.name)
        if (VALVES_DOC.read_text(encoding="utf-8") if VALVES_DOC.exists() else "") != doc:
            stale.append(str(VALVES_DOC.relative_to(HERE)))
        if not stale:
            print("ok — claude_agent_pipe.py and docs/valves.md match src/")
            return 0
        print(f"STALE — {', '.join(stale)} differ from src/; run python3 build.py")
        return 1
    OUT.write_text(built, encoding="utf-8")
    VALVES_DOC.parent.mkdir(exist_ok=True)
    VALVES_DOC.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT.name} ({len(built.splitlines())} lines) and {VALVES_DOC.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
