
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
            payload = _user_input_payload(questions, timeout_ms)
            try:
                output = await asyncio.wait_for(
                    event_call(payload), _ask_user_wait_seconds(payload)
                )
            except asyncio.TimeoutError:
                log.warning("ask_user form timed out with no reply")
                output = {"error": "Event call timed out: the form never answered."}
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
    "tool result's status is `no_ui`, put its `ask_in_reply` text in your "
    "reply verbatim and end the turn; the next user message carries the "
    "answers. If the status is `unanswered`, proceed on your best assumption "
    "and say which you took."
)

