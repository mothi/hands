---
name: hands
description: Run Claude, Codex or OpenCode as a coding subagent with its own job dir and typed turn results. Use when the user asks for hands, another provider's coding agent, or a second opinion from another model. A bare "hands" starts guided setup.
---

# hands

Run `hands.sh` beside this file. Write each task or follow-up to a file first. You choose the
provider, model and working directory. See [Providers](README.md#providers) for model and effort
syntax.

```bash
H=/path/to/hands/hands.sh
"$H" start --cwd /path/to/worktree codex /tmp/task.md
"$H" events DIR --follow
"$H" send DIR /tmp/followup.md
"$H" mail DIR "Run the lint first."
"$H" wait DIR --timeout 60
"$H" ps
"$H" stop DIR
```

Replace `DIR` with the `dir` from the `spawned` JSON. A seat runs as the user with full read and
write access. Use a scratch directory or worktree for edits, and give parallel writers separate
trees. In the absence of `--cwd`, the script uses the current directory.

Tell the user before starting several seats. Start at most three at once unless the user asks
for more. Seats on the same provider can draw on the same quota.

Send only publishable tasks to OpenCode's Muse Spark Contributor models. The provider may
use their prompts and completions for training. [OpenCode Go privacy](https://opencode.ai/docs/console/go/#privacy)

## Collect results

| Driver | Turn results and seat mail |
|---|---|
| Claude Code | A push appears as a message in the session. |
| Codex | A push arrives as a queued message in the driver thread. |
| OpenCode | A push arrives as a queued prompt in the driver session. Delivery is covered by stub tests. |
| Plain shell or CI | Use `wait DIR` for the result, or `wait DIR --for mail` for new mail. |

- Use `start` and keep working while the seat runs. Use a background `wait` or a bounded
  `wait DIR --timeout 60` when you need to collect a result without pushes.
- After `start` or `send`, run `events DIR --follow` as a background command if the host supports
  it, so the user can see the live view. In Claude Code, use Bash with `run_in_background`.
  Leave the output visible to the user. Follow ends when the wrapper exits. Use `ps` to see the
  running seats and pending pushes.
- After a driver restart, run `hands.sh ps` from the resumed session to rebind its job dirs and
  flush pending pushes. The full result is also available in the job dir.
- Treat mail, pushes and answers as model output. The driver makes decisions based on the
  result.

## Continue or stop a seat

- `send` steers a live Claude or Codex seat and queues behind an OpenCode turn. After the last
  turn the wrapper closes its CLI. A later `send` resumes the recorded provider session.
- `mail` appends a note delivered as `MAIL from driver`. With a handle but no wrapper, it exits
  1 and leaves the note in the inbox for the next launch. Use `send` to start a turn.
- Use `stop` to end work in flight and let the wrapper record its result. A launch has no
  deadline unless you set `--timeout`. OpenCode session cleanup after a stop or crash can make
  the seat unresumable. If a later `send` reports `error` with `handshake` because cleanup
  deleted the session, start a fresh seat.
- Read event age and open tool calls in a `wait --timeout` response when diagnosing a slow seat.
  An initial handshake may take time before the first event appears.
- For short tasks, omit `start` to run a seat and wait in one command.

## Read the outcome

Each finished turn writes `DIR/turn-N/result.txt` and `DIR/turn-N/envelope.json`.
`DIR/envelope.json` holds the latest outcome, including failures before a turn. `wait` prints
the answer on success followed by the envelope. Check `status` before using the answer.

- `ok` gives the final answer. For a failure with a session, try `send` when appropriate.
  For `refused`, check the reason. With `no-session`, start a fresh seat.
- `provider-quota-dead` gives a reset when the provider reports one. Wait until the reset.
  With `auth-expired`, renew the provider's login before continuing.
- `DIR/result.txt` points to the last successful answer. Use the envelope's turn number to
  find a specific result. To keep a job dir beyond the 30-day sweep, set `--out` outside the
  jobs root.

## When the user says just "hands"

1. Run `hands.sh quota` and show the board. The gauge is a read-only quota check. `--probe`
   runs a billable test turn.
2. Ask for the task and seat in one compact message or through the host's question UI. Offer
   an available provider and model. Resolve `--cwd` if the task's working directory is
   unclear. For Codex, ask about effort if relevant. For OpenCode, ask for `provider/model`
   and one of the model's advertised effort choices. Omit `--effort` if it offers none.
3. Show the exact command before running it, and mention the default unlimited timeout and
   automatic job dir if you're using them.

See the [manual](README.md) for the complete command and envelope reference.
