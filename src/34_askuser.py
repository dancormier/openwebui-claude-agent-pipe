# ---------------------------------------------------------------------------
# ask_user: the pure half. Normalizes the agent's question list into the
# payload Open WebUI's `request:user_input` event expects (its own builtin
# ask_user in open_webui/tools/builtin.py sets the field limits), renders the
# same questions as markdown for clients with no form (Conduit, API callers,
# anything without a socket session), and maps the form's reply into the
# tool result. No SDK or Open WebUI imports so the head-slice suites reach it.
# Open WebUI's `__event_call__` targets the one sid that sent the message,
# and the browser drops the frame without an ack unless that tab has the
# chat open, so another tab or device never sees the form (2026-09-18).
# ---------------------------------------------------------------------------

_ASK_USER_TOOL = "mcp__ask-user__ask_user"
_ASK_USER_MAX_QUESTIONS = 4
_ASK_USER_MAX_OPTIONS = 3
# The event carries no `timeout_ms`: Open WebUI 0.11.3 starts the form's
# countdown only for a positive number, and its expiry is the same cancel
# path as the Cancel button, so it dismissed a form the user was still
# typing into. The wait below is a backstop for a form that can never
# answer — unmounted by a chat switch or reload, which sends no cancel and
# hung the turn forever (seen 2026-09-03). The server's own bound
# (WEBSOCKET_EVENT_CALLER_TIMEOUT, unset = forever) cuts the wait first if
# it is shorter; the outcome is the same either way.
_ASK_USER_WAIT_MINUTES = 30
_ASK_USER_WAIT_MINUTES_MAX = 240
# The web client renders the event only if that chat is open and the reply
# message is already in its local store; otherwise it drops it silently and
# never acknowledges (a form sent the same second the chat was re-opened
# was lost, 2026-09-17). Re-sending the same event resets the same form, so
# a periodic re-send is what gets a dropped form in front of the user. Each
# re-send re-enumerates the user's sessions, so a tab or device opened after
# the form first fired gets it on the next re-arm.
_ASK_USER_REARM_SECONDS = 60
_ASK_USER_REARM_SECONDS_MIN = 10
_ASK_USER_REARM_SECONDS_MAX = 600
_ASK_USER_TIMED_OUT = "Event call timed out: the form never answered."
_ASK_USER_UNANSWERED_INSTRUCTION = (
    "The user did not answer. Proceed on your best assumption and say "
    "which one you took."
)
_ASK_USER_LOST_INSTRUCTION = (
    "The form was lost before the user answered (chat switched, page "
    "reloaded, or the wait ran out). Put the ask_in_reply text in your "
    "reply verbatim, then end the turn without doing the work; the user's "
    "next message carries the answers."
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


def _user_input_payload(questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "request:user_input",
        "data": {
            "questions": questions,
            "allow_other": any(q["allow_other"] for q in questions),
        },
    }


def _ask_user_wait_seconds(wait_minutes: Any) -> float:
    if not isinstance(wait_minutes, int) or not 1 <= wait_minutes <= _ASK_USER_WAIT_MINUTES_MAX:
        wait_minutes = _ASK_USER_WAIT_MINUTES
    return wait_minutes * 60.0


def _ask_user_rearm_seconds(rearm_seconds: Any) -> float:
    """0 disables the re-send; anything else is clamped into the bounds."""
    if not isinstance(rearm_seconds, int) or isinstance(rearm_seconds, bool):
        rearm_seconds = _ASK_USER_REARM_SECONDS
    if rearm_seconds <= 0:
        return 0.0
    return float(min(max(rearm_seconds, _ASK_USER_REARM_SECONDS_MIN), _ASK_USER_REARM_SECONDS_MAX))


def _ask_output_settles(output: Any, others_pending: bool) -> bool:
    """Whether one send's outcome ends the wait. A server-side timeout on
    one send says nothing about a later re-send still in flight
    (WEBSOCKET_EVENT_CALLER_TIMEOUT below the valve ages the first call out
    before the pipe's own wait does), so it is ignored while others remain;
    every other outcome — answers, cancel, a dead session — is final."""
    if others_pending and isinstance(output, dict):
        reason = str(output.get("error") or "")
        if reason and "timed out" in reason.lower():
            return False
    return True


def _task_output(task: "asyncio.Task") -> Any:
    try:
        return task.result()
    except asyncio.CancelledError:
        return {"error": _ASK_USER_TIMED_OUT}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _ask_with_rearm(
    event_call: Callable, payload: Dict[str, Any],
    wait_seconds: float, rearm_seconds: float,
) -> Any:
    """Send the form, re-send it every rearm_seconds until something
    answers, and give up at wait_seconds with the timed-out error shape.
    Every send still in flight is cancelled before returning, so no socket
    call outlives the tool."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    tasks: List["asyncio.Task"] = [asyncio.ensure_future(event_call(payload))]
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"error": _ASK_USER_TIMED_OUT}
            pending = [t for t in tasks if not t.done()]
            if not pending:
                if not rearm_seconds:
                    return {"error": _ASK_USER_TIMED_OUT}
                pending = [asyncio.ensure_future(event_call(payload))]
                tasks.append(pending[0])
            timeout = min(remaining, rearm_seconds) if rearm_seconds else remaining
            done, _ = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            # Two sends can land in one batch; `done` is a set, so a stale
            # timeout must never win over the answers sitting next to it.
            outputs = [_task_output(t) for t in done]
            for output in outputs:
                if _ask_output_settles(output, True):
                    return output
            if outputs and not any(not t.done() for t in tasks):
                return outputs[0]
            # Every cancelled send leaves its ack callback registered in
            # python-socketio's manager until the client acks or disconnects
            # (socketio/async_server.py `call`, base_manager.py), so each
            # re-send costs one entry per live session for the life of that
            # session: ~30 per session per unanswered form at the defaults,
            # cleared on disconnect. That is why the interval floor is 10 s
            # and the default 60 s.
            if not done and rearm_seconds:
                tasks.append(asyncio.ensure_future(event_call(payload)))
    finally:
        leftover = [t for t in tasks if not t.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)


async def _fan_out_call(
    list_sids: Callable[[], List[str]], send_to: Callable, payload: Dict[str, Any],
    origin_sid: Optional[str] = None,
) -> Any:
    """Send one event to every sid at once and return the first reply that
    is not an error. An error from the originating sid is final: a client
    with no form (Conduit) acks the event with an error at once, and the
    turn must fall through to asking in text instead of waiting on tabs
    that were never looking. The same error from any other sid says nothing
    about the one the user is typing in, so it is ignored while others
    pend; all-failed returns the origin's error, else the first in sid
    order. No sids at all is not a verdict either: every session may be
    mid-reconnect, so the call parks until the re-arm loop cancels it."""
    sids = list(list_sids() or [])
    tasks: List["asyncio.Task"] = [
        asyncio.ensure_future(send_to(sid, payload)) for sid in sids
    ]
    by_sid = dict(zip(sids, tasks))
    try:
        if not tasks:
            await asyncio.Event().wait()
        while True:
            pending = [t for t in tasks if not t.done()]
            if not pending:
                break
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                output = _task_output(task)
                if not isinstance(output, dict):
                    continue
                if not output.get("error") or task is by_sid.get(origin_sid):
                    return output
        origin = by_sid.get(origin_sid)
        ordered = ([origin] if origin is not None else []) + tasks
        for task in ordered:
            output = _task_output(task)
            if isinstance(output, dict) and output.get("error"):
                return output
        return _task_output(tasks[0])
    finally:
        leftover = [t for t in tasks if not t.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)


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
    """Turn the form's reply into the tool result. A cancel means the user
    saw the form and declined → `unanswered`. A timeout means the form was
    unmounted before it could answer (the form itself never expires) →
    `lost`, with the questions rendered for the reply. Any other error
    means the form never reached them — a client that holds a socket
    session but has no form (Conduit answers the event with "Invalid user
    input request.", verified 2026-09-02), or a dropped session — so the
    questions fall through to the no-form path."""
    if not isinstance(output, dict):
        return {"status": "unanswered", "reason": "no response",
                "instruction": _ASK_USER_UNANSWERED_INSTRUCTION}
    if output.get("error"):
        reason = str(output["error"])
        if "timed out" in reason.lower():
            return {"status": "lost", "reason": reason,
                    "ask_in_reply": _render_questions_markdown(questions),
                    "instruction": _ASK_USER_LOST_INSTRUCTION}
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


