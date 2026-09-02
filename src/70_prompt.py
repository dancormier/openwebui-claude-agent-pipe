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
