# Contributing to hands

Thanks for helping. hands is small on purpose, and these rules keep it that way.

## The surface is frozen

The flags, the six seat verbs (`start`, `send`, `mail`, `wait`, `events`, `stop`) and `quota` are
the whole interface. A new capability is a separate script that calls `hands.sh`, not another flag. A new
provider is one entry in the wire table (`WIRES` in `seat.py`), not a flag.

## Every change is tested

Run all three suites before a commit. Each must print `0 failed` and exit 0:

```bash
bash tests/selftest.sh
python3 tests/test_acp.py
python3 tests/test_quota.py
```

They are hermetic: stub CLIs stand in for the real ones, and nothing touches the network or
spends quota. The hands suite takes a few minutes and needs `bash` and `jq`. A row that cannot
run on this machine prints `skip`.

A new behavior lands with a selftest row, and the row must be able to fail: revert the fix alone,
run the suite, and see that row go red. A row nobody has seen fail proves nothing.

Don't edit the checkout your live seats run from: a running wrapper starts new `seat.py`
processes from the files on disk, so a half-made edit can break a live seat. Work in a separate
clone or a `git worktree`.

## Fixtures

`tests/fixtures/` holds recorded provider output that the suites replay (see `tests/fixtures/README.md`).
When a provider changes its error wording or stream shape, add the new recording the same
day. Before you commit one, strip it of personal data: home paths, user and host names, account
and session ids, emails, tokens, local timestamps, and anything else that is yours.

## Engine rules

- **One seat contract.** `seat_box()` in `hands.sh` is the only copy of the text every seat
  receives first. A seat's prompt is that box, a blank line, the task file, a blank line, and its
  job-dir line; the job dir's `prompt.txt` is exactly that input.
- **The job dir is the only state.** No daemon, no registry, no cache file. A relaunch reads the
  handle (`DIR/session`), and `events.jsonl` is the truth.
- **Typed failure, never a retry loop.** Every turn ends in an envelope with a status the caller
  can act on; quota deaths carry their reset time.
- **Fix the defect, don't add state.** Prefer one general rule over a provider-specific branch,
  and remove what a change makes redundant.

## Pull requests

Describe what changed and why, name the selftest rows you added, and confirm all three suites
pass. Report security issues privately (see `SECURITY.md`), not in a public issue.
