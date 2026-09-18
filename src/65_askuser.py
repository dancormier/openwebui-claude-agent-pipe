
def _fan_out_event_call(
    user_id: Optional[str], chat_id: Optional[str], message_id: Optional[str],
    fallback: Optional[Callable], origin_sid: Optional[str] = None,
) -> Optional[Callable]:
    """An event_call that reaches every live session of the user instead of
    the one sid Open WebUI's `__event_call__` is bound to. Falls back to the
    given call outside Open WebUI (head-slice tests) or without the ids the
    frame needs."""
    if not (user_id and chat_id and message_id):
        return fallback
    try:
        from open_webui.socket.main import sio, SESSION_POOL, get_session_ids_by_user_id
        from open_webui.env import WEBSOCKET_EVENT_CALLER_TIMEOUT
    except ImportError as exc:
        log.debug("ask_user fan-out unavailable, using the single-sid call: %s", exc)
        return fallback
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

    async def event_call(payload: Dict[str, Any]) -> Any:
        return await _fan_out_call(list_sids, send_to, payload, origin_sid)

    return event_call


def _build_ask_user_mcp_server(
    event_call: Optional[Callable],
    wait_minutes: int = _ASK_USER_WAIT_MINUTES,
    rearm_seconds: int = _ASK_USER_REARM_SECONDS,
):
    """Return (mcp_config, tool_names) for the ask_user tool. Registered even
    without an event_call: the tool then hands the questions back as markdown
    for the agent to ask in its reply, so clients with no form still get
    asked instead of guessed at."""

    @tool(
        "ask_user",
        (
            "Ask the user 1-4 questions and get the answers back in this "
            "same turn. Call this whenever you would otherwise end your "
            "reply by asking the user something: a missing detail, a choice "
            "between approaches, a preference. Bundle every open question "
            "into one call. Give 2-3 options for a choice (short label, your "
            "recommendation and its reason in that option's description); "
            "give no options for an open-ended detail like a name or a "
            "time, and the user gets a text field."
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
                                    "detail (the user gets a text field)"
                                ),
                                "maxItems": _ASK_USER_MAX_OPTIONS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
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
        if event_call is None:
            result = _no_ui_result(questions)
        else:
            payload = _user_input_payload(questions)
            output = await _ask_with_rearm(
                event_call, payload,
                _ask_user_wait_seconds(wait_minutes),
                _ask_user_rearm_seconds(rearm_seconds),
            )
            if isinstance(output, dict) and output.get("error"):
                log.warning("ask_user form not answered: %s", output["error"])
            result = _map_user_input_response(output, questions)
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
    "as text. It shows a multiple-choice form and returns the answers in the "
    "same turn, so you can finish the work without another round trip. "
    "Bundle every open question into one call: at most 4 questions; 2-3 "
    "options for a choice, with your recommendation and its reason in that "
    "option's description, or no options for an open-ended detail like a "
    "name or a time, which gives the user a text field. When the stakes are "
    "low, prefer a sensible assumption you state over any question at all; "
    "but when you do ask, ask through the tool. One exception: a "
    "confirmation the rules require before a risky action stays a plain "
    "text question that ends the turn and waits for an explicit yes. If the "
    "tool result's status is `no_ui` or `lost`, put its `ask_in_reply` text "
    "in your reply verbatim and end the turn; the next user message carries "
    "the answers. If the status is `unanswered`, the user cancelled the form: "
    "proceed on your best assumption and say which you took."
)

