#!/usr/bin/env python3
"""Tests for the ask_user pure helpers (src/34_askuser.py).

Run: python3 test_askuser.py [<path-to-pipe.py>]

Same standalone pattern as test_turn.py: slice the module at the SDK import,
stub pydantic, exec the head. The event round-trip itself is verified live
in the web UI; these cover the normalization, the no-form markdown, and the
reply mapping that both paths share.
"""

import pathlib
import sys
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

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError as exc:
        return str(exc)
    return None


Q = [
    {
        "header": "Scope",
        "question": "Which files should the migration touch?",
        "options": [
            {"label": "Only auth", "description": "Smallest diff (recommended)"},
            {"label": "Auth and sessions", "description": "Both tables move"},
        ],
    },
    {
        "id": "busy",
        "question": "What if the endpoint is busy?",
        "options": [{"label": "Retry"}, {"label": "Fail"}, "Queue"],
        "allow_other": False,
    },
]

# ---- normalization ----
norm = mod._normalize_questions(Q)
check("ids default to q<n>, explicit id kept", [q["id"] for q in norm] == ["q1", "busy"], norm)
check("header defaults to Question <n>", norm[1]["header"] == "Question 2")
check("string option becomes label with empty description", norm[1]["options"][2] == {"label": "Queue", "description": ""})
check("allow_other defaults true, explicit false kept", norm[0]["allow_other"] is True and norm[1]["allow_other"] is False)
check("description kept", norm[0]["options"][0]["description"] == "Smallest diff (recommended)")

long = {"question": "x" * 600, "header": "h" * 60, "options": [{"label": "l" * 100, "description": "d" * 300}, {"label": "b"}]}
n1 = mod._normalize_questions([long])[0]
check("fields clamped to the form's limits", len(n1["question"]) == 500 and len(n1["header"]) == 48 and len(n1["options"][0]["label"]) == 80 and len(n1["options"][0]["description"]) == 240)

check("rejects empty list", raises(mod._normalize_questions, []) is not None)
check("rejects non-list", raises(mod._normalize_questions, "q?") is not None)
check("rejects five questions", "at most 4" in (raises(mod._normalize_questions, [Q[0]] * 5) or ""))
check("rejects one option", "2-3 options" in (raises(mod._normalize_questions, [{"question": "q", "options": [{"label": "a"}]}]) or ""))
check("rejects four options", "2-3 options" in (raises(mod._normalize_questions, [{"question": "q", "options": ["a", "b", "c", "d"]}]) or ""))
check("rejects missing question text", "question text" in (raises(mod._normalize_questions, [{"options": ["a", "b"]}]) or ""))
check("rejects blank label", "label" in (raises(mod._normalize_questions, [{"question": "q", "options": [{"label": " "}, "b"]}]) or ""))
check("rejects duplicate ids", "duplicate" in (raises(mod._normalize_questions, [{"id": "x", "question": "q", "options": ["a", "b"]}] * 2) or ""))

# ---- no-form markdown ----
md = mod._render_questions_markdown(norm)
check("questions numbered with header", "1. **Scope** — Which files should the migration touch?" in md, md)
check("options lettered with description", "   - (a) Only auth — Smallest diff (recommended)" in md, md)
check("option without description has no dash", "   - (c) Queue\n" in md, md)
check("free-text line only when allow_other", md.count("or type your own answer") == 1)
check("reply hint covers every question", md.rstrip().endswith("Reply with your picks, e.g. `1a, 2a`."), md)

# ---- event payload ----
payload = mod._user_input_payload(norm)
check("payload is the request:user_input event", payload["type"] == "request:user_input" and payload["data"]["questions"] is norm)
check("payload allow_other true if any question allows it", payload["data"]["allow_other"] is True)
check("default timeout inside OWUI's 60-240s window", 60_000 <= payload["data"]["timeout_ms"] <= 240_000)
check("out-of-range timeout falls back", mod._user_input_payload(norm, 5)["data"]["timeout_ms"] == mod._ASK_USER_TIMEOUT_MS)
check("in-range timeout kept", mod._user_input_payload(norm, 90_000)["data"]["timeout_ms"] == 90_000)

# ---- reply mapping ----
r = mod._map_user_input_response({"answers": {"q1": "Only auth", "busy": " Retry "}}, norm)
check("answers mapped and stripped", r == {"status": "answered", "answers": {"q1": "Only auth", "busy": "Retry"}}, r)
r = mod._map_user_input_response({"answers": {"q1": "Only auth", "busy": ""}}, norm)
check("blank answer reported as skipped", r["status"] == "answered" and r["skipped"] == ["busy"], r)
r = mod._map_user_input_response({"answers": {"q1": "my own words"}}, norm)
check("free-text answer passes through", r["answers"]["q1"] == "my own words")
for name, out in [
    ("error", {"error": "timed out"}),
    ("cancelled", {"status": "cancelled"}),
    ("non-dict", None),
    ("no answers key", {"status": "answered"}),
    ("all blank", {"answers": {"q1": ""}}),
]:
    r = mod._map_user_input_response(out, norm)
    check(f"{name} → unanswered with instruction", r["status"] == "unanswered" and r["instruction"] == mod._ASK_USER_UNANSWERED_INSTRUCTION, r)
check("error reason carried", mod._map_user_input_response({"error": "timed out"}, norm)["reason"] == "timed out")

# ---- no-UI result ----
r = mod._no_ui_result(norm)
check("no_ui result carries the markdown and the instruction", r["status"] == "no_ui" and r["ask_in_reply"] == md and "end the turn" in r["instruction"])

# ---- status-line preview ----
check("preview is the first question", mod._ask_user_preview({"questions": Q}) == "Which files should the migration touch?")
check("preview falls back to header", mod._ask_user_preview({"questions": [{"header": "Scope", "options": []}]}) == "Scope")
check("preview empty on junk", mod._ask_user_preview({"questions": "x"}) == "")
check("tool preview uses the custom hook", mod._tool_preview(mod._ASK_USER_TOOL, {"questions": Q}) == "Which files should the migration touch?")
st = mod._TurnState()
_, status = mod._on_tool_use(mod._ASK_USER_TOOL, {"questions": Q}, "t1", st, False, 0.0)
check("status line while waiting", status == f"🔧 {mod._ASK_USER_TOOL}: Which files should the migration touch?", status)

if fails:
    print(f"\nFAILED: {len(fails)} — " + ", ".join(fails))
    sys.exit(1)
print("ok — ask_user tests passed")
