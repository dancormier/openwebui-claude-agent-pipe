# Security

This function runs Claude Code with `bypassPermissions` by default: any user
it is enabled for can execute code on the Open WebUI host as the Open WebUI
process user. That is the design, not a bug; the README's Security section
says what to restrict. Vulnerabilities are things that break the guarantees
the pipe does make: a user reading another user's chats through
`search_chats` / `read_chat`, the redactor letting a credential of a shape it
claims to catch through, a keyless caller resuming a session that is not
theirs, or a valve doing more than its description says.

Report one by opening a
[private security advisory](https://github.com/dancormier/openwebui-claude-agent-pipe/security/advisories/new)
on this repository, or by emailing the address on the maintainer's GitHub
profile if advisories are unavailable to you. Include the Open WebUI,
`claude-agent-sdk` and Claude Code CLI versions and a reproduction. You will
get an acknowledgement within a week. There is no bug bounty.
