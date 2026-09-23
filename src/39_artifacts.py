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
    ".mp3",
    ".wav",
    ".m4a",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
# Open WebUI's /api/v1/files/{id}/content route serves only text/plain and
# PDF inline; every other content type gets Content-Disposition: attachment,
# so a Markdown or YAML deliverable forced a download instead of opening in
# the tab. Python's mimetypes has no entry for .md/.yaml at all (they fell to
# application/octet-stream), and application/json is not in the inline set.
_INLINE_TEXT_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json"}
# Safety cap to avoid uploading runaway files. Uploaded artifacts are served
# via OpenWebUI's file endpoint, so they don't bloat the chat history even
# when large — this is only a "don't accidentally ship a DVD ISO" guard.
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB
# Per-turn caps. An agent that clones a repo or unpacks an export into the
# workdir makes every file in it "new"; one such turn linked 29,122 files
# (1,972 of them inline images) into a single message, which locked the
# browser tab on open (2026-09-10). Anything past the cap is counted, not
# linked, and images past the inline cap become download links.
_MAX_ARTIFACTS_PER_TURN = 25
_MAX_INLINE_IMAGES = 8


def _iter_artifact_files(scan_dirs: List[Path]) -> "list[Path]":
    """Yield image/document artifacts from each scan dir. Workdir is searched
    recursively; other dirs (typically /tmp) are searched non-recursively to
    avoid picking up unrelated files under nested system caches.

    Non-workdir dirs match images only: the /tmp scan exists because agents
    save matplotlib/PIL output there from habit, but matching documents there
    too published every fetched-page scratch dump (.html/.txt) as an
    artifact. Document deliverables belong in the workdir, which keeps the
    full extension set.

    Dot-directories in the workdir (`.git`, `.obsidian`, `.venv`) are pruned:
    nothing under them is a deliverable, and a checkout's `.git` alone holds
    thousands of matching-extension files."""
    seen: List[Path] = []
    for idx, root in enumerate(scan_dirs):
        if not root.exists():
            continue
        extensions = _ARTIFACT_EXTENSIONS if idx == 0 else _IMAGE_EXTENSIONS
        if idx == 0:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    path = Path(dirpath) / name
                    if path.suffix.lower() in extensions and path.is_file():
                        seen.append(path)
            continue
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in extensions:
                seen.append(path)
    return seen


_ARTIFACTS_PROMPT = (
    "Files you write into the workdir are uploaded and linked automatically "
    "after your reply, as a paperclip line. Never link a workdir file by its "
    "filesystem path: that path is not a URL the client can open."
)

_warned_base_urls: Set[str] = set()


def _artifact_base_url(valve: str) -> str:
    """Origin to prefix artifact URLs with. Native clients (Conduit) render
    the message with no page origin, so a relative `/api/v1/files/...` link
    has nowhere to resolve; the web UI is fine either way. A scheme-less
    value would render as a relative path (`chat.example.com/api/...`) and
    produce dead links everywhere, so it is refused, not guessed at."""
    base = ((valve or "").strip() or os.environ.get("WEBUI_URL", "")).strip()
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        if base not in _warned_base_urls:
            _warned_base_urls.add(base)
            logging.getLogger(__name__).warning(
                "Artifact base URL %r has no http(s) scheme; using relative links",
                base,
            )
        return ""
    return base.rstrip("/")


def _snapshot_artifacts(scan_dirs: List[Path]) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in _iter_artifact_files(scan_dirs):
        try:
            snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return snapshot


async def _inline_new_artifacts(
    scan_dirs: List[Path],
    before: Dict[str, int],
    user_id: Optional[str],
    max_artifacts_per_turn: int = _MAX_ARTIFACTS_PER_TURN,
    max_inline_images: int = _MAX_INLINE_IMAGES,
    base_url: str = "",
) -> List[str]:
    """Upload artifacts new or modified since `before` to OpenWebUI's file
    store, and return markdown referencing the served URLs.

    Why not base64 data URIs: large blobs (multi-MB PDFs) encoded as
    `data:application/pdf;base64,…` in a markdown link cause browsers to spam
    the address bar and stall when clicked. They'd also persist in chat
    history, bloating the DB on every turn.

    URL shape: `{base_url}/api/v1/files/{id}/content` for every artifact
    (`base_url` empty → relative, which only the web UI can resolve).
      - Images: loaded by the markdown `<img>` tag → display inline.
      - PDFs and `text/plain` (see _INLINE_TEXT_EXTENSIONS): the route emits
        `Content-Disposition: inline` → browser opens them in a new tab.
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

    changed: List[Path] = []
    for path in sorted(_iter_artifact_files(scan_dirs)):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if before.get(str(path)) != mtime:
            changed.append(path)
    overflow = max(0, len(changed) - max_artifacts_per_turn)
    changed = changed[:max_artifacts_per_turn]

    chunks: List[str] = []
    doc_links: List[str] = []
    inline_images = 0
    for path in changed:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: {size // 1024 // 1024} MiB exceeds {_MAX_ARTIFACT_BYTES // 1024 // 1024} MiB limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        if ext in _INLINE_TEXT_EXTENSIONS:
            mime = "text/plain"
        else:
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
            row = await _resolve(Files.insert_new_file(
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
            ))
        except Exception as exc:
            log.exception("Artifact DB row failed: %s", path)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue
        if row is None:
            # insert_new_file logs and returns None instead of raising.
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: file row rejected)_\n")
            continue

        url = f"{base_url}/api/v1/files/{file_id}/content"
        if is_image and inline_images < max_inline_images:
            inline_images += 1
            chunks.append(f"\n\n![{path.name}]({url})\n")
        else:
            kib = size // 1024
            doc_links.append(f"[{path.name}]({url}) ({kib} KiB)")
    # One compact paragraph for all document links instead of a block per
    # file: multi-file turns were eating a screenful of chat. Not a <details>
    # toggle — Conduit renders raw HTML literally.
    if doc_links:
        label = "file" if len(doc_links) == 1 else f"{len(doc_links)} files"
        chunks.append(f"\n\n📎 {label}: " + " · ".join(doc_links) + "\n")
    if overflow:
        chunks.append(
            f"\n\n_({overflow} more new files in the workdir not linked: only the "
            f"first {max_artifacts_per_turn} are uploaded per turn. Checkouts "
            "and exports belong outside the workdir.)_\n"
        )
    return chunks
