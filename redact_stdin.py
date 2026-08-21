#!/usr/bin/env python3
"""Apply the pipe's output-boundary redaction to stdin.

One redaction implementation, both boundaries. The pipe scrubs its own
streamed output in-process; async job results are produced by a detached
worker that never imports the pipe, so it slices this file's sibling
`claude_agent_pipe.py` at the SDK import and reuses `_redact_secrets`. Same
mechanism as `test_redaction.py`: the head is exec'd with pydantic stubbed, so
this runs with zero packages installed.

  python3 redact_stdin.py [path-to-pipe.py] [--known-from-tpl <secrets.tpl>]
                          [--known-env NAME[,NAME...]] < raw > clean

Known-value redaction. The pipe's patterns are prefix-anchored, which cannot
recognise a bare uuid (TICKTICK_ACCESS_TOKEN), an arbitrary word (NTFY_TOPIC)
or a password (MACOS_KEYCHAIN_PASSWORD). A caller that can load the live
values names them instead: `--known-from-tpl` takes the `export NAME=` lines
of secrets.tpl (names only — values come from this process's environment,
which the caller fills by sourcing ~/.secrets in a subshell around this
command), `--known-env` names variables directly. Every value present is
scrubbed as its exact text and as its base64, urlsafe-base64, hex and
URL-encoded forms, after the pattern pass. Values shorter than 8 characters
are skipped: nothing distinguishes them from prose, and scrubbing every
"true" would mangle the result. Values are never written anywhere; stderr
names only the variables that were empty.

Exit 0: redacted text on stdout, hit labels (if any) on stderr.
Exit 1: the redactor could not be loaded, or a named template is missing —
        nothing is written to stdout and the caller MUST fail closed rather
        than deliver unredacted output.
"""

import base64
import os
import pathlib
import re
import sys
import types
import urllib.parse

SPLIT = "from claude_agent_sdk import ("
MIN_KNOWN_LEN = 8
_TPL_EXPORT = re.compile(r"^export\s+([A-Z][A-Z0-9_]*)=", re.M)


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


def tpl_names(tpl_path):
    return _TPL_EXPORT.findall(tpl_path.read_text(encoding="utf-8"))


def _variants(value):
    raw = value.encode("utf-8")
    forms = {value, raw.hex(), raw.hex().upper(), urllib.parse.quote(value, safe="")}
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = enc.decode("ascii")
        forms.add(text)
        forms.add(text.rstrip("="))
    return {f for f in forms if len(f) >= MIN_KNOWN_LEN}


def known_values(names, environ):
    """Return ([(name, variant)] longest-first, [names with no usable value])."""
    pairs, missing = [], []
    for name in names:
        value = environ.get(name, "")
        if len(value) < MIN_KNOWN_LEN:
            missing.append(name)
            continue
        pairs.extend((name, v) for v in _variants(value))
    # Longest first, so a padded base64 form is not left behind as a stub by
    # its unpadded twin, and the exact value never outlives its encodings.
    pairs.sort(key=lambda p: len(p[1]), reverse=True)
    return pairs, missing


def redact_known(text, pairs):
    hits = []
    for name, variant in pairs:
        n = text.count(variant)
        if n:
            text = text.replace(variant, f"«redacted:known:{name}»")
            hits.extend([f"known:{name}"] * n)
    return text, hits


def parse_args(argv):
    pipe_path = None
    names = []
    tpl = None
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg == "--known-from-tpl":
            tpl = pathlib.Path(args.pop(0))
        elif arg == "--known-env":
            names.extend(n for n in args.pop(0).split(",") if n)
        elif arg.startswith("--"):
            raise SystemExit(f"redact_stdin: unknown option {arg}")
        elif pipe_path is None:
            pipe_path = pathlib.Path(arg)
        else:
            raise SystemExit(f"redact_stdin: unexpected argument {arg}")
    if pipe_path is None:
        pipe_path = pathlib.Path(__file__).with_name("claude_agent_pipe.py")
    return pipe_path, tpl, names


def main(argv):
    pipe_path, tpl, names = parse_args(argv)
    try:
        redact = load_redactor(pipe_path)
        if tpl is not None:
            names = tpl_names(tpl) + names
    except Exception as exc:  # noqa: BLE001 — any failure here must fail closed
        print(f"redact_stdin: redactor unavailable: {exc}", file=sys.stderr)
        return 1
    pairs, missing = known_values(names, os.environ)
    clean, hits = redact(sys.stdin.read())
    clean, known_hits = redact_known(clean, pairs)
    sys.stdout.write(clean)
    hits.extend(known_hits)
    if hits:
        print("redacted: " + ", ".join(sorted(set(hits))), file=sys.stderr)
    if missing:
        print("known-values: no value for " + ", ".join(missing), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
