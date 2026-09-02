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

- `src/*.py` — the source, one module per concern, concatenated in filename
  order. **Edit these, never the built file.**
- `claude_agent_pipe.py` — the built function, committed because it is what
  gets deployed and what the redaction consumers slice. `python3 build.py`
  regenerates it; `python3 build.py --check` fails in CI when it is stale.
  Single file by design: Open WebUI stores a function as one body, posted to
  the admin API or pasted into Admin → Functions.
- `redact_stdin.py` — a CLI over the pipe's own secret redactor, for job
  workers that deliver agent output outside the chat stream. It slices the
  redactor out of `claude_agent_pipe.py` at the SDK import line, so the two
  redaction boundaries share one implementation.
- `test_*.py` — standalone suites, stdlib only, no pytest: `python3 test_sessions.py`
  (optionally against a deployed copy: `python3 test_sessions.py <pipe.py>`).

## Configuration

Everything is a valve (Admin → Functions → claude_code → Valves). The ones that
matter on first install:

- `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token`; this is the sanctioned
  path for running the SDK on a Claude subscription.
- `WORKDIR_ROOT` — parent directory for per-chat workspaces.
- `PERMISSION_MODE` — defaults to `bypassPermissions`; the agent runs as the
  Open WebUI process user with no prompts, so your rules file is the guardrail.
- `GATEWAY_CONTRACT_PATH` — a rules file appended to the system prompt for
  `#repo:` sessions, which otherwise load only the target repo's `CLAUDE.md`.
  Empty by default; set it if you use `REPO_MAP`.
- `REPO_MAP` — `name=path` allowlist for `#repo:<name>` chats.

## Patches on top of upstream

Thirteen so far — durable and keyless sessions, history replay, redaction,
image attachments, `#repo:` workdirs, a context-usage footer, effort and
fallback valves. Each is described in [CHANGELOG.md](CHANGELOG.md).

Tested against `claude-agent-sdk` 0.2.116 (the version installed on the
author's Open WebUI host); the function's `requirements:` line sets that as
the floor.

## Notes

- repo-rooted chats (#repo:) don't surface files created in the repo cwd as chat attachments — the artifact scanner deliberately walks only the scratch workdir and /tmp.

- Hook verification: because the agent runs with bypassPermissions, the author's setup runs a daily canary (`verify-pipe-hooks.sh` in the homelab repo) that proves the deny-secret-exfil PreToolUse hook fires for pipe-like headless runs and that no Home Assistant actuation connector tool is callable in them (the `permissions.deny` list in `claude/settings.json` matches exact tool names, so a renamed connector would otherwise un-deny them silently).

## Deploying

Merging here deploys nothing: Open WebUI runs the copy stored in `webui.db`.
Post `claude_agent_pipe.py` as the content of function `claude_code` via the
admin API (`POST /api/v1/functions/id/claude_code/update`, admin bearer token)
or paste it into Admin → Functions. Valve state lives in `webui.db`, not here,
and survives redeploys.

The author's wrapper lives in the `homelab` repo and reads this checkout via
`PIPE_DIR` (default `~/Developer/openwebui-claude-agent-pipe`): it backs up the
deployed copy, posts this one, verifies parity, and runs the suites against the
deployed copy.

Known-value redaction (2026-08-21): the patterns are prefix-anchored and cannot
recognise a uuid token, an ntfy topic or a password, so the two job workers and
`slack-reply.sh` call `redact_stdin.py --known-from-tpl <secrets.tpl>`
inside a subshell that sourced `~/.secrets`, and every live value (plus its
base64/hex/URL-encoded forms) is scrubbed by value. That path slices this *checkout's* copy of the pipe, so a pattern added here
protects the workers as soon as the checkout is pulled, but the gateway stream
only after a redeploy.
