# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's **Security** tab
and choose **Report a vulnerability**. Don't open a public issue for them.

Include what you found, how to reproduce it, and the macOS (or Linux) and provider CLI versions
you ran. You should get a first reply within a week.

## What hands protects, and what it doesn't

Read README § Security model before you run a seat. In short:

- A seat is a coding agent with **full read and write access, running as you**. There is no
  read-only mode. Without `--sandbox`, nothing stops a seat from reading your files,
  running any command, or reaching the network.
- `--sandbox` (macOS only) runs the seat's CLI under `sandbox-exec`. It denies reads of common
  secret stores and other repos, and limits writes. It is a guard against mistakes, not a
  boundary against a determined attacker: the network stays open and `sandbox-exec` is
  deprecated by Apple.
- hands stores no credentials of its own. Each CLI uses its own login. While a seat lives, its
  job dir holds the host session's push socket path and token (`driver.sock`, `driver.token`,
  mode 0600). An unsandboxed seat can read them.

In scope: a sandboxed seat that can read a denied path or write outside its allowed paths, mail
that reaches a file it shouldn't, a job dir or temp file that another local user can read or
replace, credentials that leak into process arguments, logs or job dirs, and the sweep deleting
anything it didn't create.

Out of scope: what an unsandboxed seat does (it runs as you, by design), and bugs in the
provider CLIs themselves (report those to the provider).
