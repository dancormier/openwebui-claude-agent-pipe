# Open WebUI reports per-turn events (an image it generated, a failed tool)
# as <context> blocks in the system message. A resumed CLI session keeps the
# system prompt from its first turn, so a notice left there goes stale on
# every later turn and a new one never arrives; they ride the user prompt.
_TURN_CONTEXT_RE = re.compile(r"<context>.*?</context>", re.DOTALL)


def _system_texts(body: Dict[str, Any]) -> List[str]:
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    return parts


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    """Collect `role=system` content from body.messages, minus the per-turn
    <context> blocks. OpenWebUI merges the Workspace Model's configured
    system prompt into messages[0] before the pipe is called
    (payload.py:apply_system_prompt_to_body)."""
    parts = [_TURN_CONTEXT_RE.sub("", p).strip() for p in _system_texts(body)]
    merged = "\n\n".join(p for p in parts if p)
    return merged or None


def _extract_turn_context(body: Dict[str, Any]) -> List[str]:
    return [
        m.group(0)
        for p in _system_texts(body)
        for m in _TURN_CONTEXT_RE.finditer(p)
    ]


