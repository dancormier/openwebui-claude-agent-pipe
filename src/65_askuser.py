
def _build_ask_user_mcp_server(
    event_call: Optional[Callable], timeout_ms: int = _ASK_USER_TIMEOUT_MS
):
    """Return (mcp_config, tool_names) for the ask_user tool. Registered even
    without an event_call: the tool then hands the questions back as markdown
    for the agent to ask in its reply, so clients with no form still get
    asked instead of guessed at."""

    @tool(
        "ask_user",
        (
            "Ask the user up to 4 multiple-choice questions and wait for the "
            "answers. Use only when a real ambiguity would change the work "
            "materially; otherwise decide and say what you assumed. Each "
            "option needs a short label; put your recommendation and its "
            "reason in that option's description."
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
                                "minItems": _ASK_USER_MIN_OPTIONS,
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
                        "required": ["question", "options"],
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
            try:
                output = await event_call(_user_input_payload(questions, timeout_ms))
            except Exception as exc:
                log.warning("ask_user event_call failed: %s", exc)
                output = {"error": f"{type(exc).__name__}: {exc}"}
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
    "You have an `ask_user` tool that shows the user a short multiple-choice "
    "form and waits for the answers. Use it only when a genuine ambiguity "
    "would waste a long turn or lead to materially different work; for "
    "routine judgment calls, decide and say what you assumed. One call per "
    "turn, at most 4 questions, 2-3 options each, your recommendation and "
    "its reason in that option's description. It is not an approval "
    "channel: a confirmation the rules require still ends the turn as a "
    "plain question and waits for an explicit yes. If the result's status "
    "is `no_ui`, put its `ask_in_reply` text in your reply verbatim and end "
    "the turn; the next user message carries the answers. If the status is "
    "`unanswered`, proceed on your best assumption and say which you took."
)

