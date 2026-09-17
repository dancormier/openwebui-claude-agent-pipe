#!/usr/bin/env python3
"""Tests for the artifact walker and uploader (src/39_artifacts.py).

Run: python3 test_artifacts.py [<path-to-pipe.py>]

Same standalone pattern as test_turn.py: slice the module at the SDK import,
stub pydantic, exec the head. open_webui's file store is stubbed with an
in-memory dict so the uploader runs without Open WebUI installed.
"""

import asyncio
import os
import pathlib
import sys
import tempfile
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

uploaded = []
forms = []


class _Storage:
    @staticmethod
    def upload_file(handle, name, tags):
        data = handle.read()
        uploaded.append(name)
        return data, f"/store/{name}"


class _Files:
    """Open WebUI 0.11 made insert_new_file async; older releases are sync.
    `mode` flips the stub so both shapes are exercised."""
    mode = "sync"

    @staticmethod
    def insert_new_file(user_id, form):
        forms.append(form)
        if _Files.mode == "async":
            async def _later():
                return form
            return _later()
        if _Files.mode == "reject":
            return None
        return form


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


_mod("open_webui")
_mod("open_webui.models")
_mod("open_webui.models.files", FileForm=lambda **kw: kw, Files=_Files)
_mod("open_webui.storage")
_mod("open_webui.storage.provider", Storage=_Storage)

src = PIPE.read_text(encoding="utf-8")
head = src.split(SPLIT, 1)[0]
mod = types.ModuleType("pipe_head")
mod.__dict__["__name__"] = "pipe_head"
exec(compile(head, str(PIPE), "exec"), mod.__dict__)

fails = []


def inline(*args, **kwargs):
    return asyncio.run(mod._inline_new_artifacts(*args, **kwargs))


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


def touch(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ---- walker prunes dot-directories in the workdir ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    touch(root, "report.md")
    touch(root, "sub/chart.png")
    touch(root, ".git/objects/pack.json")
    touch(root, "clone/.obsidian/app.json")
    touch(root, "notes.py")
    found = sorted(p.relative_to(root).as_posix() for p in mod._iter_artifact_files([root]))
    check("dot-dirs pruned, nested too", found == ["report.md", "sub/chart.png"], found)

# ---- non-workdir scan dirs stay flat and image-only ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    touch(root, "a.png")
    touch(root, "b.txt")
    touch(root, "deep/c.png")
    found = sorted(p.name for p in mod._iter_artifact_files([root / "nowhere", root]))
    check("secondary dir: images, non-recursive", found == ["a.png"], found)

# ---- per-turn cap and overflow note ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    for i in range(30):
        touch(root, f"f{i:02d}.md")
    uploaded.clear()
    chunks = inline([root], before, "user")
    text = "".join(chunks)
    check("uploads capped", len(uploaded) == mod._MAX_ARTIFACTS_PER_TURN, len(uploaded))
    check("overflow counted", "5 more new files" in text, text[-200:])
    check("capped files are the first sorted", uploaded[0].endswith("_f00.md") and uploaded[-1].endswith("_f24.md"), uploaded[:1] + uploaded[-1:])

# ---- untouched files are not re-uploaded ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    touch(root, "old.md")
    before = mod._snapshot_artifacts([root])
    touch(root, "new.md")
    uploaded.clear()
    chunks = inline([root], before, "user")
    check("only the new file uploads", [u.split("_", 1)[1] for u in uploaded] == ["new.md"], uploaded)
    check("no overflow note under cap", "more new files" not in "".join(chunks))

# ---- inline image cap: extras become download links ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    for i in range(10):
        touch(root, f"img{i:02d}.png")
    uploaded.clear()
    text = "".join(inline([root], before, "user"))
    check("all images uploaded", len(uploaded) == 10, len(uploaded))
    check("inline images capped", text.count("![") == mod._MAX_INLINE_IMAGES, text.count("!["))
    check("rest are file links", "📎 2 files:" in text, text[-300:])

# ---- configured caps override the defaults together ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    for i in range(5):
        touch(root, f"custom{i:02d}.png")
    uploaded.clear()
    text = "".join(inline([root], before, "user", 3, 1))
    check("custom artifact cap limits uploads", len(uploaded) == 3, len(uploaded))
    check("custom artifact cap reports overflow", "2 more new files" in text, text)
    check("custom inline cap renders one image", text.count("![") == 1, text)
    check("remaining uploaded images are links", "📎 2 files:" in text, text)

# ---- async insert_new_file (Open WebUI 0.11) is awaited, not dropped ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    touch(root, "chart.png")
    uploaded.clear()
    _Files.mode = "async"
    try:
        text = "".join(inline([root], before, "user"))
    finally:
        _Files.mode = "sync"
    check("async row insert yields an inline image", "![chart.png]" in text, text)
    check("no un-awaited coroutine warning path", "not linkable" not in text, text)

# ---- insert_new_file returning None is reported, not linked ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    touch(root, "chart.png")
    _Files.mode = "reject"
    try:
        text = "".join(inline([root], before, "user"))
    finally:
        _Files.mode = "sync"
    check("rejected row is not linked", "![" not in text and "not linkable" in text, text)

# ---- links are relative by default, absolute with a base URL ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    touch(root, "chart.png")
    touch(root, "report.md")
    text = "".join(inline([root], before, "user"))
    check("relative image link without base", "![chart.png](/api/v1/files/" in text, text)
    check("relative doc link without base", "[report.md](/api/v1/files/" in text, text)
    text = "".join(inline([root], before, "user", base_url="https://chat.example.com"))
    check("absolute image link with base", "![chart.png](https://chat.example.com/api/v1/files/" in text, text)
    check("absolute doc link with base", "[report.md](https://chat.example.com/api/v1/files/" in text, text)
    check("no relative links remain", "](/api/v1/files/" not in text, text)

# ---- base URL resolution: valve, then WEBUI_URL, then relative ----
_saved = os.environ.pop("WEBUI_URL", None)
try:
    check("no valve, no env: relative", mod._artifact_base_url("") == "")
    os.environ["WEBUI_URL"] = "https://env.example.com/"
    check("env fallback, trailing slash stripped", mod._artifact_base_url("") == "https://env.example.com")
    check("valve wins over env", mod._artifact_base_url(" https://valve.example.com/ ") == "https://valve.example.com")
finally:
    if _saved is None:
        os.environ.pop("WEBUI_URL", None)
    else:
        os.environ["WEBUI_URL"] = _saved

# ---- text deliverables are recorded text/plain so the content route serves them inline ----
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    before = mod._snapshot_artifacts([root])
    for name in ("a.md", "b.txt", "c.yaml", "d.yml", "e.json", "f.csv", "g.png", "h.pdf"):
        touch(root, name)
    forms.clear()
    inline([root], before, "user")
    types = {f["filename"]: f["meta"]["content_type"] for f in forms}
    for name in ("a.md", "b.txt", "c.yaml", "d.yml", "e.json"):
        check(f"{name} is text/plain", types.get(name) == "text/plain", types.get(name))
    check("csv keeps its own type", types.get("f.csv") == "text/csv", types.get("f.csv"))
    check("png keeps its own type", types.get("g.png") == "image/png", types.get("g.png"))
    check("pdf keeps its own type", types.get("h.pdf") == "application/pdf", types.get("h.pdf"))

# ---- pasted workdir paths rewrite to served URLs ----
rw = mod._rewrite_workdir_paths
m = {"/w/c/report.md": "U1", "/w/c/report.md.bak": "U2", "/w/c/img.png": "U3"}
check("link target rewritten, label kept", rw("see [the report](/w/c/report.md) now", m) == "see [the report](U1) now")
check("bare path becomes a named link", rw("saved to /w/c/img.png.", m) == "saved to [img.png](U3).")
check("unrelated path untouched", rw("[x](/w/c/other.md) and /w/c/report.md5", m) == "[x](/w/c/other.md) and /w/c/report.md5")
check("prefix path handled longest-first", rw("[a](/w/c/report.md.bak) [b](/w/c/report.md)", m) == "[a](U2) [b](U1)")
check("unknown longer suffix untouched", rw("/w/c/report.md.old", m) == "/w/c/report.md.old")
check("no mapping is a no-op", rw("plain text", {}) == "plain text")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all passed")
