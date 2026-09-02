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

_DEFAULT_WORKDIR_ROOT = "/tmp/claude-agent-pipe"


def _workdir_root(raw: str) -> str:
    """The WORKDIR_ROOT valve, or its default when blank. A cleared field must
    not become Path("") — that is the Open WebUI process's own cwd."""
    return (raw or "").strip() or _DEFAULT_WORKDIR_ROOT


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
    THAT repo's CLAUDE.md and never sees the operator's own rules file. On
    2026-07-27 that meant `#repo:<name>` sessions ran with no operator rules
    at all — under bypassPermissions — because the repo had no root
    instruction file. Adding one per repo fixes the repos we remember; this
    fixes the class, including the next REPO_MAP entry someone adds without
    one.

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
    the chat). Tooling the agent runs (the author's background-job submitter,
    which posts a result back into the originating chat) reads this variable;
    nothing in this repo does.

    Set only when an id exists — never as an empty string. Consumers treat
    "unset" as "this session has no chat to deliver into", and a worker that
    spawns nested agents must strip the variable (`env -u HUB_CHAT_ID`)
    rather than rely on it being absent, because a child inherits the whole
    environment.
    """
    return {"HUB_CHAT_ID": chat_id} if chat_id else {}


