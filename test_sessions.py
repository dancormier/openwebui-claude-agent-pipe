#!/usr/bin/env python3
"""Tests for the pipe's session-persistence helpers.

Run: python3 hub/pipe/test_sessions.py
     python3 hub/pipe/test_sessions.py <path-to-pipe.py>

Same standalone pattern as test_redaction.py: slice the module at the SDK
import, stub pydantic, exec the head. Runs against the deployed copy too.
"""

import json
import pathlib
import sys
import tempfile
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

fp = mod._history_fingerprint
strip_latest = mod._strip_latest_user

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}\n    got:  {got!r}\n    want: {want!r}")


U1 = {"role": "user", "content": "hello"}
A1 = {"role": "assistant", "content": "hi there"}
U2 = {"role": "user", "content": "second question"}

# ── fingerprint ────────────────────────────────────────────────────────────
check("deterministic", fp([U1, A1]), fp([U1, A1]))
if fp([U1, A1]) == fp([U1]):
    fails.append("fingerprint ignores assistant turn")
if fp([U1, A1]) == fp([A1, U1]):
    fails.append("fingerprint ignores order")

# Normalization: content-parts form and stray whitespace hash the same.
U1_PARTS = {"role": "user", "content": [{"type": "text", "text": "  hello  "}]}
check("parts == string form", fp([U1_PARTS, A1]), fp([U1, A1]))

# System messages and empty texts are excluded.
check(
    "system excluded",
    fp([{"role": "system", "content": "persona"}, U1, A1]),
    fp([U1, A1]),
)

# ── prefix slicing ─────────────────────────────────────────────────────────
check("drops latest user", strip_latest([U1, A1, U2]), [U1, A1])
check("single message → empty", strip_latest([U1]), [])
check("no user at all", strip_latest([A1]), [A1])

# ── session meta round-trip ────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as root:
    check("missing meta → {}", mod._load_session_meta(root, "chat1"), {})
    mod._save_session_meta(root, "chat1", {"session_id": "s-1", "cwd": "/x"})
    check(
        "meta round-trip",
        mod._load_session_meta(root, "chat1"),
        {"session_id": "s-1", "cwd": "/x"},
    )
    # Corrupt file → {} (recreate, never crash). Meta lives in a shared
    # .sessions/ dot-directory outside any chat workdir (see
    # _session_meta_path) — never under the chat's own workdir.
    (pathlib.Path(root) / ".sessions" / "chat1.json").write_text("{oops")
    check("corrupt meta → {}", mod._load_session_meta(root, "chat1"), {})

# ── fingerprint store ──────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as root:
    check("missing store → {}", mod._load_fp_store(root), {})
    entry = {"session_id": "s-2", "workdir": "/w", "cwd": "/w", "ts": time.time()}
    mod._save_fp_store(root, {"abc": entry})
    check("store round-trip", mod._load_fp_store(root), {"abc": entry})
    # Stale entries pruned on load.
    old = dict(entry, ts=time.time() - mod._FP_STORE_TTL_SECONDS - 60)
    mod._save_fp_store(root, {"abc": entry, "old": old})
    check("stale pruned", sorted(mod._load_fp_store(root)), ["abc"])
    # Corrupt store → {}.
    (pathlib.Path(root) / mod._FP_STORE_FILE).write_text("not json")
    check("corrupt store → {}", mod._load_fp_store(root), {})

# ── repo map (PR 2) ────────────────────────────────────────────────────────
parse_map = mod._parse_repo_map
extract_repo = mod._extract_repo_prefix

check(
    "map parses",
    parse_map("homelab=/Users/x/homelab, vault=/Users/x/Obsidian/personal"),
    {"homelab": "/Users/x/homelab", "vault": "/Users/x/Obsidian/personal"},
)
check("map empty", parse_map(""), {})
check("map junk dropped", parse_map("nopath,=x, =,a=b"), {"a": "b"})

check("prefix extracted", extract_repo("#repo:homelab fix the daemon"),
      ("homelab", "fix the daemon"))
check("prefix alone", extract_repo("#repo:vault"), ("vault", ""))
check("no prefix", extract_repo("plain question"), (None, "plain question"))
check("mid-text not a prefix", extract_repo("see #repo:x docs"),
      (None, "see #repo:x docs"))

if fails:
    print(f"FAIL ({len(fails)})")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("ok — session tests passed")
