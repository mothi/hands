# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's **Security** tab
and choose **Report a vulnerability**. Don't open a public issue for them.

Include what you found, how to reproduce it, and the macOS (or Linux) and provider CLI versions
you ran. You should get a first reply within a week.

## What hands protects, and what it doesn't

Read README § Security model before you run a seat. In short:

- A seat is a coding agent with **full read and write access, running as you**. There is no
  read-only mode. Nothing stops a seat from reading your files, running any command, or
  reaching the network.
- hands stores no credentials of its own. Each CLI uses its own login. Every `start` (or one-shot
  run) and `send` from a driver session writes what names that session to the job dir's
  `driver` file (mode 0600): for Claude Code its push socket path, token and session id, for
  Codex its thread id, for OpenCode its session id. A later command from the same session
  rewrites it when the session's address changes. Nothing deletes it when the seat ends: it
  stays until the job dir is deleted (the 30-day sweep, for a dir in the jobs root; an `--out`
  dir is never swept). Anything that runs as you can read it, every seat included.

In scope: mail that reaches a file it shouldn't, a job dir or temp file that another local user can read or
replace, credentials that leak into process arguments, logs or job dirs, and the sweep deleting
anything it didn't create.

Out of scope: what a seat does (it runs as you, by design), and bugs in the
provider CLIs themselves (report those to the provider).
