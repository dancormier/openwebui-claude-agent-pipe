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
check("blank WORKDIR_ROOT falls back to the default", mod._workdir_root("  "), mod._DEFAULT_WORKDIR_ROOT)
check("set WORKDIR_ROOT is kept, trimmed", mod._workdir_root(" /srv/pipe "), "/srv/pipe")
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

# ── model resolution (PR 3) ────────────────────────────────────────────────
resolve = mod._model_from_requested
check("suffixed id wins", resolve("claude_code.claude-code-claude-fable-5", "claude-opus-5"),
      "claude-fable-5")
check("base id falls through", resolve("claude_code.claude-code", "claude-opus-5"),
      "claude-opus-5")
check("empty falls through", resolve("", "claude-opus-5"), "claude-opus-5")

# ── agent env (chat id handed to the SDK subprocess) ───────────────────────
_agent_env = mod._agent_env

_NO_BG = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}

check("chat id becomes HUB_CHAT_ID",
      _agent_env("be6f5945-867f-4be5-8055-0a4e5d65f136"),
      {**_NO_BG, "HUB_CHAT_ID": "be6f5945-867f-4be5-8055-0a4e5d65f136"})

# Absent, NOT empty: hub-async.sh treats an unset variable as "this session has
# no chat", and the async worker relies on that to make nested jobs fail.
check("no chat id yields no HUB_CHAT_ID key at all", _agent_env(None), _NO_BG)
check("empty chat id yields no HUB_CHAT_ID key at all", _agent_env(""), _NO_BG)

# ── gateway contract for repo-rooted sessions ──────────────────────────────
# A #repo: session's cwd is the mapped repo, so the SDK loads that repo's
# CLAUDE.md and never reads hub/AGENTS.md — which is how #repo:homelab ran with
# no Hard Rules at all under bypassPermissions until 2026-07-27. The pipe now
# appends the contract itself whenever the agent is rooted outside WORKDIR_ROOT.
_gateway_contract = mod._gateway_contract

_root = tempfile.mkdtemp()
_contract = pathlib.Path(_root) / "AGENTS.md"
_contract.write_text("# Hub contract\n\nHard Rules go here.\n", encoding="utf-8")
_workdir_root = pathlib.Path(_root) / "hub"
(_workdir_root / "chat-abc").mkdir(parents=True)
_repo = pathlib.Path(_root) / "homelab"
_repo.mkdir()


def sent(text):
    """The contract body, ignoring the injected framing preamble."""
    return text.split("\n\n", 1)[1] if text else text


# Repo-rooted: the agent cannot reach the contract from its cwd, so send it.
check("repo cwd gets the contract",
      sent(_gateway_contract(str(_repo), str(_workdir_root), str(_contract))),
      "# Hub contract\n\nHard Rules go here.\n")

# The framing matters: the contract's own text calls the cwd a scratch
# workspace, which is false for exactly these sessions, and system-prompt text
# outranks anything the agent reads off disk.
_framed = _gateway_contract(str(_repo), str(_workdir_root), str(_contract))
if str(_repo) not in _framed.split("\n\n", 1)[0]:
    fails.append("framing preamble does not name the actual cwd")
if "EXCEPT" not in _framed.split("\n\n", 1)[0]:
    fails.append("framing preamble does not carve out the stale cwd claims")

# Scratch workdir: ~/hub/CLAUDE.md already points at the contract, so appending
# it again would duplicate several KB into every single turn.
check("workdir cwd gets nothing",
      _gateway_contract(str(_workdir_root / "chat-abc"), str(_workdir_root), str(_contract)),
      "")
check("workdir root itself gets nothing",
      _gateway_contract(str(_workdir_root), str(_workdir_root), str(_contract)), "")

# Fail open on a bad path rather than taking the turn down — a missing contract
# file must not break chat, but it also must not silently look like success.
check("missing contract file yields nothing",
      _gateway_contract(str(_repo), str(_workdir_root), str(pathlib.Path(_root) / "nope.md")),
      "")
check("empty workdir root disables the exemption",
      sent(_gateway_contract(str(_repo), "", str(_contract))),
      "# Hub contract\n\nHard Rules go here.\n")

# A cwd that merely starts with the same characters is NOT inside the root.
_sibling = pathlib.Path(str(_workdir_root) + "-other")
_sibling.mkdir()
check("prefix-similar sibling path is not inside the root",
      sent(_gateway_contract(str(_sibling), str(_workdir_root), str(_contract))),
      "# Hub contract\n\nHard Rules go here.\n")

# Symlinks: the exemption must survive either side being a link. WORKDIR_ROOT
# defaults under /tmp, which is itself a symlink on macOS, so a startswith
# implementation would send the contract to every scratch session on a default
# install. These cases are what make resolve() load-bearing.
_link_root = pathlib.Path(_root) / "hub-link"
_link_root.symlink_to(_workdir_root)
check("cwd reached through a symlinked root is still inside it",
      _gateway_contract(str(_link_root / "chat-abc"), str(_workdir_root), str(_contract)), "")
check("root given as a symlink still matches a real cwd",
      _gateway_contract(str(_workdir_root / "chat-abc"), str(_link_root), str(_contract)), "")
check("traversal out of the root sends the contract",
      sent(_gateway_contract(str(_workdir_root / ".." / "homelab"), str(_workdir_root), str(_contract))),
      "# Hub contract\n\nHard Rules go here.\n")
check("traversal that stays inside the root sends nothing",
      _gateway_contract(str(_workdir_root / "chat-abc" / ".."), str(_workdir_root), str(_contract)), "")

# Never raise. A valve typo or a binary file must not take the turn down —
# UnicodeDecodeError is a ValueError and expanduser() raises RuntimeError on an
# unknown ~user, so neither is covered by catching OSError alone.
_binary = pathlib.Path(_root) / "binary.md"
_binary.write_bytes(b"\xff\xfe not utf-8")
check("undecodable contract yields nothing instead of raising",
      _gateway_contract(str(_repo), str(_workdir_root), str(_binary)), "")
check("unknown ~user in the path yields nothing instead of raising",
      _gateway_contract(str(_repo), str(_workdir_root), "~nosuchuser42/AGENTS.md"), "")
check("NUL byte in the path yields nothing instead of raising",
      _gateway_contract(str(_repo), str(_workdir_root), "/tmp/\x00bad.md"), "")

if fails:
    print(f"FAIL ({len(fails)})")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("ok — session tests passed")
