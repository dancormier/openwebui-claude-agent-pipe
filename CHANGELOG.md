# Changelog

Patches on top of [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code)
commit `5bbc1fc`, in the order they landed. Numbering matches the pipe's own
comments and the homelab PRs that introduced them.

## Unreleased (2026-09-02)

- Subagent visibility: a `Task` call registers a subagent; its tool calls
  show as `↳ <agent> · 🔧 …` in the status line and heartbeat; its result
  closes it with `✅ <agent> · N tools · Ns`; the Done line counts
  subagents. Subagent text deltas no longer stream into the main reply
  (they are the report back to the main thread, not the answer).
- `_pipe_stream`'s per-message handling moved into `src/35_turn.py`: a
  `_TurnState` plus pure handlers (`_on_stream_event`, `_on_tool_use`,
  `_on_tool_result`, `_session_status`, `_done_line`, …) that return
  `(chunks, status)`, covered by `test_turn.py`. The usage publish is
  `Pipe._record_usage`. Rendering is unchanged; the tool-preview helpers moved
  above the SDK import so the head-slice suites reach them.
- Source split into `src/` modules assembled by `build.py`; the built file is
  unchanged apart from the artifact-extension constants moving next to their
  users, and the slice line the redaction consumers depend on is preserved.
- Split into its own repository with history; `GATEWAY_CONTRACT_PATH` defaults
  to empty and the docs no longer assume the author's machine.
- `requirements:` floor raised to `claude-agent-sdk>=0.2.116`, the version the
  suites run against.

## Patches

1. **no-KB crash fix** — `_build_kb_mcp_server` returned a 2-tuple on the
   no-knowledge path; callers unpack 3 (`return None, [], {}`). Not filed upstream.
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
12. **Context-usage status** — the end-of-turn status reads
    `Done · 1m42s · 12 tools · context 396k/1M (40%)`. Context comes from
    `client.get_context_usage()` (the CLI's `/context` data — real
    totalTokens against the real per-model window, so Haiku's 200k and the
    1M models are both right automatically). Fallback when that call fails:
    the last main-thread API call's usage summed
    (input + cache_read + cache_creation + output) against the
    `CONTEXT_WINDOW_TOKENS` valve (the ResultMessage's own `usage` is never
    used — it re-counts the cached prefix per tool round). Duration and
    main-thread tool count ride along; `Session: context compacted` is
    surfaced when auto-compaction fires. The last-call numbers are also
    emitted as an OWUI-normalized `chat:completion` usage event for live
    listeners (community usage-display filters), **and** written directly to
    the saved message's `usage` field via
    `Chats.upsert_message_to_chat_by_id_and_message_id` (`touch=False`) —
    OWUI's socket emitter never persists `chat:completion` events, and the
    message's ⓘ info popover renders only from the saved `usage`, so without
    the DB write the popover never appears. Both paths are guarded so a
    failure can't break the turn.
13. **Effort / task-budget / fallback valves** — `EFFORT` sets the default
    effort level (empty = SDK default `high`); a message starting with
    `/effort <low|medium|high|xhigh|max>` overrides it for that turn.
    `TASK_BUDGET_TOKENS` (≥20000; 0 off) gives the model a per-turn token
    budget it sees and paces itself against — the graceful alternative to a
    turn cap while `MAX_TURNS` is 0. `FALLBACK_MODEL` retries the turn on
    another model if the primary fails or is unavailable.
