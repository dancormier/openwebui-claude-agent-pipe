
def _fan_out_event_call(
    user_id: Optional[str], chat_id: Optional[str], message_id: Optional[str],
    fallback: Optional[Callable], origin_sid: Optional[str] = None,
) -> Optional[Callable]:
    """A factory of event_calls that reach every live session of the user
    instead of the one sid Open WebUI's `__event_call__` is bound to. Each
    ask takes a fresh call from it, so the once-per-session bookkeeping
    (which sids already hold the form) starts empty per form. Falls back to
    the given call outside Open WebUI (head-slice tests) or without the ids
    the frame needs, wrapped the same way so even that sid is sent the form
    once."""
    if fallback is not None:
        origin = origin_sid or "origin"

        async def send_fallback(sid: str, payload: Dict[str, Any]) -> Any:
            return await fallback(payload)

        def make_fallback() -> Callable:
            return _once_per_session_call(lambda: [origin], send_fallback, origin)

    if not (user_id and chat_id and message_id):
        return make_fallback if fallback is not None else None
    try:
        from open_webui.socket.main import sio, SESSION_POOL, get_session_ids_by_user_id
        from open_webui.env import WEBSOCKET_EVENT_CALLER_TIMEOUT
    except ImportError as exc:
        log.debug("ask_user fan-out unavailable, using the single-sid call: %s", exc)
        return make_fallback if fallback is not None else None
    timeout_errors: Tuple[type, ...] = (TimeoutError,)
    try:
        import socketio

        timeout_errors = (TimeoutError, socketio.exceptions.TimeoutError)
    except (ImportError, AttributeError):
        pass

    def list_sids() -> List[str]:
        # get_session_ids_by_user_id can name sids the pool has already
        # dropped; Open WebUI's own caller re-checks ownership the same way.
        live = sorted(
            sid for sid in set(get_session_ids_by_user_id(user_id))
            if (SESSION_POOL.get(sid) or {}).get("id") == user_id
        )
        if origin_sid in live:
            live.remove(origin_sid)
            live.insert(0, origin_sid)
        return live

    async def send_to(sid: str, payload: Dict[str, Any]) -> Any:
        try:
            return await sio.call(
                "events",
                {"chat_id": chat_id, "message_id": message_id, "data": payload},
                to=sid, timeout=WEBSOCKET_EVENT_CALLER_TIMEOUT,
            )
        except timeout_errors:
            return {"error": "Event call timed out. The browser tab may be inactive or closed."}

    def make_event_call() -> Callable:
        return _once_per_session_call(list_sids, send_to, origin_sid)

    return make_event_call


def _build_ask_user_mcp_server(
    make_event_call: Optional[Callable[[], Callable]],
    wait_minutes: int = _ASK_USER_WAIT_MINUTES,
    rearm_seconds: int = _ASK_USER_REARM_SECONDS,
    inflight: Optional[_InflightTurn] = None,
    shown_chars: Optional[Callable[[], int]] = None,
):
    """Return (mcp_config, tool_names) for the ask_user tool. Registered even
    without an event_call factory: the tool then hands the questions back as
    markdown for the agent to ask in its reply, so clients with no form still
    get asked instead of guessed at. With the turn's in-flight entry, a
    message typed into the chat while the form is pending answers it."""

    @tool(
        "ask_user",
        (
            "Ask the user 1-3 questions and get the answers back in this "
            "same turn. Call this whenever you would otherwise end your "
            "reply by asking the user something: a missing detail, a choice "
            "between approaches, a preference. Bundle every open question "
            "into one call. Give 2-3 options for a choice (short label, your "
            "recommendation and its reason in that option's description) "
            "and the user gets a form to tap. Give no options for an "
            "open-ended detail like a name or a time: the questions are then "
            "printed in your reply for a typed answer. The user sees only "
            "the questions, so write what the answer depends on in your "
            "reply before calling."
        ),
        {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _ASK_USER_MAX_QUESTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "header": {
                                "type": "string",
                                "description": "Tab title, a few words",
                            },
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "description": (
                                    "2-3 choices; omit for an open-ended "
                                    "detail (asked in the reply, no form)"
                                ),
                                "maxItems": _ASK_USER_MAX_OPTIONS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {
                                            "type": "string",
                                            "description": (
                                                "One line: your recommendation "
                                                "and its reason, or what picking "
                                                "this means"
                                            ),
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "allow_other": {
                                "type": "boolean",
                                "description": "Offer a free-text answer (default true)",
                            },
                        },
                        "required": ["question"],
                    },
                }
            },
            "required": ["questions"],
        },
    )
    async def _ask(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            questions = _normalize_questions(args.get("questions"))
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": f"ask_user: {exc}"}],
                "is_error": True,
            }
        started = time.monotonic()
        if make_event_call is None:
            result = _no_ui_result(questions)
        elif _needs_typing(questions):
            result = _in_reply_result()
        else:
            payload = _user_input_payload(questions)
            typed = None
            if inflight is not None:
                typed = asyncio.get_running_loop().create_future()
                inflight.pending_ask = typed
            try:
                output = await _ask_with_rearm(
                    make_event_call(), payload,
                    _ask_user_wait_seconds(wait_minutes),
                    _ask_user_rearm_seconds(rearm_seconds),
                    typed=typed,
                )
            finally:
                if inflight is not None:
                    inflight.pending_ask = None
            if isinstance(output, dict) and output.get("error"):
                log.warning("ask_user form not answered: %s", output["error"])
            result = _map_user_input_response(output, questions)
        if shown_chars is not None:
            # The tool call can reach here before the turn loop has drained
            # the text deltas that preceded it; a result that came back
            # without waiting on a form needs the loop to catch up first.
            if time.monotonic() - started < 0.2:
                await asyncio.sleep(0.2)
            result = _with_shown_text(result, shown_chars())
        log.info(
            "ask_user %s%s after %.0fs",
            result["status"],
            f" ({result['reason']})" if result.get("reason") else "",
            time.monotonic() - started,
        )
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ]
        }

    server = create_sdk_mcp_server("ask-user", "0.1", tools=[_ask])
    # Claude Code 2.1 defers MCP tools behind ToolSearch, so without this
    # the model sees only the tool's name and never reads its description;
    # alwaysLoad is documented for stdio/http servers only but the CLI
    # honours it on sdk servers too (verified 2026-09-02, CLI 2.1.258).
    return {**server, "alwaysLoad": True}, [_ASK_USER_TOOL]


_ASK_USER_PROMPT = (
    "Asking the user something: whenever your reply would end with a "
    "question for the user - a missing detail, a choice between approaches, "
    "a preference - call the `ask_user` tool instead of writing the question "
    "as text. The user sees only the questions and options you pass to the "
    "tool, so first write, as normal reply text, everything the answer "
    "depends on: what you found, the draft, the trade-off. Text you have "
    "not output is not on their screen; never refer to it as 'above'. When "
    "every question has options, the tool shows a multiple-choice form and "
    "returns the answers in the same turn, so you can finish the work "
    "without another round trip. Bundle every open question into one call: "
    "at most 3 questions; 2-3 options for a choice, with your recommendation "
    "and its reason in that option's description. Give no options for an "
    "open-ended detail like a name or a time: a typed answer never goes "
    "through a form, so the tool prints the questions in your reply and "
    "returns `in_reply`; end the turn there, and the next user message "
    "carries the answers. When the stakes are "
    "low, prefer a sensible assumption you state over any question at all; "
    "but when you do ask, ask through the tool. One exception: a "
    "confirmation the rules require before a risky action stays a plain "
    "text question that ends the turn and waits for an explicit yes. If the "
    "tool result's status is `no_ui` or `lost`, put its `ask_in_reply` text "
    "in your reply verbatim and end the turn; the next user message carries "
    "the answers. If the status is `unanswered`, the user submitted the form "
    "empty: proceed on your best assumption and say which you took."
)

