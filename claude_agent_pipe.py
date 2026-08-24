"""
title: Claude Code
description: Run Claude Code's agent loop from inside OpenWebUI chats via the Claude Agent SDK.
author: Thomas Friedel
version: 0.1
license: MIT
requirements: claude-agent-sdk>=0.1.60, anthropic>=0.40.0
"""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".xml",
    ".xlsx",
    ".docx",
    ".pptx",
    ".zip",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
# Safety cap to avoid uploading runaway files. Uploaded artifacts are served
# via OpenWebUI's file endpoint, so they don't bloat the chat history even
# when large — this is only a "don't accidentally ship a DVD ISO" guard.
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB

# ── Secret redaction ────────────────────────────────────────────────────────
# Everything this pipe emits is persisted in webui.db chat history and synced
# to mobile clients, so a secret that reaches the stream is a secret on disk
# indefinitely. Redaction lives here — at the pipe's own output boundary —
# rather than in any single tool, because the agent runs under
# bypassPermissions and can read a credential through paths nobody enumerated
# in advance (a `cat`, a `sqlite3 SELECT *`, an env dump, a stack trace).
# Guarding individual tools is opt-in and fails open when a new path appears;
# guarding the boundary fails closed regardless of source.
#
# Patterns are prefix-anchored on purpose: a token's fixed prefix plus a
# length floor is specific enough that false positives are rare, and the
# failure mode of over-matching (a redacted non-secret) is far cheaper than
# under-matching (a rotation).
_SECRET_PATTERNS: List[tuple] = [
    ("anthropic-token", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    # [_\-] included: modern sk-proj- keys contain separators, so a pure
    # alnum floor of 32 never matched them (audit 2026-08-14).
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("slack-token", re.compile(r"xox[baprse]-[A-Za-z0-9\-]{10,}")),
    # xapp- (Slack app-level token) is NOT covered by xox[baprse].
    ("slack-app-token", re.compile(r"xapp-[0-9]-[A-Za-z0-9\-]{10,}")),
    ("google-client-secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("google-refresh-token", re.compile(r"1//0[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # 1Password service-account token (the op-secrets loader's own credential).
    ("onepassword-service-token", re.compile(r"ops_[A-Za-z0-9_\-]{20,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # The BODY is the secret, not the header. Matching only the header emitted
    # «redacted:private-key» followed by the entire key -- worse than a miss,
    # because the logged hit reads as "handled" and suppresses rotation.
    # Full block first so it consumes the body; the bare header stays as a
    # backstop for a block whose END never arrives.
    ("private-key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.S)),
    # NOTE: no bare-header pattern. It would replace the header and strand the
    # body, and would also hide the run from _scrub_open_private_key below,
    # which is what actually handles a block whose END never arrives.
]

# A private key spans many chunks, and its body matches no prefix rule in
# _SECRET_TAIL_RX, so the stream redactor needs an explicit hold-until-END.
_PK_BEGIN_RX = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_PK_END_RX = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
# RSA-4096 armor runs ~3.2KB; past this we stop waiting for END and redact.
_MAX_HELD_KEY = 16384


def _scrub_open_private_key(text: str) -> tuple:
    """Redact a BEGIN-with-no-END run through end of `text`.

    Releasing such a run raw is what let key bodies escape one chunk at a time.
    """
    m = _PK_BEGIN_RX.search(text)
    if not m or _PK_END_RX.search(text, m.end()):
        return text, []
    return text[: m.start()] + "«redacted:private-key»", ["private-key"]

# A secret split across two stream chunks defeats a per-chunk regex, so the
# redactor holds back any tail that could still grow into a match. These
# match a *truncated* secret anchored at end-of-buffer; the held tail is
# released once the next chunk proves it harmless.
_SECRET_TAIL_RX = re.compile(
    r"(?:"
    r"s|sk|sk-|sk-a(?:n(?:t(?:-[A-Za-z0-9_\-]*)?)?)?|sk-(?:proj-?)?[A-Za-z0-9]*"
    r"|x|xo|xox|xox[baprse](?:-[A-Za-z0-9\-]*)?|xa|xap|xapp(?:-[A-Za-z0-9\-]*)?"
    r"|G|GO|GOC|GOCS|GOCSP|GOCSPX(?:-[A-Za-z0-9_\-]*)?"
    r"|1|1/|1//|1//0[A-Za-z0-9_\-]*"
    r"|g|gh|gh[pousr](?:_[A-Za-z0-9]*)?|github(?:_(?:p(?:a(?:t(?:_[A-Za-z0-9_]*)?)?)?)?)?"
    r"|A|AK|AKI|AKIA[0-9A-Z]*|AI|AIz|AIza[0-9A-Za-z_\-]*"
    r"|e|ey|eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*){0,2}"
    r"|o|op|ops(?:_[A-Za-z0-9_\-]*)?"
    r"|-{1,5}(?:BEGIN[A-Z ]*)?"
    r")\Z"
)

# Upper bound on the held-back tail. This was 512, which was wrong for JWTs:
# a Google id_token runs ~1-2KB, so its still-growing prefix blew the cap, took
# the release-raw branch below, and streamed out verbatim -- and because no
# COMPLETE pattern ever matched, hits was empty and even the warning log stayed
# silent. Sized for the longest secret we recognise, not the shortest.
_MAX_HELD_TAIL = 4096


def _redact_secrets(text: str) -> tuple:
    """Replace complete secrets in `text`. Returns (clean_text, hit_labels)."""
    if not text:
        return text, []
    hits: List[str] = []
    for label, rx in _SECRET_PATTERNS:
        text, n = rx.subn(f"«redacted:{label}»", text)
        if n:
            hits.extend([label] * n)
    # Complete blocks are gone by now, so any surviving BEGIN is unterminated:
    # a key still streaming, or output cut off mid-armor. Either way the body
    # after it must not be released. Runs last so it cannot mask a full block.
    text, pk_hits = _scrub_open_private_key(text)
    hits.extend(pk_hits)
    return text, hits


class _StreamRedactor:
    """Chunk-boundary-safe secret scrubber for a single response stream.

    `feed()` returns the portion of the stream that is safe to emit now,
    withholding any trailing fragment that could still become a secret once
    more text arrives. `flush()` releases whatever is left at end of stream.
    """

    def __init__(self) -> None:
        self._held = ""
        self.hits: List[str] = []

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        buf = self._held + chunk
        # An unterminated private key holds from BEGIN: its base64 body matches
        # no prefix rule below, so without this the body streams out in pieces.
        pk = _PK_BEGIN_RX.search(buf)
        if pk and not _PK_END_RX.search(buf, pk.end()):
            if len(buf) - pk.start() <= _MAX_HELD_KEY:
                self._held = buf[pk.start() :]
                buf = buf[: pk.start()]
            else:
                buf, pk_hits = _scrub_open_private_key(buf)
                self.hits.extend(pk_hits)
                self._held = ""
            safe, hits = _redact_secrets(buf)
            self.hits.extend(hits)
            return safe
        # Split off the still-growing tail BEFORE redacting. Redacting first
        # would fire on a partially-arrived secret the moment it cleared the
        # pattern's length floor, releasing the rest of it verbatim as the
        # remaining characters streamed in.
        tail = _SECRET_TAIL_RX.search(buf)
        if tail and len(buf) - tail.start() <= _MAX_HELD_TAIL:
            self._held = buf[tail.start() :]
            buf = buf[: tail.start()]
        elif tail:
            # Over cap. Releasing raw is exactly how an oversized JWT escaped:
            # no complete pattern matches a still-arriving secret, so nothing
            # redacts it and nothing logs it. A run this long that still looks
            # like a secret prefix gets redacted instead -- consistent with the
            # module's rule that over-matching is far cheaper than a rotation.
            buf = buf[: tail.start()] + "«redacted:truncated»"
            self.hits.append("truncated")
            self._held = ""
        else:
            self._held = ""
        safe, hits = _redact_secrets(buf)
        self.hits.extend(hits)
        return safe

    def flush(self) -> str:
        # A stream can end mid-key (truncated output, killed tool). Scrub the
        # open BEGIN run first, or flush would release the held body verbatim.
        pending, pk_hits = _scrub_open_private_key(self._held)
        self.hits.extend(pk_hits)
        out, hits = _redact_secrets(pending)
        self.hits.extend(hits)
        self._held = ""
        return out


def _redact_event(value: Any, hits: List[str]) -> Any:
    """Recursively scrub secrets from an event payload.

    Status descriptions carry tool previews and error text, so they leak the
    same way response text does. Events arrive whole rather than streamed, so
    no held-tail machinery is needed here.
    """
    if isinstance(value, str):
        clean, found = _redact_secrets(value)
        hits.extend(found)
        return clean
    if isinstance(value, dict):
        return {k: _redact_event(v, hits) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_event(v, hits) for v in value]
    return value


def _redacting_emitter(
    emitter: Optional[Callable], hits: List[str]
) -> Optional[Callable]:
    """Wrap an OpenWebUI event emitter so every payload is scrubbed first."""
    if emitter is None:
        return None

    async def _emit(event: Any) -> Any:
        return await emitter(_redact_event(event, hits))

    return _emit


def _message_text(message: Dict[str, Any]) -> str:
    """Plain text of one OpenAI-shape message (string or content-parts list).
    Non-text parts (image_url, …) are skipped."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(t for t in texts if t)
    return ""


# ── Session persistence ─────────────────────────────────────────────────────
# The in-process _chat_sessions map dies on every OWUI restart, pipe
# redeploy, and valve edit — silently downgrading chats to flattened-text
# history replay. These helpers persist session identity as small JSON files
# under WORKDIR_ROOT: a per-chat meta file for keyed callers (native UI) and
# a fingerprint→session store for keyless callers (Conduit and other generic
# OpenAI-API clients, which send no chat_id). Files hold session ids and
# paths only — never credentials.

_FP_STORE_FILE = "_sessions.json"
_FP_STORE_TTL_SECONDS = 30 * 24 * 3600  # prune keyless entries after 30 days


def _strip_latest_user(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The conversation prefix used for fingerprint lookup: everything
    before the latest user message (the prompt being answered this turn)."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return list(messages[:i])
    return list(messages)


def _history_fingerprint(messages: List[Dict[str, Any]]) -> str:
    """Stable identity for a conversation prefix. Normalizes to
    (role, stripped text) pairs of user/assistant messages so cosmetic
    differences (content-parts vs plain string, surrounding whitespace)
    don't change the hash. An edited or regenerated message legitimately
    changes it — that chat gets one cold replay and re-registers."""
    parts: List[str] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(m).strip()
        if text:
            parts.append(f"{role}\n{text}")
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# The hash of an all-empty normalization (no messages, or none that survive
# normalization). Every brand-new keyless chat's first-turn lookup collapses
# to this same key, so a turn must never be allowed to *register* under it —
# doing so would let one chat's fingerprint store entry answer for every
# other unrelated new chat.
_EMPTY_FP = _history_fingerprint([])


def _session_meta_path(workdir_root: str, chat_id: str) -> Path:
    """Lives in a shared `.sessions/` dot-directory directly under
    WORKDIR_ROOT — never inside a chat's own workdir (or, in PR2, a repo
    checkout's cwd). `_snapshot_artifacts`/`_inline_new_artifacts` walk the
    chat workdir looking for downloadable files, and `.json` is one of the
    extensions they pick up; a meta file living alongside a chat's work
    would get uploaded as a `.session.json` artifact and linked with a 📎
    every keyed turn."""
    return Path(workdir_root) / ".sessions" / f"{chat_id}.json"


def _load_session_meta(workdir_root: str, chat_id: str) -> Dict[str, Any]:
    """Per-chat session metadata ({"session_id": …, "cwd": …}). Missing or
    corrupt → {}: the turn just falls back to history replay."""
    try:
        data = json.loads(
            _session_meta_path(workdir_root, chat_id).read_text("utf-8")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session_meta(
    workdir_root: str, chat_id: str, meta: Dict[str, Any]
) -> None:
    """Atomic write (tmp + replace) so a crash mid-write can't leave a
    half-JSON file that would poison every later turn."""
    path = _session_meta_path(workdir_root, chat_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta), "utf-8")
        tmp.replace(path)
    except OSError:
        logging.getLogger(__name__).warning("could not persist session meta for %s", chat_id, exc_info=True)


def _fp_store_path(workdir_root: str) -> Path:
    return Path(workdir_root) / _FP_STORE_FILE


def _load_fp_store(workdir_root: str) -> Dict[str, Dict[str, Any]]:
    """fingerprint → {"session_id", "workdir", "cwd", "ts"}. Missing or
    corrupt → {}. Entries older than the TTL are pruned on load."""
    try:
        data = json.loads(_fp_store_path(workdir_root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - _FP_STORE_TTL_SECONDS
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, dict) and v.get("ts", 0) >= cutoff
    }


def _save_fp_store(workdir_root: str, store: Dict[str, Dict[str, Any]]) -> None:
    path = _fp_store_path(workdir_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store), "utf-8")
        tmp.replace(path)
    except OSError:
        logging.getLogger(__name__).warning("could not persist keyless session store", exc_info=True)


# ── Repo-aware workdir ──────────────────────────────────────────────────────

_REPO_PREFIX_RX = re.compile(r"^#repo:([A-Za-z0-9][\w-]*)\s*", re.ASCII)


def _parse_repo_map(raw: str) -> Dict[str, str]:
    """REPO_MAP valve: comma-separated name=path allowlist. Malformed
    entries are dropped — a typo must not widen where chats can run."""
    out: Dict[str, str] = {}
    for pair in raw.split(","):
        name, sep, path = pair.partition("=")
        name, path = name.strip(), path.strip()
        if sep and name and path:
            out[name] = path
    return out


def _extract_repo_prefix(prompt: str) -> Tuple[Optional[str], str]:
    """`#repo:<name>` at the very start of a message selects the chat's
    working repo. Returns (name or None, prompt with the prefix removed)."""
    m = _REPO_PREFIX_RX.match(prompt.lstrip())
    if not m:
        return None, prompt
    return m.group(1), prompt.lstrip()[m.end():]


def _model_from_requested(requested: str, default: str) -> str:
    """Map an invoked OpenWebUI model id back to a Claude model ID. Extra
    picker entries carry the model in their suffix ("…claude-code-<model>");
    everything else falls through to the default (MODEL valve)."""
    marker = "claude-code-"
    if marker in requested:
        return requested.split(marker, 1)[1]
    return default


def _gateway_contract(cwd: str, workdir_root: str, contract_path: str) -> str:
    """The gateway contract to append when the agent's cwd won't supply it.

    A `#repo:` session runs with cwd set to the mapped repo, so the SDK loads
    THAT repo's CLAUDE.md and never reads `hub/AGENTS.md`. On 2026-07-27 that
    meant `#repo:homelab` sessions ran with no response contract, no async
    rules, and no Hard Rules — under bypassPermissions — because the repo had
    no root instruction file at all. Adding one per repo fixes the repos we
    remember; this fixes the class, including the next REPO_MAP entry someone
    adds without one.

    Returns "" when cwd is inside WORKDIR_ROOT: those sessions already load
    `<WORKDIR_ROOT>/CLAUDE.md`, and appending several KB to every turn on top
    of that is pure waste. Pass workdir_root="" to disable that exemption —
    the caller does exactly that when SETTING_SOURCES would stop the agent
    loading anything from its cwd at all.

    Never raises. Every failure returns "" or falls through to sending the
    contract; a bad path or an undecodable file must not take a chat turn
    down. Note UnicodeDecodeError is a ValueError, and expanduser() raises
    RuntimeError on an unknown ~user — both reachable from a valve typo.
    """
    try:
        cwd_path = Path(cwd).resolve()
    except (OSError, ValueError, RuntimeError):
        return ""
    if workdir_root:
        try:
            root = Path(workdir_root).resolve()
            # relative_to on RESOLVED paths, not startswith: "<root>-other"
            # shares a prefix with "<root>" but is a different directory, and
            # either side may be a symlink (WORKDIR_ROOT defaults under /tmp,
            # which is itself a symlink on macOS).
            cwd_path.relative_to(root)
            return ""
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        text = Path(contract_path).expanduser().read_text(encoding="utf-8")
    except (OSError, ValueError, RuntimeError):
        return ""
    # Frame it. The contract's own opening describes the cwd as a per-chat
    # scratch workspace, which is false for exactly the sessions this append
    # exists to serve — and system-prompt text outranks anything the agent
    # reads from disk, so an unframed copy would actively mislead.
    return (
        f"Gateway contract, injected because this session is rooted at {cwd} "
        "rather than a per-chat scratch workspace. Everything below applies, "
        "EXCEPT statements about your working directory being a scratch "
        "workspace or about which instruction files you load automatically — "
        "your cwd is a real working tree, and this contract is being supplied "
        "to you directly.\n\n" + text
    )


def _agent_env(chat_id: Optional[str]) -> Dict[str, str]:
    """Environment additions for the agent's subprocess.

    The agent has no other way to learn which chat it is serving: its cwd is a
    scratch workdir (or, in a #repo: session, a repo that says nothing about
    the chat), so `hub-async.sh` used to be told to guess the id from
    `basename $PWD`. Passing it explicitly retires that guess.

    Set only when an id exists — never as an empty string. `hub-async.sh`
    treats "unset" as "this session cannot receive an async result", which is
    also how nested async jobs are blocked: `hub-async-worker.sh` strips
    HUB_CHAT_ID from the environment it hands `claude -p` (`env -u`), so a
    `submit` inside a running job dies for want of a chat. Note that stripping
    is required, not incidental — the worker inherits this variable from the
    submitting agent through `hub-async.sh`'s `os.execv`.
    """
    return {"HUB_CHAT_ID": chat_id} if chat_id else {}


def _fmt_tokens(n: int) -> str:
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1_000)}k"
    s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{s}M"


def _context_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Context size implied by one API call's usage: the input-side fields
    are the entire prompt (system + history + tool results), and adding
    output_tokens gives the session context after the reply."""
    if not usage:
        return 0
    return sum(
        int(usage.get(k) or 0)
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )


def _context_status(usage: Optional[Dict[str, Any]], window: int) -> str:
    """Render 'context 74k/200k (37%)', or '' when unknown/disabled."""
    used = _context_tokens(usage)
    if not used or window <= 0:
        return ""
    pct = round(100 * used / window)
    return f"context {_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct}%)"


def _owui_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """OWUI-normalized usage dict from one API call's usage block."""
    out_tok = int(usage.get("output_tokens") or 0)
    total = _context_tokens(usage)
    return {
        "prompt_tokens": total - out_tok,
        "completion_tokens": out_tok,
        "total_tokens": total,
        "cache_read_input_tokens": int(
            usage.get("cache_read_input_tokens") or 0
        ),
        "cache_creation_input_tokens": int(
            usage.get("cache_creation_input_tokens") or 0
        ),
    }


def _fmt_duration(ms: int) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_PREFIX_RX = re.compile(
    r"/effort[:= ]\s*(low|medium|high|xhigh|max)\b", re.IGNORECASE
)


def _extract_effort_prefix(prompt: str) -> Tuple[Optional[str], str]:
    """`/effort <level>` at the very start of a message overrides the EFFORT
    valve for that turn. Returns (level or None, prompt without the prefix)."""
    stripped = prompt.lstrip()
    m = _EFFORT_PREFIX_RX.match(stripped)
    if not m:
        return None, prompt
    return m.group(1).lower(), stripped[m.end():].lstrip()


from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIJSONDecodeError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

log = logging.getLogger(__name__)

# OpenWebUI calls pipe() fresh for each chat turn. We keep a chat_id -> session_id
# map in-process so follow-up turns resume the same Claude Code session.
_chat_sessions: Dict[str, str] = {}


_TOOL_PREVIEW_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Task": "description",
}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    if key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    # Collapse to one line — multi-line values break inline-code spans and leak
    # "# python comments" into markdown as H1 headings.
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated + (" …" if "\n" in raw and not truncated.endswith("…") else "")


_FENCE_LANG_PER_TOOL = {
    "Bash": "bash",
    "Glob": "text",
    "Grep": "text",
    "WebSearch": "text",
    "WebFetch": "text",
}


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    """Full tool invocation as fenced code block(s). If the tool has a known
    primary field (Bash→command, Read→file_path, …), render that with the
    right syntax highlight and append any other fields as a small JSON block.
    Tools with no known primary render as a single JSON block.
    """
    if not tool_input:
        return "```\n(no input)\n```"
    primary = _TOOL_PREVIEW_FIELDS.get(name)
    if primary and primary in tool_input:
        lang = _FENCE_LANG_PER_TOOL.get(name, "text")
        parts = [f"```{lang}\n{tool_input[primary]}\n```"]
        others = {k: v for k, v in tool_input.items() if k != primary}
        if others:
            parts.append(
                f"```json\n{json.dumps(others, indent=2, ensure_ascii=False)}\n```"
            )
        return "\n\n".join(parts)
    return f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"


def _format_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(text if isinstance(text, str) else repr(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _iter_artifact_files(scan_dirs: List[Path]) -> "list[Path]":
    """Yield image/document artifacts from each scan dir. Workdir is searched
    recursively; other dirs (typically /tmp) are searched non-recursively to
    avoid picking up unrelated files under nested system caches.

    Non-workdir dirs match images only: the /tmp scan exists because agents
    save matplotlib/PIL output there from habit, but matching documents there
    too published every fetched-page scratch dump (.html/.txt) as an
    artifact. Document deliverables belong in the workdir, which keeps the
    full extension set."""
    seen: List[Path] = []
    for idx, root in enumerate(scan_dirs):
        if not root.exists():
            continue
        iterator = root.rglob("*") if idx == 0 else root.iterdir()
        extensions = _ARTIFACT_EXTENSIONS if idx == 0 else _IMAGE_EXTENSIONS
        for path in iterator:
            if path.is_file() and path.suffix.lower() in extensions:
                seen.append(path)
    return seen


def _snapshot_artifacts(scan_dirs: List[Path]) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in _iter_artifact_files(scan_dirs):
        try:
            snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return snapshot


def _inline_new_artifacts(
    scan_dirs: List[Path],
    before: Dict[str, int],
    user_id: Optional[str],
) -> List[str]:
    """Upload artifacts new or modified since `before` to OpenWebUI's file
    store, and return markdown referencing the served URLs.

    Why not base64 data URIs: large blobs (multi-MB PDFs) encoded as
    `data:application/pdf;base64,…` in a markdown link cause browsers to spam
    the address bar and stall when clicked. They'd also persist in chat
    history, bloating the DB on every turn.

    URL shape: `/api/v1/files/{id}/content` for every artifact.
      - Images: loaded by the markdown `<img>` tag → display inline.
      - PDFs: the route emits `Content-Disposition: inline` → browser opens
        them in its native PDF viewer (new tab).
      - Everything else: the route falls back to `attachment`, so clicking
        triggers a download (fine for CSV/XLSX/ZIP — they have no sensible
        inline view anyway).
    Deliberately avoids `/content/{filename}`, which hard-codes `attachment`
    for every type and so forces a download even for PDFs.
    """
    if not user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]
    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    chunks: List[str] = []
    doc_links: List[str] = []
    for path in sorted(_iter_artifact_files(scan_dirs)):
        try:
            mtime = path.stat().st_mtime_ns
            size = path.stat().st_size
        except OSError:
            continue
        if before.get(str(path)) == mtime:
            continue  # untouched
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: {size // 1024 // 1024} MiB exceeds {_MAX_ARTIFACT_BYTES // 1024 // 1024} MiB limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(path.name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )

        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{path.name}"
        try:
            with path.open("rb") as handle:
                contents, storage_path = Storage.upload_file(
                    handle,
                    storage_filename,
                    {
                        "OpenWebUI-User-Id": user_id,
                        "OpenWebUI-File-Id": file_id,
                    },
                )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", path)
            chunks.append(f"\n\n_(Failed to save {path.name}: {exc})_\n")
            continue

        try:
            Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id,
                    filename=path.name,
                    path=storage_path,
                    data={},
                    meta={
                        "name": path.name,
                        "content_type": mime,
                        "size": len(contents),
                    },
                ),
            )
        except Exception as exc:
            log.exception("Artifact DB row failed: %s", path)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{path.name}](/api/v1/files/{file_id}/content)\n")
        else:
            kib = size // 1024
            doc_links.append(
                f"[{path.name}](/api/v1/files/{file_id}/content) ({kib} KiB)"
            )
    # One compact paragraph for all document links instead of a block per
    # file: multi-file turns were eating a screenful of chat. Not a <details>
    # toggle — Conduit renders raw HTML literally.
    if doc_links:
        label = "file" if len(doc_links) == 1 else f"{len(doc_links)} files"
        chunks.append(f"\n\n📎 {label}: " + " · ".join(doc_links) + "\n")
    return chunks


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    """Collect `role=system` content from body.messages. OpenWebUI merges the
    Workspace Model's configured system prompt into messages[0] before the
    pipe is called (payload.py:apply_system_prompt_to_body)."""
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


def _knowledge_collections(
    metadata: Optional[Dict[str, Any]],
    files: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Extract attached knowledge collections.

    Priority order:
    1. `metadata["model"]["info"]["meta"]["knowledge"]` — the authoritative
       Workspace Model config, populated regardless of the `function_calling`
       gate. This is the only source that works when the Workspace Model
       sets `function_calling=native` (which disables OpenWebUI's auto-RAG).
    2. `files` (__files__) — populated by the middleware's auto-RAG branch
       when `function_calling != "native"`. Useful for non-native chats or
       as a fallback.
    """
    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(coll: Any, name: Any) -> None:
        if not coll:
            return
        cid = str(coll)
        if cid in seen:
            return
        seen.add(cid)
        out.append({"id": cid, "name": str(name or cid)})

    def _consume(item: Dict[str, Any]) -> None:
        """Mirror OpenWebUI's vector-collection naming
        (retrieval/utils.py:get_sources_from_items)."""
        name = item.get("name")
        # Old-style: explicit collection_name(s). Legacy KBs with multiple colls.
        if item.get("collection_name"):
            _add(item["collection_name"], name)
            return
        if item.get("collection_names"):
            for coll in item["collection_names"]:
                _add(coll, name)
            return
        # Modern: collection name derived from the knowledge row's id.
        item_type = item.get("type")
        if item_type == "collection" and item.get("id"):
            _add(item["id"], name)
        elif item_type == "file" and item.get("id"):
            # legacy single-file entries skip the prefix; modern ones prepend "file-".
            coll = item["id"] if item.get("legacy") else f"file-{item['id']}"
            _add(coll, name)

    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if isinstance(item, dict):
            _consume(item)

    for f in files or []:
        if isinstance(f, dict):
            _consume(f)

    return out


def _knowledge_row_ids(metadata: Optional[Dict[str, Any]]) -> List[str]:
    """Return knowledge-table row ids for the attached Workspace-Model KBs.
    These are the IDs used to look up files via Knowledges.get_files_by_id().
    Only populated for `type: "collection"` entries — single-file entries
    aren't exposed to list/read/grep (search still works for those)."""
    ids: List[str] = []
    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if (
            isinstance(item, dict)
            and item.get("type") == "collection"
            and item.get("id")
        ):
            ids.append(str(item["id"]))
    return ids


def _build_kb_mcp_server(
    knowledge: List[Dict[str, str]],
    knowledge_row_ids: Optional[List[str]] = None,
    user_dict: Optional[Dict[str, Any]] = None,
    event_emitter: Optional[Callable] = None,
):
    """Return (mcp_config, tool_names) for a knowledge-base search tool Claude
    can invoke, or (None, []) if no KBs are attached.

    Result formatting follows OpenWebUI's native RAG shape (<source id=N ...>
    tags + citation event), so Claude's replies render with inline [N]
    citations and populate the sources side-panel. Access control is
    enforced implicitly: the closure captures only the collection_names
    that OpenWebUI's middleware already filtered by the user's grants.
    """
    if not knowledge:
        return None, [], {}

    collection_names = [k["id"] for k in knowledge]
    display = ", ".join(k["name"] for k in knowledge)

    @tool(
        "search_knowledge",
        (
            f"Search the attached knowledge base(s): {display}. "
            "Call whenever you need internal facts, prior guidance, or "
            "documented product/process details. Reformulate and search "
            "multiple times if the first query misses."
        ),
        {"query": str, "top_k": int},
    )
    async def _search(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from open_webui.main import app
            from open_webui.models.users import Users
            from open_webui.retrieval.utils import query_collection
        except Exception as exc:
            return {
                "content": [
                    {"type": "text", "text": f"Knowledge search unavailable: {exc}"}
                ]
            }

        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "content": [
                    {"type": "text", "text": "Empty query — nothing to search."}
                ]
            }
        # Default to OpenWebUI's configured RAG_TOP_K so the tool respects the
        # admin's retrieval setting. Fall back to 5 if unreadable.
        try:
            default_top_k = int(app.state.config.RAG_TOP_K.value)
        except Exception:
            default_top_k = 5
        top_k = int(args.get("top_k") or default_top_k)

        # Resolve a proper UserModel so embedding_function can attribute
        # usage / rate-limits per user (some backends require it).
        user_obj = None
        user_id = (user_dict or {}).get("id")
        if user_id:
            try:
                user_obj = Users.get_user_by_id(user_id)
            except Exception:
                pass

        async def _embed(queries, prefix=None):
            return await app.state.EMBEDDING_FUNCTION(
                queries, prefix=prefix, user=user_obj
            )

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔎 Searching KB: {query[:80]}",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        try:
            results = await query_collection(
                collection_names=collection_names,
                queries=[query],
                embedding_function=_embed,
                k=top_k,
            )
        except Exception as exc:
            log.exception("KB search failed")
            return {"content": [{"type": "text", "text": f"Search failed: {exc}"}]}

        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []

        if not docs:
            return {
                "content": [
                    {"type": "text", "text": f"No passages found for: {query!r}"}
                ]
            }

        # Surface citations in OpenWebUI's sources side-panel.
        # Emit one event per document so each source gets its own filename label
        # (a single event with a shared source.name collapses all sources into
        # one label in the UI).
        if event_emitter:
            dist_iter = dists or [None] * len(metas)
            for doc, meta, dist in zip(docs, metas, dist_iter):
                meta = meta or {}
                source_name = (
                    meta.get("name")
                    or meta.get("source")
                    or meta.get("title")
                    or "unknown"
                )
                try:
                    await event_emitter(
                        {
                            "type": "citation",
                            "data": {
                                "document": [doc],
                                "metadata": [
                                    {
                                        "source": source_name,
                                        "file_id": meta.get("file_id", ""),
                                        "relevance_score": (
                                            round(float(dist), 3)
                                            if dist is not None
                                            else None
                                        ),
                                    }
                                ],
                                "source": {"name": source_name},
                            },
                        }
                    )
                except Exception:
                    pass

        # XML <source> tags = OpenWebUI's native RAG format → renders as [N] citations.
        parts = [f"Found {len(docs)} passage(s) for {query!r}:\n"]
        for i, (doc, meta) in enumerate(zip(docs, metas), 1):
            meta = meta or {}
            name = (
                meta.get("source") or meta.get("name") or meta.get("title") or "unknown"
            )
            parts.append(f'<source id="{i}" name="{name}">{doc}</source>')
        parts.append(
            "\nCite these using [1], [2], … in your response. Do not include the XML tags themselves."
        )
        return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

    # ---------- Agentic helpers: list / read / grep ---------------------------
    # Scoped to `knowledge_row_ids` (type=collection entries only). Single-file
    # KBs (type=file) are searchable but not listable/readable by these tools.
    kb_ids: List[str] = list(knowledge_row_ids or [])

    async def _iter_scoped_files():
        from open_webui.models.knowledge import Knowledges

        for kid in kb_ids:
            try:
                files = Knowledges.get_files_by_id(kid) or []
            except Exception:
                continue
            for f in files:
                yield f

    async def _allowed_file_ids() -> Set[str]:
        return {f.id async for f in _iter_scoped_files()}

    @tool(
        "list_knowledge_documents",
        (
            f"List every document in the attached knowledge base(s): {display}. "
            "Returns file_id, filename, and size for each. Use before "
            "read_knowledge_document or grep_knowledge when you need to know "
            "what's there."
        ),
        {},
    )
    async def _list_docs(args: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
        if not kb_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "No enumerable knowledge collections attached.",
                    }
                ]
            }
        lines = [f"Documents in {display}:"]
        count = 0
        async for f in _iter_scoped_files():
            content = (f.data or {}).get("content", "") or ""
            lines.append(
                f"- file_id={f.id} · {f.filename} · {len(content) // 1024} KiB · {len(content)} chars"
            )
            count += 1
        if count == 0:
            lines.append("(no files found)")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "read_knowledge_document",
        (
            "Read the full content of a knowledge document (or a character "
            "range of it) by file_id. Use to zoom into a doc that "
            "search_knowledge found, or read it top-to-bottom if small. "
            "Omit start_char/end_char to read the whole file. "
            "Each call caps at 40 000 chars; page using start_char/end_char "
            "if the file is larger."
        ),
        {"file_id": str, "start_char": int, "end_char": int},
    )
    async def _read_doc(args: Dict[str, Any]) -> Dict[str, Any]:
        from open_webui.models.files import Files

        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return {"content": [{"type": "text", "text": "file_id is required."}]}
        allowed = await _allowed_file_ids()
        if file_id not in allowed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"file_id {file_id!r} is not in the attached knowledge base(s).",
                    }
                ]
            }

        try:
            file_obj = Files.get_file_by_id(file_id)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Lookup failed: {exc}"}]}
        if file_obj is None:
            return {"content": [{"type": "text", "text": "File not found."}]}

        content = (file_obj.data or {}).get("content", "") or ""
        total = len(content)
        start = max(0, int(args.get("start_char") or 0))
        raw_end = args.get("end_char")
        end = total if raw_end in (None, 0) else min(total, max(start, int(raw_end)))
        # Hard cap per call to avoid flooding the context window.
        MAX_CHARS = 40_000
        if end - start > MAX_CHARS:
            end = start + MAX_CHARS
        slice_ = content[start:end]
        header = (
            f"# {file_obj.filename}\n"
            f"_chars {start}..{end} of {total}"
            f"{' (truncated — call again with a higher start_char to continue)' if end < total else ''}_\n\n"
        )
        return {"content": [{"type": "text", "text": header + slice_}]}

    @tool(
        "grep_knowledge",
        (
            "Regex/substring search across knowledge documents. Runs against "
            "pre-extracted plain text in the database (no PDF re-parsing), "
            "so it's fast. Use for exact keywords, product codes, "
            "acronyms where vector search struggles.\n\n"
            "- `pattern` (required): regex or literal string\n"
            "- `file_id` (optional): if set, grep only that file; omit or "
            "leave empty to grep the whole knowledge base\n"
            "- `case_insensitive` (default true)\n"
            "- `max_matches` (default 30)\n\n"
            "Returns each hit with 80 chars of surrounding context, the "
            "source filename, and the char offset — use that offset with "
            "read_knowledge_document to fetch more context."
        ),
        {
            "pattern": str,
            "file_id": str,
            "case_insensitive": bool,
            "max_matches": int,
        },
    )
    async def _grep(args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return {"content": [{"type": "text", "text": "pattern is required."}]}
        flags = re.IGNORECASE if args.get("case_insensitive", True) else 0
        max_matches = int(args.get("max_matches") or 30)
        file_id_filter = str(args.get("file_id") or "").strip() or None
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"content": [{"type": "text", "text": f"Invalid regex: {exc}"}]}

        if file_id_filter:
            allowed = await _allowed_file_ids()
            if file_id_filter not in allowed:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"file_id {file_id_filter!r} is not in the attached knowledge base(s).",
                        }
                    ]
                }

        hits: List[str] = []
        files_scanned = 0
        async for f in _iter_scoped_files():
            if file_id_filter and f.id != file_id_filter:
                continue
            files_scanned += 1
            content = (f.data or {}).get("content", "") or ""
            for m in compiled.finditer(content):
                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(content), m.end() + 80)
                ctx = content[ctx_start:ctx_end].replace("\n", " ")
                hits.append(
                    f"- **{f.filename}** (file_id={f.id}) @ char {m.start()}:\n  …{ctx}…"
                )
                if len(hits) >= max_matches:
                    break
            if len(hits) >= max_matches:
                break

        scope = (
            f"1 file ({file_id_filter})"
            if file_id_filter
            else f"{files_scanned} file(s)"
        )
        if not hits:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"No matches for /{pattern}/ across {scope}.",
                    }
                ]
            }
        header = (
            f"Found {len(hits)} match(es) for /{pattern}/ across {scope}"
            f"{' (capped at max_matches)' if len(hits) >= max_matches else ''}:\n"
        )
        return {"content": [{"type": "text", "text": header + "\n".join(hits)}]}

    tools_list = [_search]
    tool_names = ["mcp__helm-kb__search_knowledge"]
    tools_by_name: Dict[str, Any] = {"search_knowledge": _search}
    if kb_ids:
        tools_list.extend([_list_docs, _read_doc, _grep])
        tool_names.extend(
            [
                "mcp__helm-kb__list_knowledge_documents",
                "mcp__helm-kb__read_knowledge_document",
                "mcp__helm-kb__grep_knowledge",
            ]
        )
        tools_by_name.update(
            {
                "list_knowledge_documents": _list_docs,
                "read_knowledge_document": _read_doc,
                "grep_knowledge": _grep,
            }
        )

    server = create_sdk_mcp_server("helm-kb", "0.1", tools=tools_list)
    return server, tool_names, tools_by_name


def _anthropic_kb_tool_defs(
    knowledge: List[Dict[str, str]], has_kb_ids: bool
) -> List[Dict[str, Any]]:
    """JSON-Schema tool definitions for the Anthropic Messages API. Kept
    in sync with `_build_kb_mcp_server`'s tools (same names & input fields)
    so Claude sees an identical toolbox regardless of which path is active."""
    if not knowledge:
        return []
    display = ", ".join(k["name"] for k in knowledge)
    defs: List[Dict[str, Any]] = [
        {
            "name": "search_knowledge",
            "description": (
                f"Search the attached knowledge base(s): {display}. "
                "Call whenever you need internal facts, prior guidance, or "
                "documented product/process details. Reformulate and search "
                "multiple times if the first query misses."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        }
    ]
    if has_kb_ids:
        defs.extend(
            [
                {
                    "name": "list_knowledge_documents",
                    "description": (
                        f"List every document in {display}. "
                        "Returns file_id, filename, and size for each."
                    ),
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "read_knowledge_document",
                    "description": (
                        "Read full content (or a character range) of a "
                        "knowledge document by file_id. Omit start_char / "
                        "end_char to read the whole file. Caps at 40 000 "
                        "chars per call — page using start_char to continue."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string"},
                            "start_char": {"type": "integer"},
                            "end_char": {"type": "integer"},
                        },
                        "required": ["file_id"],
                    },
                },
                {
                    "name": "grep_knowledge",
                    "description": (
                        "Regex/substring search across knowledge documents. "
                        "Fast (runs on pre-extracted text in the DB). Use for "
                        "exact keywords, product codes, acronyms where vector "
                        "search struggles."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "file_id": {"type": "string"},
                            "case_insensitive": {"type": "boolean"},
                            "max_matches": {"type": "integer"},
                        },
                        "required": ["pattern"],
                    },
                },
            ]
        )
    return defs


async def _dispatch_kb_tool(
    name: str,
    args: Dict[str, Any],
    tools_by_name: Dict[str, Any],
) -> str:
    """Call a KB tool by name and unwrap its MCP-format result into plain
    text suitable for returning as an Anthropic tool_result content block."""
    sdk_tool = tools_by_name.get(name)
    if sdk_tool is None:
        return f"Unknown tool: {name}"
    try:
        result = await sdk_tool.handler(args or {})
    except Exception as exc:
        log.exception("KB tool %s failed", name)
        return f"Tool {name} failed: {exc}"
    content = result.get("content") or []
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return first.get("text", "")
    return ""


# ---------------------------------------------------------------------------
# Fast-path gate: decide whether a turn needs the full Claude Code agent loop
# (CLI + MCP + tool-use deliberation, ~3–5 s overhead) or can ride the cheap
# Messages-API path (~300 ms – 2 s). Same model on both sides — the split is
# about mode, not model.
# ---------------------------------------------------------------------------

_AGENT_PATTERN = re.compile(
    r"\b("
    r"plot|chart|graph|pdf|"
    r"create\s+(a\s+)?file|save\s+(to\s+|as\s+)?(a\s+)?file|"
    r"generate\s+(a\s+)?(pdf|file|chart|plot|image|report)|"
    r"run\s+(this\s+)?(code|script|command|bash|python)|"
    r"execute\s+(code|script|this|the)|"
    r"download|fetch\s+(from|url|the\s+url)|"
    r"analyze\s+(the\s+|this\s+)?(file|doc|document|csv|spreadsheet|data)|"
    r"read\s+(the\s+|this\s+)?(file|doc|document)|"
    r"write\s+(to\s+)?(a\s+)?file|edit\s+\S+\.\w+|"
    r"make\s+(a\s+|me\s+a\s+)?(plot|chart|pdf|graph|visualization|viz)"
    r")\b",
    re.IGNORECASE,
)

# Mentioning a file extension is a strong "the user has / wants a file" signal.
_FILE_EXT_PATTERN = re.compile(
    r"\.(pdf|csv|tsv|xlsx|xls|docx|pptx|png|jpe?g|svg|html?|json|md|ipynb|zip|tar\.gz)\b",
    re.IGNORECASE,
)

_MODE_PREFIXES = ("/agent", "/fast")


def _strip_mode_prefix(prompt: str) -> str:
    stripped = prompt.lstrip()
    for tag in _MODE_PREFIXES:
        if stripped.startswith(tag):
            return stripped[len(tag) :].lstrip()
    return prompt


_VALID_SETTING_SOURCES = ("user", "project", "local")


def _parse_setting_sources(raw: str) -> List[str]:
    """Parse the SETTING_SOURCES valve into a list the SDK accepts.

    Empty / unset → `[]` (no filesystem settings loaded; clean baseline).
    Unknown tokens are dropped so a typo can't silently widen inheritance.
    """
    return [
        tok
        for tok in (t.strip().lower() for t in raw.split(","))
        if tok in _VALID_SETTING_SOURCES
    ]


def _needs_agent(prompt: str, files: Optional[List[Any]]) -> bool:
    """Route-per-turn heuristic. `/agent` / `/fast` prefixes are explicit
    overrides. Attachments force agent mode (the model should be able to
    read them). Otherwise: look for keywords and file-extension mentions."""
    if not prompt:
        return False
    stripped = prompt.lstrip()
    if stripped.startswith("/agent"):
        return True
    if stripped.startswith("/fast"):
        return False
    if files:
        return True
    if _AGENT_PATTERN.search(stripped):
        return True
    if _FILE_EXT_PATTERN.search(stripped):
        return True
    return False


def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if text:
            return text
    return ""


_ATTACHMENT_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
_MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB


def _save_image_attachments(body: Dict[str, Any], workdir: Path) -> List[str]:
    """Write image_url content parts of the LATEST user message to files
    under <workdir>/attachments/ so the SDK subprocess (which has filesystem
    access but receives no image blocks from this pipe) can Read them.

    Only the latest user message is scanned — historical image parts are
    deliberately not re-written on transcript replays. Returns one
    prompt-note line per attachment (the saved path, or a skip note).

    data: URIs are decoded in-process; plain http(s) URLs are downloaded
    best-effort (5s timeout, ≤10 MiB) and skipped with a note on failure."""
    messages = body.get("messages") or []
    latest: Optional[Dict[str, Any]] = None
    for message in reversed(messages):
        if message.get("role") == "user":
            latest = message
            break
    content = (latest or {}).get("content")
    if not isinstance(content, list):
        return []
    notes: List[str] = []
    counter = 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        url = ((part.get("image_url") or {}).get("url") or "").strip()
        data: Optional[bytes] = None
        ext = ".png"
        if url.startswith("data:"):
            try:
                header, b64 = url.split(",", 1)
                mime = header[5:].split(";", 1)[0].lower()
                ext = (
                    _ATTACHMENT_MIME_EXT.get(mime)
                    or mimetypes.guess_extension(mime)
                    or ".png"
                )
                data = base64.b64decode(b64)
            except Exception as exc:
                notes.append(f"[Attached image could not be decoded ({exc}) — skipped]")
                continue
        elif url.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "openwebui-claude-pipe"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read(_MAX_IMAGE_ATTACHMENT_BYTES + 1)
                    if len(data) > _MAX_IMAGE_ATTACHMENT_BYTES:
                        raise ValueError("exceeds 10 MiB limit")
                    mime = (resp.headers.get_content_type() or "").lower()
                    ext = (
                        _ATTACHMENT_MIME_EXT.get(mime)
                        or mimetypes.guess_extension(mime)
                        or ".png"
                    )
            except Exception as exc:
                notes.append(
                    f"[Attached image at {url[:100]} could not be downloaded "
                    f"({exc}) — skipped]"
                )
                continue
        else:
            continue
        if not data:
            notes.append("[Attached image was empty — skipped]")
            continue
        if len(data) > _MAX_IMAGE_ATTACHMENT_BYTES:
            notes.append(
                f"[Attached image skipped: {len(data) // (1024 * 1024)} MiB "
                f"exceeds {_MAX_IMAGE_ATTACHMENT_BYTES // (1024 * 1024)} MiB limit]"
            )
            continue
        counter += 1
        attach_dir = workdir / "attachments"
        try:
            attach_dir.mkdir(parents=True, exist_ok=True)
            path = attach_dir / f"{stamp}-{counter}{ext}"
            path.write_bytes(data)
        except OSError as exc:
            notes.append(f"[Attached image could not be saved ({exc}) — skipped]")
            continue
        notes.append(f"[Attached image saved to: {path} — use Read to view it]")
    return notes


_TRANSCRIPT_MAX_MESSAGES = 30
_TRANSCRIPT_MAX_CHARS = 24_000


def _render_transcript(body: Dict[str, Any], latest_prompt: str) -> str:
    """Render prior turns as a compact User:/Assistant: transcript ahead of
    the final ask. Used whenever there is no live SDK session to resume:
    keyless (stateless) callers on every call, and keyed chats on a cold
    start (e.g. after an OWUI restart emptied the in-process session map).
    History is capped at _TRANSCRIPT_MAX_MESSAGES messages and
    _TRANSCRIPT_MAX_CHARS chars (newest kept). System messages are excluded
    here — they already flow into options.system_prompt via
    _extract_system_prompt()."""
    messages = body.get("messages") or []
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    last_user_idx = None
    for i in range(len(convo) - 1, -1, -1):
        if convo[i].get("role") == "user":
            last_user_idx = i
            break
    history = convo[:last_user_idx] if last_user_idx is not None else []
    lines: List[str] = []
    for m in history[-_TRANSCRIPT_MAX_MESSAGES:]:
        text = _message_text(m).strip()
        if text:
            role = "User" if m.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {text}")
    if not lines:
        return latest_prompt
    transcript = "\n\n".join(lines)
    if len(transcript) > _TRANSCRIPT_MAX_CHARS:
        transcript = "…" + transcript[-_TRANSCRIPT_MAX_CHARS:]
    return (
        "<conversation_history>\n"
        "Prior turns of this conversation (no live session was available to "
        "resume, so they are replayed here for context):\n\n"
        f"{transcript}\n"
        "</conversation_history>\n\n"
        f"{latest_prompt}"
    )


class Pipe:
    class Valves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Anthropic API key (pay-per-token). Leave empty to use the subscription OAuth token or inherit from the backend env.",
        )
        CLAUDE_CODE_OAUTH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived Claude Pro/Max/Team OAuth token generated by "
                "`claude setup-token` on a machine with a browser. When set, "
                "bills against your subscription (not the API). Takes priority "
                "over ANTHROPIC_API_KEY — the key gets unset so it can't "
                "override. Anthropic's terms: use your own subscription only, "
                "don't re-offer subscription auth to end users."
            ),
        )
        MODEL: str = Field(
            default="claude-haiku-4-5",
            description="Claude model ID (e.g. claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7).",
        )
        MODELS: str = Field(
            default="",
            description=(
                "Comma-separated EXTRA Claude model IDs to expose as separate "
                "picker entries (e.g. claude-haiku-4-5,claude-opus-4-8). Each "
                "gets its own model named by ID; the base entry still uses MODEL."
            ),
        )
        INLINE_TOOL_DETAILS: bool = Field(
            default=True,
            description=(
                "Render tool inputs/thinking as inline <details> blocks in the "
                "message text. The web UI shows these as collapsibles, but some "
                "mobile clients (e.g. Conduit) render the raw tags as text - "
                "set False there; tool activity still appears via status events."
            ),
        )
        PERMISSION_MODE: str = Field(
            default="bypassPermissions",
            description='Permission mode: "default", "acceptEdits", "bypassPermissions", "plan", or "dontAsk".',
        )
        ALLOWED_TOOLS: str = Field(
            default="Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch",
            description="Comma-separated tools auto-approved without prompting.",
        )
        WORKDIR_ROOT: str = Field(
            default="/tmp/claude-agent-pipe",
            description="Root directory for per-chat workspaces. One subdir per chat_id.",
        )
        GATEWAY_CONTRACT_PATH: str = Field(
            default="~/homelab/hub/AGENTS.md",
            description=(
                "Contract appended to the system prompt when a chat is rooted "
                "outside WORKDIR_ROOT (a #repo: session), where the agent would "
                "otherwise load only that repo's CLAUDE.md and never see the "
                "gateway's Hard Rules. Empty disables the append."
            ),
        )
        MAX_TURNS: int = Field(
            default=30,
            description="Maximum agent turns per user message. 0 disables the cap.",
        )
        CONTEXT_WINDOW_TOKENS: int = Field(
            default=200_000,
            description=(
                "FALLBACK context window (tokens) for the post-turn "
                "'Done · context X/Y (N%)' status line, used only when the "
                "SDK's live context query fails (which otherwise supplies the "
                "real per-model window). 0 disables the fallback suffix."
            ),
        )
        EFFORT: str = Field(
            default="",
            description=(
                "Default effort level for agent turns: low|medium|high|xhigh|"
                "max. Empty = SDK default (high). A message starting with "
                "'/effort <level>' overrides it for that turn."
            ),
        )
        TASK_BUDGET_TOKENS: int = Field(
            default=0,
            description=(
                "Per-turn token budget the model is made aware of and paces "
                "itself against (output_config.task_budget). API minimum is "
                "20000 — smaller nonzero values are ignored. 0 disables."
            ),
        )
        FALLBACK_MODEL: str = Field(
            default="",
            description=(
                "Model to retry with if the primary model fails or is "
                "unavailable (SDK fallback_model). Empty disables."
            ),
        )
        MAX_BUFFER_MB: int = Field(
            default=32,
            description=(
                "Maximum size (MiB) of a single stream-json message read back "
                "from the Claude Code CLI. The SDK's own default is 1 MiB, "
                "which a Read of any attachment over ~780 KiB blows past once "
                "base64 expansion (~1.34x) and the JSON envelope are counted — "
                "killing the message reader mid-response. 32 MiB clears the "
                "10 MiB image-attachment cap with room to spare and matches "
                "the Anthropic API's own 32 MB request-body ceiling, so the "
                "framer stops rejecting payloads the API would have accepted. "
                "0 falls back to the SDK default."
            ),
        )
        SETTING_SOURCES: str = Field(
            default="",
            description=(
                "Comma-separated Claude Code setting sources to load: any of "
                '"user", "project", "local". Empty (default) loads NONE — each '
                "chat runs from a clean baseline and does NOT inherit the "
                "backend user's ~/.claude/ or the workdir's .claude/ config. "
                'Add "user" to load ~/.claude/CLAUDE.md and ~/.claude/'
                "settings.json (persistent context for single-user/homelab "
                "setups). WARNING: settings.json can define hooks that execute "
                "code and permission grants — only enable on instances you "
                "trust and control. Avoid on multi-user/public deployments."
            ),
        )
        REPO_MAP: str = Field(
            default="",
            description=(
                "Comma-separated name=path allowlist of repos a chat may run "
                "in, e.g. homelab=/Users/dc-homeserver/homelab,"
                "vault=/Users/dc-homeserver/Obsidian/personal. Start a chat's "
                "FIRST message with #repo:<name> to set its cwd to that path "
                "for the whole chat (loads the repo's CLAUDE.md via the "
                "project setting source). Unknown names error in-chat. Empty "
                "disables the feature. Grants nothing new — bypassPermissions "
                "already reaches these paths; this only controls cwd."
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        entries = [{"id": "claude-code", "name": "Claude Code"}]
        for m in [m.strip() for m in self.valves.MODELS.split(",") if m.strip()]:
            entries.append({"id": f"claude-code-{m}", "name": f"Claude Code ({m})"})
        return entries

    def _resolve_model(self, body: Optional[Dict[str, Any]]) -> str:
        return _model_from_requested(
            str((body or {}).get("model") or ""), self.valves.MODEL
        )

    async def _run_fast(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """Dispatcher. Pick the cheapest available fast path:
        1. API key available → direct Messages API (~300 ms – 2 s).
        2. OAuth token only → "lite agent": ClaudeSDKClient with no tools,
           no MCP, plain system prompt (~2–3 s; CLI cold-start is
           unavoidable because Anthropic's Messages API rejects OAuth tokens
           — the subscription only works via the Claude Code backend).
        """
        has_api_key = bool(
            self.valves.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
        )
        if has_api_key:
            async for chunk in self._run_messages_api(
                body, user_dict, metadata, files, event_emitter
            ):
                yield chunk
        else:
            async for chunk in self._run_lite_agent(
                body, user_dict, metadata, files, event_emitter
            ):
                yield chunk

    async def _run_messages_api(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """Direct Anthropic Messages-API streaming with optional agentic KB
        tool use. No CLI cold start, no Claude Code persona. If a Workspace
        Model has a knowledge base attached, Claude gets the same KB tools
        as the full agent (search / list / read / grep) and can reformulate
        queries in a native tool-use loop."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            yield "_Fast path needs the `anthropic` package — add it to the Function requirements._"
            return

        if self.valves.ANTHROPIC_API_KEY:
            client = AsyncAnthropic(api_key=self.valves.ANTHROPIC_API_KEY)
        else:
            client = AsyncAnthropic()

        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)
        system = "\n\n".join(p for p in system_parts if p.strip()) or None

        # Build KB tools if a workspace knowledge base is attached. Both the
        # MCP server's tool handlers and the Anthropic tool defs come from
        # the same underlying closures (via _build_kb_mcp_server's 3rd
        # return), so Claude sees an identical toolbox in either fast or
        # agent mode.
        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        _, _, kb_tools_by_name = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )
        tool_defs = _anthropic_kb_tool_defs(knowledge, bool(kb_row_ids))

        # Conversation: user/assistant only. Strip any `/agent` or `/fast`
        # prefix from user turns so the model doesn't see it as content.
        messages: List[Dict[str, Any]] = []
        for msg in body.get("messages") or []:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
            if content:
                messages.append({"role": role, "content": content})

        if not messages:
            return

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {"description": "⚡ fast mode", "done": False},
                    }
                )
            except Exception:
                pass

        # Agentic tool-use loop. Stream text as it arrives; if Claude stops
        # with stop_reason="tool_use", execute the tool(s), append the
        # tool_result content blocks, and loop. Cap iterations so a runaway
        # loop can't pin the event loop.
        MAX_TOOL_ROUNDS = 10
        for _round in range(MAX_TOOL_ROUNDS + 1):
            kwargs: Dict[str, Any] = {
                "model": self._resolve_model(body),
                "max_tokens": 4096,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tool_defs:
                kwargs["tools"] = tool_defs

            try:
                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        yield text
                    final = await stream.get_final_message()
            except Exception as exc:
                log.exception("Fast path failed")
                yield (
                    f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"
                )
                return

            if final.stop_reason != "tool_use":
                return  # end_turn — we're done

            # Serialise the assistant message (incl. tool_use blocks) back
            # into the conversation, then resolve each tool call.
            assistant_content: List[Dict[str, Any]] = []
            for block in final.content:
                bt = block.type
                if bt == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif bt == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: List[Dict[str, Any]] = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                preview = _tool_preview(block.name, block.input or {})
                if event_emitter:
                    try:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"🔧 {block.name}"
                                    + (f": {preview}" if preview else ""),
                                    "done": False,
                                },
                            }
                        )
                    except Exception:
                        pass
                # Render a compact tool-use note so the user can see what
                # Claude searched for.
                summary = f"🔧 {block.name}" + (f" · {preview}" if preview else "")
                yield (
                    "\n\n<details>\n"
                    f"<summary>{summary}</summary>\n\n"
                    f"{_tool_input_block(block.name, block.input or {})}\n\n"
                    "</details>\n\n"
                )
                text = await _dispatch_kb_tool(
                    block.name, block.input or {}, kb_tools_by_name
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        yield "\n\n_(Fast-path tool loop cap reached — switch to `/agent` for deeper research.)_\n"

    async def _run_lite_agent(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """OAuth-compatible fast path: ClaudeSDKClient with KB tools only
        (no Bash / Read / Write / Web / files). Pays the CLI cold-start
        (~1–2 s) but skips the big agent persona. When a workspace knowledge
        base is attached, Claude can agentically search / list / read / grep
        it — query reformulation works here too."""
        prompt = _strip_mode_prefix(_extract_latest_user_prompt(body))
        if not prompt:
            return

        # Workspace-Model system + prior conversation (ClaudeSDKClient.query
        # takes a single string, so we pack history into the system).
        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)

        history_lines: List[str] = []
        messages = body.get("messages") or []
        user_turns_remaining = sum(1 for m in messages if m.get("role") == "user")
        consumed = 0
        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
                consumed += 1
                if consumed == user_turns_remaining:
                    continue  # skip the latest — it's the query itself
            if content.strip():
                history_lines.append(f"{role.capitalize()}: {content.strip()}")
        if history_lines:
            system_parts.append(
                "Prior conversation (for context):\n\n" + "\n\n".join(history_lines)
            )

        # KB tools via the existing MCP server. No Bash/Read/Write/etc.
        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        kb_server, kb_tool_names, _kb_dict = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )

        if kb_tool_names:
            system_parts.append(
                "You have read-only knowledge-base tools ("
                + ", ".join(t.rsplit("__", 1)[-1] for t in kb_tool_names)
                + "). Use them when the user asks about facts that might be "
                "in the knowledge base. Reformulate and search multiple times "
                "if the first query misses."
            )
        else:
            system_parts.append("Respond concisely and directly.")
        system_text = "\n\n".join(p for p in system_parts if p.strip())

        options_kwargs: Dict[str, Any] = {
            "model": self._resolve_model(body),
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": kb_tool_names,
            "setting_sources": _parse_setting_sources(self.valves.SETTING_SOURCES),
            # NOTE: unlike the main loop, this path does not append the
            # gateway contract (_gateway_contract) — it has no cwd of its own
            # and is currently unreachable ("Fast path disabled" below). If
            # routing to it is ever restored, add the append here too, or
            # repo-rooted sessions lose the Hard Rules again.
            "system_prompt": system_text,
            "include_partial_messages": True,
        }
        if self.valves.MAX_BUFFER_MB:
            options_kwargs["max_buffer_size"] = (
                self.valves.MAX_BUFFER_MB * 1024 * 1024
            )
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}
        # No bare chat_id local here (unlike the main loop) — this fast path
        # only has `metadata`, which OWUI populates with the chat id under
        # the "chat_id" key.
        chat_id = (metadata or {}).get("chat_id") or None
        agent_env = _agent_env(chat_id)
        if agent_env:
            options_kwargs["env"] = {**options_kwargs.get("env", {}), **agent_env}
        options = ClaudeAgentOptions(**options_kwargs)

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": "⚡ fast mode (OAuth)",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        thinking_buffers: Dict[int, str] = {}
        # Paragraph break between consecutive assistant text blocks — same
        # rationale as the full agent path (see _pipe_stream's text_emitted).
        text_emitted = False
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                            elif block.get("type") == "text" and text_emitted:
                                yield "\n\n"
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    text_emitted = True
                                    yield text
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                text = thinking_buffers.pop(idx).strip()
                                if text and self.valves.INLINE_TOOL_DETAILS:
                                    yield (
                                        "\n\n<details>\n"
                                        "<summary>💭 Thinking</summary>\n\n"
                                        f"{text}\n\n"
                                        "</details>\n\n"
                                    )
                    elif isinstance(message, AssistantMessage):
                        # Tool-use rendering (KB tools only here).
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                summary = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                if self.valves.INLINE_TOOL_DETAILS:
                                    yield (
                                        "\n\n<details>\n"
                                        f"<summary>{summary}</summary>\n\n"
                                        f"{_tool_input_block(block.name, block.input)}\n\n"
                                        "</details>\n\n"
                                    )
                                elif event_emitter:
                                    try:
                                        await event_emitter({"type": "status", "data": {"description": summary, "done": False}})
                                    except Exception:
                                        pass
                    elif isinstance(message, UserMessage):
                        # Surface tool errors (quietly) so the user isn't
                        # confused by Claude retrying silently.
                        content = message.content
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, ToolResultBlock)
                                    and block.is_error
                                ):
                                    err_text = _format_tool_result(block.content)[:400]
                                    if self.valves.INLINE_TOOL_DETAILS:
                                        yield (
                                            "\n\n<details>\n<summary>"
                                            "<sub>⚙️ tool hiccup</sub></summary>\n\n"
                                            f"```\n{err_text}\n```\n\n"
                                            "</details>\n\n"
                                        )
                    elif isinstance(message, ResultMessage):
                        return
        except CLIJSONDecodeError:
            log.exception("Lite-agent fast path exceeded the stream-json buffer")
            yield (
                "\n\n**Response too large to read back.** A single message from "
                "the Claude Code CLI exceeded the "
                f"{self.valves.MAX_BUFFER_MB} MiB `MAX_BUFFER_MB` limit — "
                "usually a very large file or image read into the transcript. "
                "Raise `MAX_BUFFER_MB` in the pipe's valves, or work with a "
                "smaller file.\n"
            )
        except Exception as exc:
            log.exception("Lite-agent fast path failed")
            yield f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Public entrypoint. Scrubs secrets from everything leaving the pipe.

        Kept as a thin wrapper around `_pipe_stream` so the redaction can't be
        bypassed by a future `yield` added inside the agent loop: both the
        response stream and the status/event channel pass through here.
        OpenWebUI injects the dunder arguments by signature inspection, so
        this wrapper must keep the original parameter list verbatim.
        """
        redactor = _StreamRedactor()
        event_hits: List[str] = []
        emitter = _redacting_emitter(__event_emitter__, event_hits)

        turn_info: Dict[str, Any] = {}
        emitted_parts: List[str] = []

        # No try/finally around this loop: flushing from a `finally` would mean
        # yielding during GeneratorExit if the client disconnects mid-stream,
        # which raises RuntimeError. Dropping a held tail on an aborted stream
        # is the safe direction — it withholds text rather than emitting it.
        async for chunk in self._pipe_stream(
            body, __chat_id__, emitter, __files__, __user__, __metadata__,
            turn_info=turn_info,
        ):
            safe = redactor.feed(chunk)
            if safe:
                if not __chat_id__:
                    emitted_parts.append(safe)
                yield safe

        tail = redactor.flush()
        if tail:
            if not __chat_id__:
                emitted_parts.append(tail)
            yield tail

        # Keyless turn finished cleanly → register its post-turn fingerprint
        # so the NEXT call (whose prefix is this history + this reply) resumes
        # warm. If redaction fired, the client-stored text differs from what
        # the agent produced — the next lookup misses and replays once; safe.
        if not __chat_id__ and turn_info.get("session_id"):
            store = _load_fp_store(self.valves.WORKDIR_ROOT)
            fp = _history_fingerprint(
                (body.get("messages") or [])
                + [{"role": "assistant", "content": "".join(emitted_parts)}]
            )
            # An all-empty normalization must never claim the shared
            # empty-prefix hash that every brand-new keyless chat's cold
            # lookup collides on.
            if fp != _EMPTY_FP:
                store[fp] = {
                    "session_id": turn_info["session_id"],
                    "workdir": turn_info["workdir"],
                    "cwd": turn_info.get("cwd") or turn_info["workdir"],
                    "ts": time.time(),
                }
                _save_fp_store(self.valves.WORKDIR_ROOT, store)

        hits = redactor.hits + event_hits
        if hits:
            # Log the kinds, never the values. A hit means something read a
            # credential into the response path — worth a rotation decision
            # even though the value never reached the chat DB.
            log.warning(
                "pipe output redaction fired: %s",
                ", ".join(f"{k}×{hits.count(k)}" for k in sorted(set(hits))),
            )

    async def _pipe_stream(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
        *,
        turn_info: Optional[Dict[str, Any]] = None,
        _no_resume: bool = False,
    ) -> AsyncGenerator[str, None]:
        # Auth selection:
        #   1. If CLAUDE_CODE_OAUTH_TOKEN valve is set → use subscription.
        #      Remove any ANTHROPIC_API_KEY from env because per Claude Code's
        #      precedence order, the API key outranks the OAuth token (docs:
        #      code.claude.com/docs/en/authentication#authentication-precedence)
        #      and would otherwise silently win.
        #   2. Else if ANTHROPIC_API_KEY valve is set → use API.
        #   3. Else → whatever the backend environment already provides.
        if self.valves.CLAUDE_CODE_OAUTH_TOKEN:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.valves.CLAUDE_CODE_OAUTH_TOKEN
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif self.valves.ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = self.valves.ANTHROPIC_API_KEY
        # claude CLI refuses --dangerously-skip-permissions under root unless
        # told it's inside a sandbox. OpenWebUI's backend runs as UID 0.
        os.environ.setdefault("IS_SANDBOX", "1")
        # HUB_CHAT_ID reaches the agent exactly one way: `_agent_env` putting it
        # in this client's `ClaudeAgentOptions.env`. `_agent_env` returning `{}`
        # means "inherit", not "guaranteed unset" — so for a caller with no chat
        # id, an ambient HUB_CHAT_ID in Open WebUI's own environment would flow
        # straight through to the agent, and `hub-async.sh` would deliver that
        # session's async result into someone else's conversation. Clearing it
        # here makes the per-client value the only path there is.
        os.environ.pop("HUB_CHAT_ID", None)

        prompt = _extract_latest_user_prompt(body)
        if not prompt:
            yield "_No user message to send to Claude Code._"
            return

        # Fast path disabled — always run the full agent loop.
        prompt = _strip_mode_prefix(prompt)
        repo_name, prompt = _extract_repo_prefix(prompt)
        effort_override, prompt = _extract_effort_prefix(prompt)

        # Keyless callers (no __chat_id__ — generic OpenAI-API clients such
        # as Conduit or voice integrations don't send OWUI's proprietary
        # top-level `chat_id` field) get session identity from a fingerprint
        # of the conversation prefix rather than from chat_id — see the
        # block below. A cold start (first turn, or a fingerprint miss: new
        # chat, edited message, pruned entry) replays the full
        # body["messages"] history as a transcript once, then resumes warm.
        # Keyed callers (native UI) keep session resume; on a cold start
        # (map entry lost, e.g. after an OWUI restart) they too replay
        # history once, then resume warm from the new session.
        chat_id = __chat_id__ or None
        # Keyless callers (Conduit, generic OpenAI-API clients) get session
        # identity by fingerprinting the conversation prefix. Hit → resume
        # that session in its own workdir (warm, tool results intact).
        # Miss (new chat, edited message) → fresh uuid workdir, replay once,
        # register after the turn. This also retires the old shared
        # "default" workdir where all keyless chats collided.
        fp_entry: Optional[Dict[str, Any]] = None
        if chat_id:
            workdir = Path(self.valves.WORKDIR_ROOT) / chat_id
        else:
            fp_entry = _load_fp_store(self.valves.WORKDIR_ROOT).get(
                _history_fingerprint(
                    _strip_latest_user(body.get("messages") or [])
                )
            )
            if fp_entry and not fp_entry.get("workdir"):
                # Malformed/partial entry — treat as a miss rather than
                # trusting a workdir (or session_id) that isn't there.
                fp_entry = None
            if fp_entry:
                workdir = Path(fp_entry["workdir"])
            else:
                workdir = (
                    Path(self.valves.WORKDIR_ROOT)
                    / f"anon-{uuid.uuid4().hex[:12]}"
                )
        workdir.mkdir(parents=True, exist_ok=True)

        # cwd: persisted repo choice wins; else an explicit #repo: prefix on
        # a fresh session; else the scratch workdir. Metadata lives under
        # WORKDIR_ROOT regardless — never inside a repo.
        repo_map = _parse_repo_map(self.valves.REPO_MAP)
        cwd = Path(
            (
                _load_session_meta(self.valves.WORKDIR_ROOT, chat_id).get("cwd")
                if chat_id
                else (fp_entry or {}).get("cwd")
            )
            or workdir
        )
        if repo_name is not None:
            if repo_name not in repo_map:
                allowed = ", ".join(sorted(repo_map)) or "(none configured)"
                yield (
                    f"_Unknown repo `{repo_name}`. Allowed: {allowed}._"
                )
                return
            target = Path(repo_map[repo_name])
            if not target.is_dir():
                yield f"_Repo path for `{repo_name}` does not exist on the host._"
                return
            if chat_id:
                has_session = bool(
                    _chat_sessions.get(chat_id)
                    or _load_session_meta(self.valves.WORKDIR_ROOT, chat_id).get(
                        "session_id"
                    )
                )
            else:
                has_session = bool(fp_entry and fp_entry.get("session_id"))
            if has_session and cwd != target:
                # A live session already exists for this chat; Claude Code
                # sessions are per-directory, so switching mid-chat can't
                # resume. Tell the user instead of silently degrading.
                yield (
                    "_This chat already runs in another directory; `#repo:` "
                    "only takes effect on a chat's first message. Start a "
                    "new chat._"
                )
                return
            cwd = target

        allowed_tools = [
            t.strip() for t in self.valves.ALLOWED_TOOLS.split(",") if t.strip()
        ]
        # In-process map is a cache; the meta file survives OWUI restarts,
        # pipe redeploys, and valve edits.
        resume_id: Optional[str] = None
        if not _no_resume:
            if chat_id:
                resume_id = _chat_sessions.get(chat_id) or _load_session_meta(
                    self.valves.WORKDIR_ROOT, chat_id
                ).get("session_id")
            elif fp_entry:
                resume_id = fp_entry.get("session_id")

        # Image attachments on the latest user message: OWUI delivers them as
        # image_url content parts, which the text-only prompt extraction
        # drops. Write them to files under the workdir and reference the
        # paths in the prompt so the agent can Read them.
        attachment_notes = _save_image_attachments(body, workdir)
        if attachment_notes:
            prompt = prompt + "\n\n" + "\n".join(attachment_notes)

        if resume_id is None:
            prompt = _render_transcript(body, prompt)

        # Knowledge base attached via Workspace Model → expose as an MCP tool
        # Claude can call agentically. OpenWebUI's middleware already added one
        # entry per attached KB to files/__files__.
        kb_server, kb_tool_names, _ = _build_kb_mcp_server(
            _knowledge_collections(__metadata__, __files__),
            knowledge_row_ids=_knowledge_row_ids(__metadata__),
            user_dict=__user__,
            event_emitter=__event_emitter__,
        )
        allowed_tools = allowed_tools + kb_tool_names

        options_kwargs: Dict[str, Any] = {
            "cwd": str(cwd),
            "model": self._resolve_model(body),
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": allowed_tools,
            # Which filesystem settings to load (user ~/.claude/, project .claude/,
            # local). Default empty = clean baseline, no inheritance of the backend
            # user's config. Opt in via the SETTING_SOURCES valve (e.g. "user" to
            # load ~/.claude/CLAUDE.md). See the valve's security warning.
            "setting_sources": _parse_setting_sources(self.valves.SETTING_SOURCES),
            # Stream token-level deltas so long answers type out instead of
            # appearing as one chunk when the block finishes.
            "include_partial_messages": True,
        }
        if resume_id:
            options_kwargs["resume"] = resume_id
        if self.valves.MAX_TURNS:
            options_kwargs["max_turns"] = self.valves.MAX_TURNS
        if self.valves.MAX_BUFFER_MB:
            options_kwargs["max_buffer_size"] = (
                self.valves.MAX_BUFFER_MB * 1024 * 1024
            )
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}
        effort = effort_override or self.valves.EFFORT.strip().lower() or None
        if effort in _EFFORT_LEVELS:
            options_kwargs["effort"] = effort
        if self.valves.TASK_BUDGET_TOKENS >= 20_000:
            options_kwargs["task_budget"] = {
                "total": self.valves.TASK_BUDGET_TOKENS
            }
        if self.valves.FALLBACK_MODEL.strip():
            options_kwargs["fallback_model"] = self.valves.FALLBACK_MODEL.strip()

        # Extend Claude Code's default agent-loop system prompt with whatever
        # the Workspace Model configured. `append` keeps the agentic prompt
        # intact while adding domain persona/rules on top.
        append_parts: List[str] = []
        # The gateway contract goes first so the operator's Workspace-Model
        # prompt is the LATER text: on a persona/style conflict the operator
        # wins, which is the precedence we want. (Presence, not position, is
        # what protects a repo-rooted session — position only decides ties.)
        if self.valves.GATEWAY_CONTRACT_PATH:
            # The WORKDIR_ROOT exemption assumes a scratch session loads
            # <WORKDIR_ROOT>/CLAUDE.md by itself — true only while
            # SETTING_SOURCES lets it. That valve defaults to "" (load
            # nothing), so without this check a valve reset would leave every
            # scratch AND keyless session contractless while repo-rooted ones
            # stayed covered: the exact inversion of the bug this fixes.
            loads_from_cwd = "project" in _parse_setting_sources(
                self.valves.SETTING_SOURCES
            )
            contract = _gateway_contract(
                str(cwd),
                self.valves.WORKDIR_ROOT if loads_from_cwd else "",
                self.valves.GATEWAY_CONTRACT_PATH,
            )
            if contract:
                append_parts.append(contract)
        system_prompt = _extract_system_prompt(body)
        if system_prompt:
            append_parts.append(system_prompt)
        if not chat_id:
            # Keyless callers are mobile/voice surfaces (Conduit): small
            # screens, often TTS. Nudge hard toward brevity.
            append_parts.append(
                "Surface note: this reply may render on a small mobile "
                "screen. Answer as concisely as correctness allows — lead "
                "with the answer, no headers unless essential, no trailing "
                "recap."
            )
        if append_parts:
            options_kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(append_parts),
            }

        agent_env = _agent_env(chat_id)
        if agent_env:
            options_kwargs["env"] = {**options_kwargs.get("env", {}), **agent_env}

        options = ClaudeAgentOptions(**options_kwargs)

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        await emit_status("Starting Claude Code…")
        # Claude often saves generated files to /tmp from habit (absolute paths
        # in matplotlib/PIL examples), even though cwd is the chat workdir.
        # Scan both so we don't miss the image.
        scan_dirs = [workdir, Path("/tmp")]
        artifact_snapshot = _snapshot_artifacts(scan_dirs)

        # Buffer thinking deltas and emit the <details>…</details> wrapper as
        # one atomic chunk at content_block_stop. Streaming the opener+content
        # token-by-token is unreliable: CommonMark's HTML block terminates at
        # blank lines, so thinking text with paragraph breaks strands the
        # opening <details><summary> as literal text in some renderers. Reset
        # at each message_start (indices restart per assistant message).
        thinking_buffers: Dict[int, str] = {}

        # Consecutive assistant text blocks (each narration beat between tool
        # calls is its own block) would otherwise stream back-to-back with no
        # separator — "…the vault note:Diff is exactly…". Emit a paragraph
        # break at each new text block after the first; Markdown collapses
        # blank lines, so an already-separated stream can't over-space.
        text_emitted = False

        # Heartbeat: when a tool starts, emit a status update every 5s showing
        # elapsed time so the user sees that long-running commands (e.g. a 30s
        # Bash) aren't stuck. Keyed by tool_use_id; completed tools removed on
        # the matching ToolResultBlock.
        active_tools: Dict[str, Dict[str, Any]] = {}
        heartbeat_task: Optional[asyncio.Task] = None

        async def _heartbeat() -> None:
            try:
                while active_tools:
                    await asyncio.sleep(2)
                    if not active_tools:
                        return
                    oldest = min(active_tools.values(), key=lambda t: t["started"])
                    elapsed = int(time.monotonic() - oldest["started"])
                    count = len(active_tools)
                    label = (
                        oldest["label"]
                        if count == 1
                        else f"{count} tools · longest {oldest['label']}"
                    )
                    log.debug("heartbeat tick: %s · %ss", label, elapsed)
                    await emit_status(f"⏳ {label} · running {elapsed}s…")
            except asyncio.CancelledError:
                pass

        def _ensure_heartbeat() -> None:
            nonlocal heartbeat_task
            if heartbeat_task is None or heartbeat_task.done():
                heartbeat_task = asyncio.create_task(_heartbeat())

        # Session id delivered by this turn's init message (keyed AND keyless
        # callers) — consumed by keyless registration and resume-retry.
        turn_session_id: Optional[str] = None

        # Usage of the most recent main-thread API call. Its field sum IS the
        # session's current context size; ResultMessage.usage cannot be used
        # for this — it accumulates across every call in the turn, re-counting
        # the cached prefix once per tool round. Fallback only: the primary
        # context source is client.get_context_usage() at turn end.
        last_usage: Optional[Dict[str, Any]] = None

        # Main-thread tool calls this turn, for the Done line.
        tool_count = 0

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            session_id = message.data.get("session_id")
                            if session_id:
                                turn_session_id = session_id
                                if chat_id:
                                    _chat_sessions[chat_id] = session_id
                                    _save_session_meta(
                                        self.valves.WORKDIR_ROOT,
                                        chat_id,
                                        {
                                            "session_id": session_id,
                                            "cwd": str(cwd),
                                        },
                                    )
                            history = _strip_latest_user(
                                body.get("messages") or []
                            )
                            if resume_id:
                                await emit_status("Session: resumed")
                            elif _history_fingerprint(history) != _EMPTY_FP:
                                await emit_status(
                                    "Session: cold start — history replayed"
                                )
                            else:
                                await emit_status("Session: new chat")
                        elif message.subtype == "compact_boundary":
                            # Auto-compaction summarized earlier history — the
                            # context number will drop; say why.
                            await emit_status("Session: context compacted")
                        continue

                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                                await emit_status("💭 Thinking…")
                            elif block.get("type") == "text" and text_emitted:
                                yield "\n\n"
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    text_emitted = True
                                    yield text
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                            # signature_delta / input_json_delta: ignore. Tool input
                            # is rendered once fully from AssistantMessage below.
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                text = thinking_buffers.pop(idx).strip()
                                if text and self.valves.INLINE_TOOL_DETAILS:
                                    yield (
                                        "\n\n<details>\n<summary>💭 Thinking</summary>\n\n"
                                        f"{text}\n\n"
                                        "</details>\n\n"
                                    )
                                elif text:
                                    await emit_status("💭 thinking")
                        continue

                    if isinstance(message, AssistantMessage):
                        # Subagent (parent_tool_use_id set) calls run in their
                        # own context windows — only main-thread usage reflects
                        # this session.
                        if message.parent_tool_use_id is None:
                            if message.usage:
                                last_usage = message.usage
                            tool_count += sum(
                                isinstance(b, ToolUseBlock)
                                for b in message.content
                            )
                        # Text + thinking already streamed via StreamEvent. Only
                        # emit tool-use previews here (we need the completed
                        # input dict, which StreamEvent only has as partial JSON).
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                label = (
                                    f"{block.name}: {preview}"
                                    if preview
                                    else block.name
                                )
                                await emit_status(f"🔧 {label}")
                                active_tools[block.id] = {
                                    "label": label,
                                    "started": time.monotonic(),
                                }
                                _ensure_heartbeat()
                                # Render as a collapsed <details>: summary is
                                # plain text (OpenWebUI's sanitizer strips
                                # inline HTML like <strong>/<code> inside
                                # <summary> and renders the tags as literal
                                # text); expanding reveals the full tool
                                # input as a language-tagged fenced code block.
                                # Don't html.escape here — OpenWebUI escapes
                                # <summary> content itself, so pre-escaping
                                # would double-encode ("&lt;" → "&amp;lt;").
                                summary_text = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                if self.valves.INLINE_TOOL_DETAILS:
                                    tool_body = _tool_input_block(block.name, block.input)
                                    yield (
                                        "\n\n<details>\n"
                                        f"<summary>{summary_text}</summary>\n\n"
                                        f"{tool_body}\n\n"
                                        "</details>\n\n"
                                    )
                        continue

                    if isinstance(message, UserMessage):
                        content = message.content
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                active_tools.pop(block.tool_use_id, None)
                                if block.is_error:
                                    # Tool errors are usually transient — Claude
                                    # retries and recovers. Render as a quiet,
                                    # collapsed detail so the red icon / big
                                    # traceback doesn't alarm users.
                                    err_text = _format_tool_result(block.content)[:800]
                                    if self.valves.INLINE_TOOL_DETAILS:
                                        yield (
                                            "\n\n<details>\n<summary>"
                                            "<sub>⚙️ tool hiccup (retrying)</sub>"
                                            "</summary>\n\n"
                                            f"```\n{err_text}\n```\n\n"
                                            "</details>\n\n"
                                        )
                                    else:
                                        await emit_status("⚙️ tool hiccup (retrying)")
                        continue

                    if isinstance(message, ResultMessage):
                        parts: List[str] = []
                        if message.duration_ms:
                            parts.append(_fmt_duration(message.duration_ms))
                        if tool_count:
                            plural = "s" if tool_count != 1 else ""
                            parts.append(f"{tool_count} tool{plural}")
                        # Live per-model context: totalTokens against the raw
                        # model window, same data as the CLI's /context.
                        ctx = ""
                        try:
                            cu = await asyncio.wait_for(
                                client.get_context_usage(), timeout=5
                            )
                            used = int(cu.get("totalTokens") or 0)
                            window = int(cu.get("rawMaxTokens") or 0)
                            if used and window:
                                pct = round(100 * used / window)
                                ctx = (
                                    f"context {_fmt_tokens(used)}/"
                                    f"{_fmt_tokens(window)} ({pct}%)"
                                )
                        except Exception:
                            log.debug(
                                "get_context_usage failed", exc_info=True
                            )
                        if not ctx:
                            ctx = _context_status(
                                last_usage, self.valves.CONTEXT_WINDOW_TOKENS
                            )
                        if ctx:
                            parts.append(ctx)
                        await emit_status(
                            "Done · " + " · ".join(parts) if parts else "Done.",
                            done=True,
                        )
                        usage_payload = (
                            _owui_usage(last_usage) if last_usage else None
                        )
                        if __event_emitter__ is not None and usage_payload:
                            # Live listeners (usage-display filters on the
                            # socket) — this event is never persisted.
                            try:
                                await __event_emitter__(
                                    {
                                        "type": "chat:completion",
                                        "data": {"usage": usage_payload},
                                    }
                                )
                            except Exception:
                                log.debug("usage emit failed", exc_info=True)
                        if usage_payload:
                            # The socket emitter's save_to_chat branch drops
                            # chat:completion, and the message's ⓘ popover
                            # renders from the *saved* message's `usage` — so
                            # write it to the chat DB directly (in-process,
                            # like the KB lookups). The upsert merges keys, so
                            # the middleware's later {'done', 'output'} upsert
                            # keeps it; isawaitable covers OWUI versions where
                            # the upsert is sync.
                            try:
                                chat_id_meta = (__metadata__ or {}).get(
                                    "chat_id"
                                )
                                msg_id_meta = (__metadata__ or {}).get(
                                    "message_id"
                                )
                                if chat_id_meta and msg_id_meta:
                                    from open_webui.models.chats import Chats

                                    res = Chats.upsert_message_to_chat_by_id_and_message_id(
                                        chat_id_meta,
                                        msg_id_meta,
                                        {"usage": usage_payload},
                                        touch=False,
                                    )
                                    if inspect.isawaitable(res):
                                        await res
                            except Exception:
                                log.debug(
                                    "usage DB write failed", exc_info=True
                                )
                        for chunk in _inline_new_artifacts(
                            scan_dirs,
                            artifact_snapshot,
                            (__user__ or {}).get("id"),
                        ):
                            yield chunk
                        if message.subtype != "success":
                            yield f"\n\n_Agent stopped: {message.subtype}_\n"
                        # Cost footer removed: subscription billing makes it
                        # noise, and TTS reads it aloud in call mode.
                        if turn_info is not None and turn_session_id:
                            turn_info["session_id"] = turn_session_id
                            turn_info["workdir"] = str(workdir)
                            turn_info["cwd"] = str(cwd)
                        return

        except CLIJSONDecodeError:
            log.exception("Claude Agent SDK pipe exceeded the stream-json buffer")
            await emit_status("Error: response exceeded read buffer", done=True)
            yield (
                "\n\n**Response too large to read back.** A single message from "
                "the Claude Code CLI exceeded the "
                f"{self.valves.MAX_BUFFER_MB} MiB `MAX_BUFFER_MB` limit — "
                "usually a very large file or image read into the transcript. "
                "Raise `MAX_BUFFER_MB` in the pipe's valves, or work with a "
                "smaller file.\n"
            )
        except Exception as exc:
            if resume_id and turn_session_id is None:
                # Resume died before the session came up. Drop the stale id
                # and rerun this turn cold — _render_transcript replays the
                # history, and the new session id gets persisted on init.
                log.warning(
                    "resume of %s failed (%s: %s); retrying cold",
                    resume_id, type(exc).__name__, exc,
                )
                if chat_id:
                    _chat_sessions.pop(chat_id, None)
                    _save_session_meta(
                        self.valves.WORKDIR_ROOT, chat_id, {"cwd": str(cwd)}
                    )
                await emit_status("Session expired — replaying history…")
                async for chunk in self._pipe_stream(
                    body, __chat_id__, __event_emitter__, __files__,
                    __user__, __metadata__,
                    turn_info=turn_info, _no_resume=True,
                ):
                    yield chunk
                return
            log.exception("Claude Agent SDK pipe failed")
            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**Claude Code error:** `{type(exc).__name__}: {exc}`\n"
        finally:
            active_tools.clear()
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
