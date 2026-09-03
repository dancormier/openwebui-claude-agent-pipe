_TOOL_PREVIEW_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Task": "description",
    "Agent": "description",
}

_TOOL_PREVIEW_CUSTOM = {_ASK_USER_TOOL: _ask_user_preview}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    custom = _TOOL_PREVIEW_CUSTOM.get(name)
    if custom:
        raw = custom(tool_input)
    elif key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    # Collapse to one line — multi-line values break inline-code spans and leak
    # "# python comments" into markdown as H1 headings.
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated + (" …" if "\n" in raw and not truncated.endswith("…") else "")


_FENCE_LANG_PER_TOOL = {
    "Bash": "bash",
    "Glob": "text",
    "Grep": "text",
    "WebSearch": "text",
    "WebFetch": "text",
}


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    """Full tool invocation as fenced code block(s). If the tool has a known
    primary field (Bash→command, Read→file_path, …), render that with the
    right syntax highlight and append any other fields as a small JSON block.
    Tools with no known primary render as a single JSON block.
    """
    if not tool_input:
        return "```\n(no input)\n```"
    primary = _TOOL_PREVIEW_FIELDS.get(name)
    if primary and primary in tool_input:
        lang = _FENCE_LANG_PER_TOOL.get(name, "text")
        parts = [f"```{lang}\n{tool_input[primary]}\n```"]
        others = {k: v for k, v in tool_input.items() if k != primary}
        if others:
            parts.append(
                f"```json\n{json.dumps(others, indent=2, ensure_ascii=False)}\n```"
            )
        return "\n\n".join(parts)
    return f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"


def _format_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(text if isinstance(text, str) else repr(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class _TurnState:
    """Bookkeeping for one chat turn's message stream, shared by the handlers
    below so each can be a plain function of (message fields, state)."""

    def __init__(self) -> None:
        # Thinking deltas buffered per content-block index and emitted as one
        # <details> block at content_block_stop: CommonMark ends an HTML block
        # at a blank line, so streaming the opener token-by-token strands it
        # as literal text once the thinking has a paragraph break.
        self.thinking_buffers: Dict[int, str] = {}
        # Whether any assistant text has streamed yet; consecutive text blocks
        # (one narration beat per tool round) get a paragraph break between.
        self.text_emitted = False
        # Tool calls seen since the last text block started. A text block that
        # follows tool activity is a new beat — the final reply is always one —
        # and gets a rule so the reader can find where the reply resumes.
        self.tools_since_text = 0
        # Running tools keyed by tool_use_id, for the heartbeat status line.
        self.active_tools: Dict[str, Dict[str, Any]] = {}
        # Usage of the latest main-thread API call. Its field sum is the live
        # context size; ResultMessage.usage re-counts the cached prefix per
        # tool round and cannot be used for that.
        self.last_usage: Optional[Dict[str, Any]] = None
        self.tool_count = 0
        self.turn_session_id: Optional[str] = None
        # Running subagents keyed by their Task tool_use_id — the id every
        # message they produce carries as parent_tool_use_id.
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.agent_count = 0

    def note_assistant(
        self, main_thread: bool, usage: Optional[Dict[str, Any]], tool_uses: int
    ) -> None:
        # Subagent messages (parent_tool_use_id set) run in their own context
        # windows, so only main-thread usage and tool counts describe this
        # session.
        if not main_thread:
            return
        if usage:
            self.last_usage = usage
        self.tool_count += tool_uses
        self.tools_since_text += tool_uses


_Handled = Tuple[List[str], Optional[str]]


def _thinking_block(text: str) -> str:
    return (
        "\n\n<details>\n<summary>💭 Thinking</summary>\n\n"
        f"{text}\n\n"
        "</details>\n\n"
    )


# A markdown rule: the one delimiter both Open WebUI and Conduit render.
_BEAT_SEPARATOR = "\n\n---\n\n"


def _on_stream_event(ev: Dict[str, Any], state: _TurnState, inline_details: bool) -> _Handled:
    """Token-level stream event → (chunks to yield, status to emit)."""
    chunks: List[str] = []
    status: Optional[str] = None
    etype = ev.get("type")
    if etype == "message_start":
        state.thinking_buffers.clear()
    elif etype == "content_block_start":
        block = ev.get("content_block") or {}
        if block.get("type") == "thinking":
            state.thinking_buffers[ev.get("index", 0)] = ""
            status = "💭 Thinking…"
        elif block.get("type") == "text":
            if state.text_emitted and state.tools_since_text:
                chunks.append(_BEAT_SEPARATOR)
            elif state.text_emitted:
                chunks.append("\n\n")
            state.tools_since_text = 0
    elif etype == "content_block_delta":
        delta = ev.get("delta") or {}
        dt = delta.get("type")
        if dt == "text_delta":
            text = delta.get("text", "")
            if text:
                state.text_emitted = True
                chunks.append(text)
        elif dt == "thinking_delta":
            idx = ev.get("index", 0)
            if idx in state.thinking_buffers:
                state.thinking_buffers[idx] += delta.get("thinking", "")
        # signature_delta / input_json_delta: ignore. Tool input is rendered
        # once, fully, from the AssistantMessage.
    elif etype == "content_block_stop":
        idx = ev.get("index", 0)
        if idx in state.thinking_buffers:
            text = state.thinking_buffers.pop(idx).strip()
            if text and inline_details:
                chunks.append(_thinking_block(text))
            elif text:
                status = "💭 thinking"
    return chunks, status


# Claude Code renamed its delegation tool from Task to Agent (2.1.x); the
# SDK surfaces whichever name the installed CLI uses.
_DELEGATION_TOOLS = ("Task", "Agent")


def _agent_label(tool_input: Dict[str, Any]) -> str:
    kind = str(tool_input.get("subagent_type") or "agent")
    desc = str(tool_input.get("description") or "").strip()
    return f"{kind}: {desc}" if desc else kind


def _on_tool_use(
    name: str,
    tool_input: Dict[str, Any],
    tool_id: str,
    state: _TurnState,
    inline_details: bool,
    now: float,
    parent_id: Optional[str] = None,
) -> _Handled:
    """A completed tool call → status line, heartbeat registration, and the
    collapsed input block. A delegation call registers a subagent; calls made by
    a subagent (parent_id set) are labeled under it. The <summary> is plain
    text on purpose: Open WebUI strips inline HTML inside it and escapes its
    content itself, so neither tags nor pre-escaping survive."""
    preview = _tool_preview(name, tool_input)
    label = f"{name}: {preview}" if preview else name
    if name in _DELEGATION_TOOLS and parent_id is None:
        state.agents[tool_id] = {
            "label": _agent_label(tool_input), "tools": 0, "started": now,
        }
        state.agent_count += 1
        label = f"🤖 {state.agents[tool_id]['label']}"
    agent = state.agents.get(parent_id) if parent_id else None
    if agent:
        agent["tools"] += 1
        label = f"↳ {agent['label']} · {label}"
    state.active_tools[tool_id] = {"label": label, "started": now}
    chunks: List[str] = []
    if inline_details:
        summary_text = f"🔧 {name}" + (f" · {preview}" if preview else "")
        if agent:
            summary_text = f"↳ {agent['label']} · " + summary_text
        chunks.append(
            "\n\n<details>\n"
            f"<summary>{summary_text}</summary>\n\n"
            f"{_tool_input_block(name, tool_input)}\n\n"
            "</details>\n\n"
        )
    return chunks, f"🔧 {label}"


def _on_tool_result(
    tool_use_id: str,
    is_error: bool,
    content: Any,
    state: _TurnState,
    inline_details: bool,
    now: Optional[float] = None,
) -> _Handled:
    """A tool result → heartbeat release; errors render quietly, since the
    agent usually retries and recovers. The result of a `Task` call closes
    that subagent with a one-line summary."""
    started = (state.active_tools.pop(tool_use_id, None) or {}).get("started")
    agent = state.agents.pop(tool_use_id, None)
    if agent and not is_error:
        elapsed = int(now - agent["started"]) if now is not None else 0
        return [], (
            f"✅ {agent['label']} · {agent['tools']} tool"
            f"{'s' if agent['tools'] != 1 else ''} · {elapsed}s"
        )
    if not is_error:
        return [], None
    if not inline_details:
        return [], "⚙️ tool hiccup (retrying)"
    err_text = _format_tool_result(content)[:800]
    return [
        "\n\n<details>\n<summary>"
        "<sub>⚙️ tool hiccup (retrying)</sub>"
        "</summary>\n\n"
        f"```\n{err_text}\n```\n\n"
        "</details>\n\n"
    ], None


def _session_status(resumed: bool, history: List[Dict[str, Any]]) -> str:
    if resumed:
        return "Session: resumed"
    if _history_fingerprint(history) != _EMPTY_FP:
        return "Session: cold start — history replayed"
    return "Session: new chat"


def _heartbeat_label(active_tools: Dict[str, Dict[str, Any]], now: float) -> Tuple[str, int]:
    oldest = min(active_tools.values(), key=lambda t: t["started"])
    elapsed = int(now - oldest["started"])
    if len(active_tools) == 1:
        return oldest["label"], elapsed
    return f"{len(active_tools)} tools · longest {oldest['label']}", elapsed


def _context_from_usage(cu: Dict[str, Any]) -> str:
    """The CLI's own /context numbers: totalTokens against the raw model window."""
    used = int(cu.get("totalTokens") or 0)
    window = int(cu.get("rawMaxTokens") or 0)
    if not (used and window):
        return ""
    pct = round(100 * used / window)
    return f"context {_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct}%)"


def _done_line(
    duration_ms: Optional[int], tool_count: int, ctx: str, agents: int = 0
) -> str:
    parts: List[str] = []
    if duration_ms:
        parts.append(_fmt_duration(duration_ms))
    if tool_count:
        parts.append(f"{tool_count} tool{'s' if tool_count != 1 else ''}")
    if agents:
        parts.append(f"{agents} subagent{'s' if agents != 1 else ''}")
    if ctx:
        parts.append(ctx)
    return "Done · " + " · ".join(parts) if parts else "Done."

