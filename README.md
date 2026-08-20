# Claude Code pipe (installed copy)

The Open WebUI pipe function running as `claude_code`, kept here for durability.
Base: [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code) commit `5bbc1fc` + 11 local patches:

1. **no-KB crash fix** — `_build_kb_mcp_server` returned a 2-tuple on the
   no-knowledge path; callers unpack 3 (`return None, [], {}`). Not filed upstream (Dan's call).
2. **Multi-model** — `MODELS` valve exposes extra Claude models as separate
   picker entries; `_resolve_model()` maps the invoked id per request.
3. **`INLINE_TOOL_DETAILS` valve** — set False to suppress inline `<details>`
   HTML (mobile clients render it raw); tool activity still flows via status events.
4. **Image attachments** — `_save_image_attachments()` writes `image_url` parts
   of the latest user message to `<workdir>/attachments/` so the SDK subprocess
   (which gets no image blocks) can `Read` them; a prompt note points at each path.
5. **History-replay fallback** — `_render_transcript()` packs prior turns into a
   `<conversation_history>` block whenever no live SDK session can be resumed:
   stateless (keyless) callers every turn, and keyed chats on a cold start
   (e.g. after an OWUI restart), which then resume warm from the new session.
6. **No cost footer** — the per-response cost/usage line is dropped; subscription
   billing makes it noise, and TTS reads it aloud in call mode.
7. **Durable sessions** — chat_id → session_id persisted to
   `WORKDIR_ROOT/.sessions/<chat_id>.json`; survives OWUI restarts and
   redeploys. In-process map is now only a cache.
8. **Keyless warm sessions** — callers without chat_id get session
   identity via a conversation-prefix fingerprint stored in
   `WORKDIR_ROOT/_sessions.json`; each keyless chat gets its own
   `anon-<uuid>` workdir (shared `default` retired). Dead resume ids retry
   the turn cold once.
   **Conduit is not always keyless** (verified 2026-08-11): it carries the
   `chat_id` for a chat started elsewhere, so a desktop → iOS handoff mid-thread
   arrives *keyed* and resumes via patch 7, workdir `~/hub/<chat_id>/`. Which
   path a turn took is visible in the workdir name — `anon-*` means keyless.
9. **Repo-aware workdir** — `#repo:<name>` on a chat's first message runs the
   chat in an allowlisted repo (`REPO_MAP` valve) instead of the scratch
   workdir, loading that repo's CLAUDE.md. Session metadata stays under
   `WORKDIR_ROOT`.
10. **Session-mode status** — each turn surfaces `Session: resumed` /
    `cold start — history replayed` / `new chat`, so degradation is visible.
11. **Mobile concision hint** — keyless callers get a system-prompt append
    steering toward short answers.

Note: repo-rooted chats (#repo:) don't surface files created in the repo cwd as chat attachments — the artifact scanner deliberately walks only the scratch workdir and /tmp.

Note: Hook verification: `scripts/verify-pipe-hooks.sh` (run on the Mini) proves the deny-secret-exfil PreToolUse hook fires for pipe-like headless runs.

To redeploy after editing: `source ~/.secrets && scripts/deploy-pipe.sh` on the
Mini — it takes the admin key from `OPENWEBUI_ADMIN_API_KEY`, or from an
explicit `$KEY` piped in from the laptop (usage in its header). It backs up the
deployed copy, posts the repo copy, verifies parity, and runs the suites
against the deployed copy. Or paste into Admin → Functions. Valve state lives in webui.db, not
here — `scripts/set-repo-map.sh` updates the REPO_MAP valve.
