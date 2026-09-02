# openwebui-claude-agent-pipe

An [Open WebUI](https://github.com/open-webui/open-webui) pipe function that
runs each chat turn as a headless [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
session: Claude Code's full agent loop, with a real working directory and
tools, behind a normal chat UI, billed to your Claude subscription. It is for
one person (or one trusted admin) who already runs Open WebUI and wants Claude
Code in it, on the web and on mobile clients, rather than only in a terminal.

## Features

- **Real agent loop** — Read/Write/Edit/Bash/Glob/Grep/WebSearch/WebFetch and
  subagents, in a per-chat working directory, streamed token by token.
- **Durable sessions** — each chat maps to one Claude Code session that
  survives Open WebUI restarts and function redeploys; a cold start replays
  the chat history once and resumes warm from then on.
- **Keyless clients** — OpenAI-API callers that send no chat id (mobile apps,
  voice) get a session too, fingerprinted from the conversation prefix.
- **Live status** — tool activity, a heartbeat while a tool runs, subagents
  grouped under their parent, and a `Done · 1m42s · 12 tools · context 74k/200k`
  line at the end (also written to the message's ⓘ usage popover).
- **`ask_user`** — the agent can pause and ask up to four multiple-choice
  questions through Open WebUI's own form, and get the answers in the same turn.
- **Earlier chats** — `search_chats` / `read_chat` let the agent look up the
  calling user's past conversations (sqlite deployments; read-only; scoped to
  that user).
- **Knowledge bases** — a Workspace Model's attached knowledge becomes
  `search_knowledge` / `list` / `read` / `grep` tools the agent calls itself.
- **Images and artifacts** — attached images are written to the workdir for
  the agent to read; files the agent creates come back inline or as links.
- **Repo-rooted chats** — `#repo:<name>` on a first message runs the chat
  inside an allowlisted repository, loading its `CLAUDE.md`.
- **Output redaction** — API keys, tokens and private keys are scrubbed from
  the reply stream and status events before they reach the chat database.
- **Effort, budget, fallback** — per-turn `/effort <level>`, a task token
  budget the model paces itself against, and a fallback model.

Tested on macOS 15 and Linux (the official Docker image), Open WebUI 0.11.3,
`claude-agent-sdk` 0.2.116, Claude Code CLI 2.1.258. The `requirements:` line
sets 0.2.116 as the floor; the SDK bundles the Claude Code CLI, so nothing
else installs.

## Install

1. **Get a token.** On any machine with a browser and Claude Code installed,
   run `claude setup-token` and copy the long-lived OAuth token it prints.
   This is the sanctioned way to run the Agent SDK on a Pro/Max/Team
   subscription. (An `ANTHROPIC_API_KEY` works too, pay-per-token.)
2. **Add the function.** Admin Panel → Functions → **+** → paste the contents
   of [`claude_agent_pipe.py`](claude_agent_pipe.py), id `claude_code`, any
   name, Save. Open WebUI installs `claude-agent-sdk` from the `requirements:`
   line on save (allow a minute). Or use the admin API:
   ```sh
   curl -sf -X POST http://localhost:8080/api/v1/functions/create \
     -H "Authorization: Bearer $OWUI_ADMIN_KEY" -H 'Content-Type: application/json' \
     --data-binary @<(python3 -c 'import json;print(json.dumps({"id":"claude_code","name":"Claude Code","meta":{"description":"Claude Code agent loop"},"content":open("claude_agent_pipe.py").read()}))')
   ```
3. **Set the valves.** Functions → Claude Code → ⚙ Valves:
   `CLAUDE_CODE_OAUTH_TOKEN` = the token from step 1; `WORKDIR_ROOT` = a
   directory the Open WebUI process can write (default
   `/tmp/claude-agent-pipe`; use a persistent path so sessions survive
   reboots). Read the [Security](#security) section before leaving
   `PERMISSION_MODE` at its default.
4. **Enable it** with the toggle on the Functions page, then pick
   **Claude Code** in the model picker (extra picker entries via `MODELS`).
5. **Send a message.** The status line shows `Session: new chat`, then tool
   activity, then the Done line. A second message shows `Session: resumed`.

Every valve is described in [docs/valves.md](docs/valves.md) (generated from
the code, so it is always current).

## How it works

`pipe()` is called once per turn. It resolves the working directory
(`WORKDIR_ROOT/<chat_id>`, or `anon-<uuid>` for keyless callers, or the
`#repo:` target), decides whether a Claude Code session can be resumed, and
runs the turn through `ClaudeSDKClient`, translating the SDK's message stream
into text chunks and Open WebUI status events.

- **Sessions.** The chat id → session id map is persisted to
  `WORKDIR_ROOT/.sessions/<chat_id>.json`; the in-process dict is only a
  cache. Keyless callers are fingerprinted from their conversation prefix into
  `WORKDIR_ROOT/_sessions.json` (entries expire after 30 days). A dead resume
  id is dropped and the turn is retried cold.
- **Cold starts.** When no session can be resumed, the prior turns are packed
  into a `<conversation_history>` block ahead of the prompt (last 30
  messages, 24k chars), so nothing is lost; the next turn resumes warm.
- **Tools.** Everything Claude Code has, gated by `ALLOWED_TOOLS` and
  `PERMISSION_MODE`, plus in-process MCP servers for `ask_user`, chat search,
  and knowledge. Those are registered with `alwaysLoad` because Claude Code
  2.1 otherwise hides MCP tools behind `ToolSearch` and the model never
  finds them.
- **The agent's environment.** The chat id is exported to the agent's
  subprocess as `HUB_CHAT_ID` (only when one exists), so tooling the agent
  runs can find out which chat it is serving. Nothing in this repo reads it.
- **Redaction.** Every chunk and event passes through `_redact_secrets`
  before leaving `pipe()`. A hit is logged by kind, never by value.

## Security

Read this before exposing the function to anyone but yourself.

- **`bypassPermissions` is the default.** Every user who can pick the model
  runs arbitrary code as the Open WebUI process user, on the Open WebUI host,
  with no prompts, using your OAuth token. Treat the function as a shell for
  everyone it is enabled for. Restrict it to admins in Open WebUI's model
  access controls, or tighten `PERMISSION_MODE` and `ALLOWED_TOOLS`.
- **Valves are global.** One token, one `WORKDIR_ROOT`, one `REPO_MAP` for
  every user of the function. Two users cannot read each other's chats
  (chat search is scoped by user id) but they share the filesystem and the
  subscription. This is a single-user or single-admin design.
- **`REPO_MAP` hands out paths.** Anyone who can start a `#repo:` chat runs
  the agent inside that repository. It grants nothing `bypassPermissions`
  did not already reach; it only sets the working directory.
- **`SETTING_SOURCES`** loads the host user's `~/.claude` (with `user`),
  which can define hooks that execute code. Leave it empty unless you
  control the host account.
- **What the redactor catches:** prefix-shaped credentials (Anthropic,
  OpenAI, Slack, GitHub, Google, AWS, 1Password, JWTs) and PEM private keys,
  in the reply and in status events. **What it cannot catch:** bare UUID
  tokens, passwords, hostnames, or anything that looks like prose. The
  agent can still `cat` a secret into a file the chat never sees. Keep
  credentials out of `WORKDIR_ROOT` and the mapped repos, or give the agent
  a hook (Claude Code's `PreToolUse`) that refuses to publish them.
- **Running as root** (the official Docker image does) makes the CLI refuse
  `bypassPermissions` unless `IS_SANDBOX=1`; the pipe sets it. That is the
  CLI's own sandbox flag, not an actual sandbox.

Report a vulnerability as described in [SECURITY.md](SECURITY.md).

## Open WebUI coupling

The pipe imports a few Open WebUI internals. Each import is wrapped so a
version that moved them degrades a feature instead of failing the turn:

| Import | Used for | If missing |
|---|---|---|
| `open_webui.env.DATA_DIR` | locating `webui.db` for chat search | chat search reports itself unavailable (or set `CHAT_DB_PATH`) |
| `open_webui.models.chats.Chats.upsert_message_to_chat_by_id_and_message_id` | the ⓘ usage popover | no popover; the status line still shows context |
| `open_webui.models.files`, `open_webui.storage.provider.Storage` | artifact upload | files the agent creates are not linked |
| `open_webui.models.knowledge`, `open_webui.models.users`, `open_webui.retrieval.utils.query_collection`, `open_webui.main.app` | knowledge tools | the tools answer "unavailable" |

Chat search reads the sqlite `chat` table directly and is off by construction
on Postgres deployments (`DATABASE_URL` starting with `postgres` makes the
tools report unavailable).

Claude Code CLI coupling: `alwaysLoad` on SDK MCP servers (2.1.x), the
subagent tool being named `Task` or `Agent` (both handled), and `ToolSearch`
deferral. `test_turn.py` pins the name cases.

## Files

- `src/*.py` — the source, one module per concern, concatenated in filename
  order into the built file. **Edit these, never the built file.**
- `claude_agent_pipe.py` — the built function: what you install and what
  the redaction consumers slice. `python3 build.py` regenerates it (and
  `docs/valves.md`); `python3 build.py --check` fails CI when either is stale.
- `redact_stdin.py` — a CLI over the pipe's redactor for job workers that
  deliver agent output outside the chat stream (`--known-env` scrubs live
  values by value, not just by shape). Optional; the pipe does not need it.
- `test_*.py` — standalone suites, stdlib only: `python3 test_sessions.py`,
  optionally against a deployed copy (`python3 test_sessions.py <pipe.py>`).

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and
[CHANGELOG.md](CHANGELOG.md) for what changed on top of upstream.

## Updating a deployed copy

Merging here deploys nothing: Open WebUI runs the copy stored in `webui.db`.
Paste the new `claude_agent_pipe.py` over the function in Admin → Functions,
or post it to `POST /api/v1/functions/id/claude_code/update` with the same
JSON shape as the create call. Valve state lives in `webui.db` and survives
redeploys. The author's own deploy wrapper (backup, post, verify parity, run
the suites against the deployed copy) lives in a separate homelab repo and is
not needed to use this one.

## Attribution

Fork of [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code)
(MIT) at commit `5bbc1fc`, which has been dormant since. The upstream shape
(one pipe, valves, knowledge tools, inline tool details) is Thomas Friedel's;
the patches listed in the changelog are this repo's. MIT, see [LICENSE](LICENSE).
