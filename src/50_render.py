def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    """Collect `role=system` content from body.messages. OpenWebUI merges the
    Workspace Model's configured system prompt into messages[0] before the
    pipe is called (payload.py:apply_system_prompt_to_body)."""
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
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


