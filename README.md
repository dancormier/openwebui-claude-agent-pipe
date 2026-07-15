# Claude Code pipe (installed copy)

The Open WebUI pipe function running as `claude_code`, kept here for durability.
Base: [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code) commit `5bbc1fc` + 3 local patches:

1. **no-KB crash fix** — `_build_kb_mcp_server` returned a 2-tuple on the
   no-knowledge path; callers unpack 3 (`return None, [], {}`). Not filed upstream (Dan's call).
2. **Multi-model** — `MODELS` valve exposes extra Claude models as separate
   picker entries; `_resolve_model()` maps the invoked id per request.
3. **`INLINE_TOOL_DETAILS` valve** — set False to suppress inline `<details>`
   HTML (mobile clients render it raw); tool activity still flows via status events.

To redeploy after editing: update the function content via the admin API
(see vault: Projects/Revised home AI hub plan/Implementation Plan, Task 3.2)
or paste into Admin → Functions. Valve state lives in webui.db, not here.
