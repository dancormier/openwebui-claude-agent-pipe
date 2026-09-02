from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIJSONDecodeError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

log = logging.getLogger(__name__)

# OpenWebUI calls pipe() fresh for each chat turn. We keep a chat_id -> session_id
# map in-process so follow-up turns resume the same Claude Code session.
_chat_sessions: Dict[str, str] = {}


