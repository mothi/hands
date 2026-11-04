# Contributing to hands

Thanks for helping. hands is small on purpose, and these rules keep it that way.

## The surface is frozen

The flags, the six seat verbs (`start`, `send`, `mail`, `wait`, `events`, `stop`) and `quota` are
the whole interface. A new capability is a separate script that calls `hands.sh`, not another flag. A new
provider is one entry in the provider table (`PROVIDERS` in `wires.py`), not a flag.

## Every change is tested

Run all four suites before a commit. Each must print `0 failed` and exit 0:

```bash
python3 tests/test_wires.py   # the clients and translators on their own (seconds)
python3 tests/test_cli.py     # hands.py end to end (about a minute)
python3 tests/test_seat.py    # the wrapper against stub CLIs (about four minutes)
python3 tests/test_quota.py   # the quota board
```

They are hermetic: the stub CLIs in `tests/stubs` stand in for the real ones, and nothing
touches the network or spends quota. To run one test, name it:
`python3 tests/test_seat.py CodexSeats.test_one_app_server_serves_every_turn`.

A new behavior lands with a test, and the test must be able to fail: revert the fix alone, run
the test, and see it go red. A test nobody has seen fail proves nothing.

Don't edit the checkout your live seats run from: a running wrapper starts new `seat.py`
processes from the files on disk, so a half-made edit can break a live seat. Work in a separate
clone or a `git worktree`.

## Fixtures

`tests/fixtures/` holds recorded provider output that the suites replay (see `tests/fixtures/README.md`).
When a provider changes its error wording or stream shape, add the new recording the same
day. Before you commit one, strip it of personal data: home paths, user and host names, account
and session ids, emails, tokens, local timestamps, and anything else that is yours.

## Engine rules

- **One seat contract.** `SEAT_BOX` in `hands.py` is the only copy of the text every seat
  receives first. A seat's prompt is that box, a blank line, the task file, a blank line, and its
  job-dir line; the job dir's `prompt.txt` is exactly that input.
- **The job dir is the only state.** No daemon, no registry, no cache file. A relaunch reads the
  handle (`DIR/session`), and `events.jsonl` is the truth.
- **Typed failure, never a retry loop.** Every turn ends in an envelope with a status the caller
  can act on; quota deaths carry their reset time.
- **Fix the defect, don't add state.** Prefer one general rule over a provider-specific branch,
  and remove what a change makes redundant.

## Pull requests

Describe what changed and why, name the tests you added, and confirm all four suites pass. Report security issues privately (see `SECURITY.md`), not in a public issue.
