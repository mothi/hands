# Contributing to hands

## The surface is frozen

The one-shot run, six seat verbs (`start`, `send`, `mail`, `wait`, `events`, `stop`), `ps`,
`--probe`, `quota` and their flags are the public interface. Build new capabilities as separate
scripts that call `hands.sh`. Provider wiring is registered in `PROVIDERS` in `wires.py`.

## Tests

Run the full CI suite from the repository root before committing. It must exit 0.

```bash
python3 -m unittest discover -s tests -v
```

The suite uses temporary sandboxes and stub CLIs in `tests/stubs/`, including quota stubs.
It makes no external network requests and spends no quota. To select one test, pass its class
and method to its file, for example

```bash
python3 tests/test_seat_codex.py CodexSeats.test_a_seat_ends_with_its_turn_and_a_send_resumes_it
```

Add a test for new behavior. Before accepting a new or changed test, revert the fix and verify
that the test fails for the expected reason. Use a separate clone or worktree when the main
checkout serves live seats. Wrappers can start new processes from the files on disk while
they run.

## Fixtures

Keep recorded provider output in [tests/fixtures](tests/fixtures/README.md). When a provider
changes its error wording or stream shape, add a new recording and preserve the old one.
Before committing a new recording, remove personal data including home paths, usernames,
account and session ids, email addresses, tokens and local timestamps. Document its provenance,
including any derivation from an existing recording.

## Engine rules

- `SEAT_BOX` in `hands.py` is the shared seat contract. `prompt.txt` holds it first, followed
  by the task file and the job-dir line, with blank lines between them.
- The job dir holds hands' state. A relaunch reads the handle and events. Preserve the
  [job dir contract](README.md#job-dir-reference).
- Give each completed turn a typed outcome. Provider quota failures carry a reset when it is
  available. Leave retry decisions to the caller.
- Fix the defect with existing state where possible and remove code the change makes redundant.

## Pull requests

Describe the change and why it matters, identify new tests and confirm the full test run passed.
Use the [private reporting process](SECURITY.md) for security issues.
