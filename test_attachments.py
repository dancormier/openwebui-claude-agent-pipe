#!/usr/bin/env python3
"""Tests for image attachments (src/70_prompt.py).

Run: python3 test_attachments.py [<path-to-pipe.py>]

The downsizing cases need Pillow, which Open WebUI ships and CI does not; they
are skipped without it, and the no-Pillow fallback is covered either way.
"""

import base64
import io
import pathlib
import re
import struct
import tempfile
import zlib

from _loader import check, check_eq, load_full, report

mod = load_full()
mod.log.disabled = True

try:
    from PIL import Image
except ImportError:
    Image = None


def tiny_png():
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def body_for(data, mime="image/png"):
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]}


def save(data, mime="image/png"):
    workdir = pathlib.Path(tempfile.mkdtemp())
    notes = mod._save_image_attachments(body_for(data, mime), workdir)
    return notes, sorted(p.name for p in (workdir / "attachments").glob("*")), workdir


def noted_paths(note):
    return [pathlib.Path(p) for p in re.findall(r"(/\S+\.(?:png|jpg))", note)]


def encoded(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


print("small image")
notes, files, _ = save(tiny_png())
check_eq("one note", len(notes), 1)
check("note is the plain saved-to line", notes[0].endswith("— use Read to view it]"), notes[0])
check_eq("only the original is written", len(files), 1)

print("image over the old 10 MiB limit that cannot be decoded")
junk = b"\x00" * (12 * 1024 * 1024)
notes, files, _ = save(junk)
check("not skipped", "skipped" not in notes[0], notes[0])
check_eq("original is on disk", len(files), 1)
check("note says no copy was made", "no downsized copy could be made" in notes[0], notes[0])
check_eq("original keeps every byte", noted_paths(notes[0])[0].stat().st_size, len(junk))

print("disk bound")
bound = mod._MAX_IMAGE_ATTACHMENT_BYTES
mod._MAX_IMAGE_ATTACHMENT_BYTES = 1024
notes, files, _ = save(b"\x00" * 2048)
mod._MAX_IMAGE_ATTACHMENT_BYTES = bound
check("over the bound is skipped with a note", "skipped" in notes[0], notes[0])
check_eq("nothing written", files, [])

if Image is None:
    print("SKIP  downsizing cases: Pillow not installed")
else:
    edge = mod._IMAGE_VIEW_MAX_EDGE

    print("wide screenshot")
    notes, files, _ = save(encoded(Image.new("RGB", (3000, 1500), "white"), "PNG"))
    view, original = noted_paths(notes[0])
    check_eq("note points at the copy first", view.name.endswith("-view.png"), True)
    check_eq("copy fits the edge", Image.open(view).size, (edge, edge // 2))
    check_eq("original untouched", Image.open(original).size, (3000, 1500))
    check_eq("both files on disk", len(files), 2)

    print("photo")
    noise = Image.effect_noise((4000, 3000), 80).convert("RGB")
    photo = encoded(noise, "JPEG", quality=95)
    notes, files, _ = save(photo, "image/jpeg")
    view, original = noted_paths(notes[0])
    check_eq("copy is a jpeg", view.suffix, ".jpg")
    check("copy fits the edge", max(Image.open(view).size) == edge, Image.open(view).size)
    check_eq("original keeps every byte", original.stat().st_size, len(photo))

    print("photo saved as png falls through to jpeg")
    notes, files, _ = save(encoded(noise, "PNG"))
    view, _ = noted_paths(notes[0])
    check_eq("copy is a jpeg", view.suffix, ".jpg")
    check("no stray png copy left", not any(f.endswith("-view.png") for f in files), files)

    print("EXIF rotation survives the re-encode")
    exif = Image.Exif()
    exif[0x0112] = 6
    notes, _, _ = save(encoded(Image.new("RGB", (3000, 1000), "white"), "JPEG", exif=exif), "image/jpeg")
    view, _ = noted_paths(notes[0])
    width, height = Image.open(view).size
    check("copy is upright", height > width, (width, height))

    print("small image is left alone")
    notes, files, _ = save(encoded(Image.new("RGB", (800, 600), "white"), "PNG"))
    check_eq("only the original is written", len(files), 1)

report("attachment tests passed")
