_TASK_TIMEOUT_SECONDS = 60

_ANSWERED_TASKS = ("title_generation", "tags_generation", "follow_up_generation")

_TASK_SYSTEM_PROMPT = (
    "You fill in one background field for a chat app. Follow the instructions "
    "in the message exactly and reply with only the JSON they ask for."
)


def _task_prompt(task: Optional[str], body: Dict[str, Any]) -> str:
    """The prompt to answer for an Open WebUI background task, or "" to answer
    nothing. Open WebUI puts its whole task template, chat history included,
    in the last user message. Other tasks (web-search queries, autocomplete,
    emoji, ...) get an empty reply: each falls back to its own default, which
    costs nothing and is safe."""
    if task not in _ANSWERED_TASKS:
        return ""
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            return _message_text(message).strip()
    return ""
