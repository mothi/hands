# Security policy

## Reporting a vulnerability

In GitHub, open the repository's **Security** tab and choose **Report a vulnerability** to
send a private report. Include what you found, how to reproduce it, and your OS and provider
CLI versions. Expect a first reply within a week.

## Trust boundary

Every seat is a coding agent running as you. It can read and write your files, run commands
and use the network. A worktree or scratch directory scopes the working directory. See the
[security model](README.md#security-model) for permissions and environment details.

Provider CLIs keep their own logins. The OpenCode quota gauge reads the existing Go token
from the CLI's `auth.json` and passes it to `curl` on stdin. It does not store the raw response.

For push delivery, a `start` or `send` from a supported driver session can write `DIR/driver`
with mode 0600. It holds the host and address, plus the session id when available. For
Claude Code, the address includes a socket path and authentication token. A command from
the same driver session can refresh the address after a restart. The file stays when the
seat ends and is readable by processes running as you, including seats. The 30-day sweep can
remove eligible job dirs inside the jobs root. For retention outside the sweep, choose an
`--out` path outside that root.

Report mail writing through an unsafe path, job files another local user can read or replace,
credential leaks, and unintended sweep deletions. For vulnerabilities in the provider CLIs,
contact the provider.
