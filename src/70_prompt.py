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
# A bound on what one upload may write to disk, not on what the agent can
# view: anything large gets a downsized view copy below.
_MAX_IMAGE_ATTACHMENT_BYTES = 64 * 1024 * 1024
# The Claude API rejects an image over 2000 px on either edge once a request
# holds more than 20 images, which a long chat with screenshots reaches.
_IMAGE_VIEW_MAX_EDGE = 2000
_IMAGE_VIEW_MAX_BYTES = 4 * 1024 * 1024


class _NoViewCopy(Exception):
    pass


def _write_view_copy(path: Path) -> Optional[Tuple[Path, Tuple[int, int]]]:
    """Write a downsized copy of an image that is too large to view as is,
    next to the original, and return (copy path, its pixel size). None when
    the image is small enough already. Raises _NoViewCopy when that cannot
    be told or done: Pillow ships with Open WebUI but is not a requirement
    of this pipe, it cannot decode every format (HEIC, SVG), and it refuses
    a decompression bomb, whose file is small and whose pixels are not."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise _NoViewCopy("Pillow is not installed") from exc
    try:
        with Image.open(path) as img:
            if (
                max(img.size) <= _IMAGE_VIEW_MAX_EDGE
                and path.stat().st_size <= _IMAGE_VIEW_MAX_BYTES
            ):
                return None
            # Phone photos carry their rotation in EXIF, which the re-encode drops.
            img = ImageOps.exif_transpose(img)
            if img.mode.startswith("I"):
                # A plain convert clips 16- and 32-bit samples at 255.
                img = img.point(lambda v: v / 256).convert("L")
            elif img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGBA" if "transparency" in img.info else "RGB")
            img.thumbnail((_IMAGE_VIEW_MAX_EDGE, _IMAGE_VIEW_MAX_EDGE))
            view = path.with_name(f"{path.stem}-view.png")
            # PNG keeps screenshot text sharp; a photo saved as PNG is the
            # case that stays too heavy and falls through to JPEG.
            if path.suffix == ".png":
                img.save(view, optimize=True)
                if view.stat().st_size <= _IMAGE_VIEW_MAX_BYTES:
                    return view, img.size
                view.unlink()
            view = view.with_suffix(".jpg")
            if img.mode == "RGBA":
                # JPEG has no alpha, and dropping it turns transparent
                # pixels black: dark text on a clear background vanishes.
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
            img.convert("RGB").save(view, "JPEG", quality=88)
            return view, img.size
    except Exception as exc:
        raise _NoViewCopy(str(exc)) from exc


def _attachment_note(path: Path) -> str:
    mib = path.stat().st_size / (1024 * 1024)
    try:
        made = _write_view_copy(path)
    except _NoViewCopy as exc:
        log.warning("no view copy for %s: %s", path.name, exc)
        # Without a decode the pixel size is unknown, so only the byte size
        # can say whether the original is safe to hand to Read as is.
        if isinstance(exc.__cause__, ImportError) and mib * 1024 * 1024 <= _IMAGE_VIEW_MAX_BYTES:
            return f"[Attached image saved to: {path} — use Read to view it]"
        return (
            f"[Attached image saved to: {path} ({mib:.1f} MiB, no downsized "
            f"copy could be made) — check its size and convert or downscale "
            f"it before Read]"
        )
    if made:
        view, (width, height) = made
        return (
            f"[Attached image saved to: {view} — use Read to view it. That is "
            f"a {width}x{height} copy; the full-size original ({mib:.1f} MiB) "
            f"is at {path} — crop a region from it when you need finer detail]"
        )
    return f"[Attached image saved to: {path} — use Read to view it]"


def _save_image_attachments(body: Dict[str, Any], workdir: Path) -> List[str]:
    """Write image_url content parts of the LATEST user message to files
    under <workdir>/attachments/ so the SDK subprocess (which has filesystem
    access but receives no image blocks from this pipe) can Read them.

    Only the latest user message is scanned — historical image parts are
    deliberately not re-written on transcript replays. Returns one
    prompt-note line per attachment (the saved path, or a skip note).

    data: URIs are decoded in-process; plain http(s) URLs are downloaded
    best-effort (5s timeout) and skipped with a note on failure. An image
    too large to view as is also gets a downsized copy, and the note points
    at that."""
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
                        raise ValueError(
                            f"exceeds {_MAX_IMAGE_ATTACHMENT_BYTES // (1024 * 1024)} MiB limit"
                        )
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
        notes.append(_attachment_note(path))
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
