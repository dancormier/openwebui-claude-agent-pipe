# ---------------------------------------------------------------------------
# ask_user: the pure half. Normalizes the agent's question list into the
# payload Open WebUI's `request:user_input` event expects (its own builtin
# ask_user in open_webui/tools/builtin.py sets the field limits), renders the
# same questions as markdown for clients with no form (Conduit, API callers,
# anything without a socket session), and maps the form's reply into the
# tool result. No SDK or Open WebUI imports so the head-slice suites reach it.
# ---------------------------------------------------------------------------

_ASK_USER_TOOL = "mcp__ask-user__ask_user"
_ASK_USER_MAX_QUESTIONS = 4
_ASK_USER_MAX_OPTIONS = 3
_ASK_USER_TIMEOUT_MS = 240_000
# The form's own timer expiry answers with a cancel; the grace covers the
# round trip. Open WebUI's server-side wait (WEBSOCKET_EVENT_CALLER_TIMEOUT)
# is unset by default, which is `timeout=None`: a form unmounted by a chat
# switch or reload never answers, and without this bound the tool call —
# and the turn — hang forever (seen 2026-09-03).
_ASK_USER_TIMEOUT_GRACE_MS = 15_000
_ASK_USER_UNANSWERED_INSTRUCTION = (
    "The user did not answer. Proceed on your best assumption and say "
    "which one you took."
)
_ASK_USER_NO_UI_INSTRUCTION = (
    "This client has no question form. Put the ask_in_reply text in your "
    "reply verbatim, then end the turn without doing the work; the user's "
    "next message carries the answers."
)


def _normalize_questions(raw: Any) -> List[Dict[str, Any]]:
    """Validate and clamp the agent's questions to the form's limits.
    Raises ValueError with a message the agent can act on."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("questions must be a non-empty list")
    if len(raw) > _ASK_USER_MAX_QUESTIONS:
        raise ValueError(f"at most {_ASK_USER_MAX_QUESTIONS} questions per call")
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for index, q in enumerate(raw, 1):
        if not isinstance(q, dict):
            raise ValueError(f"question {index} must be an object")
        text = str(q.get("question") or "").strip()[:500]
        if not text:
            raise ValueError(f"question {index} needs question text")
        options = q.get("options") or []
        if not isinstance(options, list) or len(options) > _ASK_USER_MAX_OPTIONS:
            raise ValueError(
                f"question {index} takes at most {_ASK_USER_MAX_OPTIONS} options"
            )
        norm_options = []
        for opt in options:
            if isinstance(opt, str):
                opt = {"label": opt}
            if not isinstance(opt, dict):
                raise ValueError(f"question {index}: each option must be an object")
            label = str(opt.get("label") or "").strip()[:80]
            if not label:
                raise ValueError(f"question {index}: each option needs a label")
            norm_options.append(
                {
                    "label": label,
                    "description": str(opt.get("description") or "").strip()[:240],
                }
            )
        qid = str(q.get("id") or f"q{index}").strip()[:64]
        if qid in seen:
            raise ValueError(f"duplicate question id: {qid}")
        seen.add(qid)
        out.append(
            {
                "id": qid,
                "header": str(q.get("header") or "").strip()[:48]
                or f"Question {index}",
                "question": text,
                "options": norm_options,
                # Fewer than two options is not a choice; the free-text field
                # is then the answer, so it cannot be switched off.
                "allow_other": len(norm_options) < 2 or bool(q.get("allow_other", True)),
            }
        )
    return out


def _render_questions_markdown(questions: List[Dict[str, Any]]) -> str:
    """The no-form rendering: numbered questions, lettered options, so a
    one-line reply like `1b, 2a` is unambiguous."""
    lines: List[str] = []
    for n, q in enumerate(questions, 1):
        lines.append(f"{n}. **{q['header']}** — {q['question']}")
        for letter, opt in zip("abc", q["options"]):
            desc = f" — {opt['description']}" if opt["description"] else ""
            lines.append(f"   - ({letter}) {opt['label']}{desc}")
        if q["allow_other"]:
            lines.append(
                "   - or type your own answer" if q["options"] else "   - type your answer"
            )
        lines.append("")
    picks = ", ".join(
        f"{n}a" if q["options"] else f"{n}: …"
        for n, q in enumerate(questions, 1)
    )
    lines.append(f"Reply with your picks, e.g. `{picks}`.")
    return "\n".join(lines)


def _user_input_payload(
    questions: List[Dict[str, Any]], timeout_ms: int = _ASK_USER_TIMEOUT_MS
) -> Dict[str, Any]:
    if not isinstance(timeout_ms, int) or not 60_000 <= timeout_ms <= 240_000:
        timeout_ms = _ASK_USER_TIMEOUT_MS
    return {
        "type": "request:user_input",
        "data": {
            "questions": questions,
            "allow_other": any(q["allow_other"] for q in questions),
            "timeout_ms": timeout_ms,
        },
    }


def _ask_user_wait_seconds(payload: Dict[str, Any]) -> float:
    return (payload["data"]["timeout_ms"] + _ASK_USER_TIMEOUT_GRACE_MS) / 1000


def _answer_text(value: Any) -> str:
    """The form answers with {"type":"option","label",...} for a pick and
    {"type":"other","text"} for free text; older or other clients may send
    a bare string."""
    if isinstance(value, dict):
        value = value.get("text") if value.get("type") == "other" else value.get("label")
    if value is None:
        return ""
    return str(value).strip()


def _map_user_input_response(
    output: Any, questions: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Turn the form's reply into the tool result. A cancel or a timeout
    means the user saw the form and did not answer → `unanswered`. Any
    other error means the form never reached them — a client that holds a
    socket session but has no form (Conduit answers the event with
    "Invalid user input request.", verified 2026-09-02), or a dropped
    session — so the questions fall through to the no-form path."""
    if not isinstance(output, dict):
        return {"status": "unanswered", "reason": "no response",
                "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
    if output.get("error"):
        reason = str(output["error"])
        if "timed out" in reason.lower():
            return {"status": "unanswered", "reason": reason,
                    "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
        return {**_no_ui_result(questions), "reason": reason}
    if output.get("status") == "cancelled":
        return {"status": "unanswered", "reason": "cancelled",
                "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
    raw = output.get("answers")
    if not isinstance(raw, dict):
        return {"status": "unanswered", "reason": "no answers",
                "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
    answers: Dict[str, str] = {}
    for q in questions:
        value = _answer_text(raw.get(q["id"]))
        if value:
            answers[q["id"]] = value
    if not answers:
        return {"status": "unanswered", "reason": "empty answers",
                "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
    result: Dict[str, Any] = {"status": "answered", "answers": answers}
    skipped = [q["id"] for q in questions if q["id"] not in answers]
    if skipped:
        result["skipped"] = skipped
    return result


def _no_ui_result(questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "no_ui",
        "ask_in_reply": _render_questions_markdown(questions),
        "instruction": _ASK_USER_NO_UI_INSTRUCTION,
    }


def _ask_user_preview(tool_input: Dict[str, Any]) -> str:
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, dict):
            return str(first.get("question") or first.get("header") or "")
    return ""


