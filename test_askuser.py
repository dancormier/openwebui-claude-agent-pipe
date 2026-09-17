#!/usr/bin/env python3
"""Tests for the ask_user pure helpers (src/34_askuser.py).

Run: python3 test_askuser.py [<path-to-pipe.py>]

Same standalone pattern as test_turn.py: slice the module at the SDK import,
stub pydantic, exec the head. The event round-trip itself is verified live
in the web UI; these cover the normalization, the no-form markdown, and the
reply mapping that both paths share.
"""

import asyncio
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
check("rejects four options", "at most 3" in (raises(mod._normalize_questions, [{"question": "q", "options": ["a", "b", "c", "d"]}]) or ""))
free = mod._normalize_questions([{"question": "Which restaurant?", "allow_other": False}, {"question": "q", "options": ["only"], "allow_other": False}])
check("no options → free text, allow_other forced on", free[0]["options"] == [] and free[0]["allow_other"] is True)
check("one option → allow_other forced on", len(free[1]["options"]) == 1 and free[1]["allow_other"] is True)
fmd = mod._render_questions_markdown(free)
check("free-text question renders a type-your-answer line", "1. **Question 1** — Which restaurant?\n   - type your answer\n" in fmd, fmd)
check("reply hint mixes typed and lettered picks", fmd.rstrip().endswith("e.g. `1: …, 2a`."), fmd)
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
check("payload carries no timeout_ms, so the form never expires on its own", "timeout_ms" not in payload["data"], payload)
check("wait bound is the valve in seconds", mod._ask_user_wait_seconds(30) == 1800.0)
check("wait bound default", mod._ask_user_wait_seconds(mod._ASK_USER_WAIT_MINUTES) == mod._ASK_USER_WAIT_MINUTES * 60.0)
for bad in (0, -5, 241, "30", None, 2.5):
    check(f"wait bound {bad!r} falls back to the default", mod._ask_user_wait_seconds(bad) == mod._ASK_USER_WAIT_MINUTES * 60.0)
check("wait bound max kept", mod._ask_user_wait_seconds(mod._ASK_USER_WAIT_MINUTES_MAX) == mod._ASK_USER_WAIT_MINUTES_MAX * 60.0)

# ---- rearm bound ----
check("rearm bound is the valve as float seconds", mod._ask_user_rearm_seconds(60) == 60.0)
check("rearm 0 disables", mod._ask_user_rearm_seconds(0) == 0.0)
check("rearm below the floor clamps up", mod._ask_user_rearm_seconds(3) == mod._ASK_USER_REARM_SECONDS_MIN)
check("rearm above the ceiling clamps down", mod._ask_user_rearm_seconds(9999) == mod._ASK_USER_REARM_SECONDS_MAX)
for bad in ("60", None, 2.5, True):
    check(f"rearm {bad!r} falls back to the default", mod._ask_user_rearm_seconds(bad) == float(mod._ASK_USER_REARM_SECONDS))
check("negative rearm disables", mod._ask_user_rearm_seconds(-1) == 0.0)

# ---- which outcomes settle the wait ----
check("answers settle", mod._ask_output_settles({"answers": {"q1": "x"}}, True))
check("cancel settles", mod._ask_output_settles({"status": "cancelled"}, True))
check("dead session settles even with others pending", mod._ask_output_settles({"error": "Client session disconnected."}, True))
check("server timeout ignored while a re-send is pending", not mod._ask_output_settles({"error": "Event call timed out. The browser tab may be inactive or closed."}, True))
check("server timeout settles when nothing else is pending", mod._ask_output_settles({"error": "Event call timed out. The browser tab may be inactive or closed."}, False))
check("non-dict settles", mod._ask_output_settles(None, True))


# ---- rearm orchestration ----
class FakeClient:
    """Each send resolves per its script: a value, an exception, or None
    (never answers). Counts sends and cancellations so leaks are visible."""

    def __init__(self, script):
        self.script = list(script)
        self.sends = 0
        self.cancelled = 0
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(payload)
        index = self.sends
        self.sends += 1
        outcome = self.script[index] if index < len(self.script) else None
        try:
            if outcome is None:
                await asyncio.sleep(3600)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, tuple):
                delay, value = outcome
                await asyncio.sleep(delay)
                return value
            return outcome
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def run(client, wait=5.0, rearm=0.05):
    return asyncio.run(mod._ask_with_rearm(client, PAYLOAD, wait, rearm))


PAYLOAD = mod._user_input_payload(norm)
answered = {"answers": {"q1": "Only auth"}}

c = FakeClient([answered])
check("answered on first send", run(c) == answered and c.sends == 1 and c.cancelled == 0, (c.sends, c.cancelled))

c = FakeClient([None, answered])
check("re-armed send answers, first cancelled", run(c) == answered and c.sends == 2 and c.cancelled == 1, (c.sends, c.cancelled))
check("re-send carries the same payload", c.payloads[0] is c.payloads[1] is PAYLOAD)

c = FakeClient([None, None, {"status": "cancelled"}])
check("cancel on a later send settles, both earlier sends cancelled", run(c)["status"] == "cancelled" and c.sends == 3 and c.cancelled == 2, (c.sends, c.cancelled))

c = FakeClient([{"error": "Client session disconnected."}])
check("error dict on first send returns immediately", run(c, rearm=1.0) == {"error": "Client session disconnected."} and c.sends == 1)

c = FakeClient([RuntimeError("boom")])
r = run(c, rearm=1.0)
check("raised send becomes the error dict", r == {"error": "RuntimeError: boom"} and c.sends == 1, r)

c = FakeClient([(0.02, {"error": "Event call timed out. The browser tab may be inactive or closed."}), (0.1, answered)])
r = run(c, rearm=0.01)
check("server timeout on the first send is ignored while a re-send is pending", r == answered and c.sends >= 2, (r, c.sends))

TIMED_OUT = {"error": "Event call timed out. The browser tab may be inactive or closed."}


class GatedClient(FakeClient):
    """Every send parks on one shared gate, so releasing it completes all of
    them in the same loop pass and the same `asyncio.wait` batch."""

    def __init__(self, script):
        super().__init__(script)
        self.gate = None

    async def __call__(self, payload):
        index = self.sends
        self.sends += 1
        await self.gate.wait()
        return self.script[index]


async def same_batch(client):
    client.gate = asyncio.Event()
    task = asyncio.ensure_future(mod._ask_with_rearm(client, PAYLOAD, 5.0, 0.01))
    while client.sends < 2:
        await asyncio.sleep(0.001)
    client.gate.set()
    return await task


for order, script in (("timeout first", [TIMED_OUT, answered]), ("answers first", [answered, TIMED_OUT])):
    c = GatedClient(script)
    r = asyncio.run(same_batch(c))
    check(f"same-batch completion, {order}: answers win over the stale timeout", r == answered and c.sends == 2, (r, c.sends))

c = FakeClient([None])
check("rearm disabled sends exactly once and expires as timed out", run(c, wait=0.1, rearm=0) == {"error": mod._ASK_USER_TIMED_OUT} and c.sends == 1 and c.cancelled == 1, (c.sends, c.cancelled))

c = FakeClient([])
r = run(c, wait=0.12, rearm=0.05)
check("total wait expiry returns the timed-out shape", r == {"error": mod._ASK_USER_TIMED_OUT}, r)
check("expiry cancels every outstanding send", c.sends >= 2 and c.cancelled == c.sends, (c.sends, c.cancelled))
check("expired wait maps to lost", mod._map_user_input_response(r, norm)["status"] == "lost")

# ---- reply mapping ----
r = mod._map_user_input_response({"answers": {"q1": "Only auth", "busy": " Retry "}}, norm)
check("answers mapped and stripped", r == {"status": "answered", "answers": {"q1": "Only auth", "busy": "Retry"}}, r)
r = mod._map_user_input_response({"answers": {"q1": "Only auth", "busy": ""}}, norm)
check("blank answer reported as skipped", r["status"] == "answered" and r["skipped"] == ["busy"], r)
r = mod._map_user_input_response({"answers": {"q1": "my own words"}}, norm)
check("bare string answer passes through", r["answers"]["q1"] == "my own words")
r = mod._map_user_input_response({"answers": {
    "q1": {"type": "option", "option_index": 1, "label": "Auth and sessions", "description": "Both tables move"},
    "busy": {"type": "other", "text": "  wait 5 min "},
}}, norm)
check("form option object → its label", r["answers"]["q1"] == "Auth and sessions", r)
check("form other object → its text, stripped", r["answers"]["busy"] == "wait 5 min", r)
r = mod._map_user_input_response({"answers": {"q1": {"type": "other", "text": "  "}}}, norm)
check("blank other text → unanswered", r["status"] == "unanswered")
for name, out in [
    ("cancelled", {"status": "cancelled"}),
    ("non-dict", None),
    ("no answers key", {"status": "answered"}),
    ("all blank", {"answers": {"q1": ""}}),
]:
    r = mod._map_user_input_response(out, norm)
    check(f"{name} → unanswered with instruction", r["status"] == "unanswered" and r["instruction"] == mod._ASK_USER_UNANSWERED_INSTRUCTION, r)
for name, err in [
    ("pipe wait ran out", "Event call timed out: the form never answered."),
    ("server bound ran out", "Event call timed out. The browser tab may be inactive or closed."),
]:
    r = mod._map_user_input_response({"error": err}, norm)
    check(f"{name} → lost with the markdown and the lost instruction", r["status"] == "lost" and r["ask_in_reply"] == md and r["instruction"] == mod._ASK_USER_LOST_INSTRUCTION and r["reason"] == err, r)
for name, err in [("client without a form", "Invalid user input request."), ("dropped session", "Client session disconnected.")]:
    r = mod._map_user_input_response({"error": err}, norm)
    check(f"{name} → no_ui with the markdown", r["status"] == "no_ui" and r["ask_in_reply"] == md and r["reason"] == err, r)

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
check("form is the only active tool → quiet wait", mod._only_ask_user(st.active_tools))
mod._on_tool_use("Bash", {"command": "ls"}, "t2", st, False, 1.0)
check("form plus another tool → normal heartbeat", not mod._only_ask_user(st.active_tools))
check("no active tools → not a quiet wait", not mod._only_ask_user({}))

if fails:
    print(f"\nFAILED: {len(fails)} — " + ", ".join(fails))
    sys.exit(1)
print("ok — ask_user tests passed")
