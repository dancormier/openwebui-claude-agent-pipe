# Contributing

- **Edit `src/*.py`, never `claude_agent_pipe.py`.** The built file is
  generated; `python3 build.py` rewrites it and `docs/valves.md`. CI runs
  `python3 build.py --check` and fails if either is stale, so run `build.py`
  before you commit and commit the outputs with the change.
- **Tests** are standalone scripts, stdlib only, no pytest:
  `for t in test_*.py; do python3 "$t" || break; done`. They exec the pure
  helpers above the SDK import with pydantic stubbed, so they run with no
  packages installed. Add a case to the matching suite for anything you
  change in a pure helper; new pure helpers go above the
  `from claude_agent_sdk import (` line so the suites can reach them.
- **Compile check:** `python3 -m py_compile claude_agent_pipe.py redact_stdin.py`.
- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, with a one-line body when
  the why is not obvious from the diff. Add a CHANGELOG entry under
  "Unreleased" for anything a user would notice.
- **Comments** explain why, not what. No section banners, no narration of
  the edit.
- Open pull requests against `main`. One concern per PR.

Testing a change against a real Open WebUI: paste the built file over the
function (Admin → Functions), or post it to
`POST /api/v1/functions/id/claude_code/update`. Open WebUI reinstalls the
`requirements:` line on every save.
