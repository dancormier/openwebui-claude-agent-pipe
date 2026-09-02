# openwebui-claude-agent-pipe

An [Open WebUI](https://github.com/open-webui/open-webui) pipe function that
runs each chat turn as a headless [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
session — Claude Code's agent loop, with a real working directory, behind a
normal chat UI. Installs as the `claude_code` function.

Fork of [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code)
(MIT) at commit `5bbc1fc`, which has been dormant since; this repo carries the
patches below on top of it. Split out of the `homelab` repo's `hub/pipe/` on
2026-09-02 with its history.

## Files

- `claude_agent_pipe.py` — the function. Single file by design: it deploys by
  being posted to Open WebUI's admin API (or pasted into Admin → Functions).
- `redact_stdin.py` — a CLI over the pipe's own secret redactor, for job
  workers that deliver agent output outside the chat stream. It slices the
  redactor out of `claude_agent_pipe.py` at the SDK import line, so the two
  redaction boundaries share one implementation.
- `test_*.py` — standalone suites, stdlib only, no pytest: `python3 test_sessions.py`
  (optionally against a deployed copy: `python3 test_sessions.py <pipe.py>`).

## Setup-specific defaults

Two valves default to one particular machine's layout and are the first things
another deployment changes: `GATEWAY_CONTRACT` (`~/homelab/hub/AGENTS.md`, the
rules file injected into every turn) and the `REPO_MAP` example paths in its
description. Everything else is generic Open WebUI / SDK configuration.

## Patches on top of upstream

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

Note: repo-rooted chats (#repo:) don't surface files created in the repo cwd as chat attachments — the artifact scanner deliberately walks only the scratch workdir and /tmp.

Note: Hook verification: homelab's `scripts/verify-pipe-hooks.sh` proves the deny-secret-exfil PreToolUse hook fires for pipe-like headless runs and that no Home Assistant actuation connector tool is callable in them (the `permissions.deny` list in `claude/settings.json` matches exact tool names, so a renamed connector would otherwise un-deny them silently).

## Deploying

Merging here deploys nothing: Open WebUI runs the copy stored in `webui.db`.
The deploy tooling lives in the `homelab` repo, which reads this checkout via
`PIPE_DIR` (default `~/Developer/openwebui-claude-agent-pipe`):
`source ~/.secrets && ~/homelab/scripts/deploy-pipe.sh` backs up the deployed
copy, posts this checkout's copy, verifies parity, and runs the suites against
the deployed copy. Or paste into Admin → Functions. Valve state lives in
`webui.db`, not here — homelab's `scripts/set-repo-map.sh` updates `REPO_MAP`.

Known-value redaction (2026-08-21): the patterns are prefix-anchored and cannot
recognise a uuid token, an ntfy topic or a password, so the two job workers and
`slack-reply.sh` call `redact_stdin.py --known-from-tpl <secrets.tpl>`
inside a subshell that sourced `~/.secrets`, and every live value (plus its
base64/hex/URL-encoded forms) is scrubbed by value. That path slices this *checkout's* copy of the pipe, so a pattern added here
protects the workers as soon as the checkout is pulled, but the gateway stream
only after a redeploy.
