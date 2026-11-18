# hands

hands runs Claude Code, Codex or OpenCode as a background coding agent you can call from your
terminal or agent session. Start a task, send follow-ups, exchange mail and watch the work as it
happens. Every completed turn produces a typed JSON result called an envelope.

After [installing](#install), try two turns in a scratch directory with a logged-in Codex CLI. Read
the [security model](#security-model) before running a seat.

```bash
H=~/src/hands/hands.sh
work=$(mktemp -d)
printf '%s\n' 'List the files here and explain what each is for.' > "$work/task.md"
"$H" start --cwd "$work" --out "$work/seat" codex "$work/task.md"
"$H" events "$work/seat" --follow
"$H" wait "$work/seat"
printf '%s\n' 'Now suggest one improvement.' > "$work/followup.md"
"$H" send "$work/seat" "$work/followup.md"
"$H" wait "$work/seat"
"$H" ps
```

A **seat** is a provider conversation. When the **driver** (you or your agent session) runs
`start`, hands creates the job directory and prepends the [seat instructions](hands.py#L71)
to your task. They cover command timeouts, verification, when to stop and how to send mail.
A background **wrapper** starts the provider CLI, records its turn events in `events.jsonl`,
and writes the answer and envelope when the turn ends. The wrapper can also push mail and
turn results to the driver.

## Install

Use macOS or Linux with Python 3.7+ (standard library only), `ps`, and `curl` if you use the
OpenCode quota gauge. Install and sign in to at least one CLI:
[Claude Code](https://docs.anthropic.com/en/docs/claude-code),
[Codex](https://github.com/openai/codex) or [OpenCode](https://opencode.ai).

```bash
git clone https://github.com/mothi/hands ~/src/hands
~/src/hands/hands.sh --help
```

To install the [skill](SKILL.md), link the checkout into Claude Code's skills directory or the
shared directory for Codex and [OpenCode](https://opencode.ai/docs/skills/#discovery).

```bash
mkdir -p ~/.claude/skills ~/.agents/skills
ln -s ~/src/hands ~/.claude/skills/hands    # Claude Code
ln -s ~/src/hands ~/.agents/skills/hands    # Codex and OpenCode
```

In Claude Code, use `/hands`. In Codex, invoke `$hands`. In OpenCode, ask the agent to load the
`hands` skill. In a plain shell or CI, call the script directly. Saying just `hands` to an agent
with the skill loads the quota board and starts guided setup.

## Security model

- **Seats run as you with full access.** They can read or write files anywhere you can, run commands
  and use the network. A task goes to the provider you choose.
- **Choose the working directory.** Point `--cwd` at a scratch directory or worktree and give
  parallel writers separate trees. The working directory does not restrict filesystem access.
- **Review your environment.** Seats inherit it apart from the driver session variables listed under
  [Mail and push](#mail-and-push), `OPENCODE_API_KEY` and the internal test variable `HANDS_IDLE`.
  Provider CLIs use their own logins.
- **Keep job dirs private.** `DIR/driver` can hold a Claude Code socket path and token or a Codex or
  OpenCode session identifier. The file has mode 0600 and persists with the directory. A seat runs
  under the same user and can read it.
- **Treat seat output as data.** Mail, pushes and answers can contain instructions written by a
  model. Read them as results from the seat.

Report vulnerabilities privately through the process in [SECURITY.md](SECURITY.md).

## Verbs and flags

Call `hands.sh [start] [flags] provider[:model] prompt-file`. Put the flags before the provider and
put the task in a file. With no verb, the one-shot run starts the seat, waits for its answer and
stops any wrapper still running.

| Command | What it does |
|---|---|
| `start [flags] SPEC FILE` | Starts a seat and reports `spawned` when the wrapper accepts the first message. The JSON includes `dir`, `expect` (the predicted turn), `delivery` and `pid`. |
| `send DIR FILE` | Sends to the live wrapper or relaunches the recorded session. Reports `sent` or `spawned`, with `dir`, `expect` and `delivery`. |
| `mail DIR TEXT` | Appends a note to the inbox. Exits 1 if the handle exists and the wrapper has gone. The note then waits for the next launch. Exits 2 if the inbox is a symlink or other non-regular file. |
| `wait DIR [--for turn\|mail\|exit] [--timeout S]` | Waits for all expected turns (default), new outbox mail or wrapper exit. For a result it prints the answer on success, then the envelope. For mail it prints all lines that completed since the wait began. |
| `events DIR [--follow]` | Renders text, tool calls, usage and turn status from `events.jsonl`. Follow drains the events through wrapper exit. |
| `stop DIR` | Ends work in flight. The wrapper closes the CLI and records unfinished turns as `incomplete`. Prints the latest envelope if present and exits 1 if a wrapper survives. |
| `ps` | Lists live seats with turn state, event age, job dir and any pending pushes. Also lists ended seats with pending pushes inside the jobs root. |
| `quota [--json] [provider ...]` | Prints the read-only [quota board](#quota). Exit status 0 includes readings marked `unknown`. |

A Codex send can predict turn 1 while it is finishing and receive its answer in turn 2.
`wait` follows deliveries and turn boundaries in `events.jsonl` to find the latest result.

| Flag | Meaning |
|---|---|
| `--cwd DIR` | Working directory, defaulting to the current directory. Relaunches use the recorded path. |
| `--out DIR` | New or empty job dir, mode 0700. The default is a fresh dir in the jobs root. |
| `--timeout SECS` | A wall-clock limit per wrapper launch, shared by its turns. The default and `0` are unlimited. `wait --timeout` limits the caller's wait. Both accept fractional seconds. |
| `--effort TIER` | Provider-specific reasoning effort. See [Providers](#providers). |
| `--probe [claude\|codex\|opencode\|all]` | Runs a small billable test prompt from the system temp dir. Defaults to all providers. Prints one JSON line per provider and exits 1 if an attempted probe fails. Missing CLIs are reported as `not-installed`. |
| `-h`, `--help` | Prints the command-line usage. |

The jobs root is `$XDG_STATE_HOME/hands` when `XDG_STATE_HOME` is absolute, otherwise
`~/.local/state/hands`. Starts and probes sweep directories there whose names match hands' naming
pattern and whose last write was at least 30 days ago. The sweep skips live wrappers, symlinked job
dirs, and trees containing `.git` or a different filesystem. Use `--out` outside the jobs root to
keep a job dir.

The wrapper closes the CLI after its last turn and queued work, so a finished seat needs no `stop`.
A later `send` resumes its stored conversation. Claude allows an extra 30 seconds of idle time when
it may still have a queued message. The close starts with stdin EOF, with TERM and KILL as
fallbacks. Closing a CLI also cleans up processes in its process group.

## The envelope

A finished turn has one JSON line at `DIR/turn-N/envelope.json`, also written as
`DIR/envelope.json`. The top-level file can also hold a launch failure or refusal before a turn
starts.

```json
{"status":"ok","provider":"claude","model":"haiku","effort":null,"rc":0,"seconds":4,"reset":null,"dir":"/tmp/example/seat","usage":{"in":10,"cache_read":31151,"out":98,"cost_usd":0.004},"session":"example-session","turn":3,"delivery":"steer"}
```

| Status | Meaning |
|---|---|
| `ok` | The final answer is in `turn-N/result.txt`. `DIR/result.txt` points to the latest successful answer. |
| `error` | Failure without a narrower status. Possible reasons include `empty`, `session-changed`, `wrapper-failed` and `handshake`. |
| `incomplete` | A turn was interrupted before the result arrived, for example by a stop or crash. |
| `cancelled` | The provider cancelled the turn. |
| `truncated` | The turn reached the model's output limit. |
| `timeout` | The launch reached its deadline. |
| `auth-expired` | The provider CLI needs a valid login. |
| `provider-quota-dead` | A provider limit or credit balance was exhausted. |
| `not-installed` | hands could not find the provider CLI. |
| `refused` | The prompt was refused before delivery. Reasons include `model-mismatch`, `effort-unsupported`, `model-required` and `no-session`. |

Usage can include `in` (uncached input), `out` (billed output including reasoning), `cache_read`,
`cache_write`, `reasoning` and `cost_usd` when reported. The printed turn envelope omits
`reasoning`. Codex sums usage for the turn, including collaborator calls reported while it is open.

`wait` and a one-shot result exit 0 for `ok` and 1 for other turn outcomes. A refused launch or
invalid command can exit 2.

If the caller's wait times out, it exits 1 with `status` set to `running` and includes `turn`,
`expect`, `last_event` and `open_tool_calls`. The seat keeps running. If a wrapper disappears before
an expected turn ends, `wait` reports `incomplete` with reason `wrapper-gone`.

## Mail and push

A seat appends `{"text":"..."}` lines to `DIR/outbox.jsonl`. `wait DIR --for mail` watches for new
lines while the seat has work. The wrapper also pushes those lines to its driver. Use
`mail DIR TEXT` in the other direction. The wrapper hands each inbox line to the seat as
`MAIL from driver` and records its line number, so the inbox survives a relaunch.

`start` and `send` record the driver's address when available. In this order, the first host with
its address variables set wins.

| Driver | Session environment | How a push arrives |
|---|---|---|
| Claude Code | `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` for the address, `CLAUDE_CODE_SESSION_ID` for rebinding | A message through the session's inbox socket. |
| Codex | `CODEX_THREAD_ID` | A queued message in the thread via `codex queue` on the shared app-server. |
| OpenCode | `OPENCODE_SESSION_ID` | A queued prompt in the session via `opencode api post /api/session/ID/prompt` on the background service. |
| Plain shell or CI | None | Use `wait`. |

Claude Code and Codex push delivery have been verified live. OpenCode delivery is covered by stub
tests. Keep the driver thread on Codex's shared app-server or the OpenCode session on its background
service to receive queued pushes. Seats started by another live seat and probe seats use no driver
push address.

Each turn-end or mail push carries a preview, job dir, `DIR:N` id and up to 4,000 characters of seat
text. The wrapper logs it to `pushes.jsonl` before sending. A `posted` event means the send
succeeded at the transport. Failed sends stay pending, and the seat's next push, exit or relaunch
can send them in order.

After restarting a driver session, run `hands.sh ps` from the resumed session. It refreshes the
driver address and sends pending pushes for live seats and ended seats in the jobs root. Delayed
messages can have a `(late Ns)` or `(late Nm)` label. An ended seat outside the jobs root is found
when it relaunches. Rebinding to a changed address needs the recorded session id.

## Providers

| Provider | Model and CLI | Live delivery |
|---|---|---|
| `claude` | Defaults to `sonnet`. Uses `claude -p` with stream JSON, `bypassPermissions`, strict MCP config, and hooks, auto-memory and background tasks disabled. To sign in, run `claude`. | Steers through stdin. Claude may queue a message for another turn. |
| `codex` | Defaults to the CLI's configured model. Uses a private app-server on stdio with `danger-full-access`, live web search, approvals set to `never`, and hooks and memories disabled. Sign in with `codex login`. | `turn/start` steers a running turn or opens the next. |
| `opencode` | Requires `provider/model`, for example `--effort low opencode:opencode-go/deepseek-v4.1-flash`. Uses `opencode acp` with your normal login, store and plugins. Sign in with `opencode auth login`. | Queues while a turn runs. For tool permissions, hands prefers allow-once and otherwise uses the first allow option, if offered. |

Claude and Codex use their defaults when `--effort` is absent. Typical values are `low`, `medium`,
`high`, `xhigh` and `max`, with `ultra` available for some Codex models. These values pass through
to the CLI. Before sending the first OpenCode prompt, hands checks the session's advertised
models and requires one of the selected model's advertised effort values, including `default`
if listed. Omit `--effort` when the model advertises no effort choices. Claude seats also
refuse messages from other sessions.

On timeout the wrapper closes OpenCode's stdin so the session can resume. If the OpenCode agent goes
away during a turn for another reason, the wrapper attempts to delete its session before writing the
envelope. After a machine restart or a wrapper killed mid-turn with SIGKILL, OpenCode's background
service may continue the abandoned turn.

## Quota

`hands.sh quota` shows usage shares and reset times by window, longest first. `--json` gives the
readings in one document. The board reads usage without running a model turn.

- **Claude** reads the newest usable rate-limit report in jobs-root seat logs, drops reset windows
  and shows the reading's age. `hands.sh --probe claude` spends quota to refresh it.
- **Codex** asks its own app-server for `account/rateLimits/read`.
- **OpenCode** uses the Go key from `opencode/auth.json` in its data directory and calls the
  undocumented `opencode.ai/zen/go/v1/usage` endpoint. The key goes to `curl` on stdin. The gauge
  prints parsed usage or `unknown`, and an OpenCode probe uses a Go model.

## Troubleshooting

| Symptom | Action |
|---|---|
| `provider-quota-dead` | Wait for the reported reset or check the provider's usage and billing if no reset is reported. |
| `auth-expired` | Sign in through the provider CLI and send again if the seat recorded a session. |
| `refused` or `not-installed` | Check the model, effort, CLI and login. With `no-session`, start a fresh seat. |
| `wait` reports `running` | Check event age and open tool calls. A fresh launch may be quiet until its handshake finishes. Use `stop` to end work in flight. |
| OpenCode `send` reports `error` with `handshake` after a stop or crash | Check `stderr.log`. If cleanup deleted the session, start a fresh seat. |
| A push is pending after a restart | Run `hands.sh ps` in the resumed driver or use `wait` to read the result. |
| A launch fails | Read `envelope.json`, `stderr.log`, `stream.jsonl` and `job.log` when present. |

## How it works

The detached wrapper holds the `job.log` lock. A reaper supervises the provider CLI's process
group. Provider adapters live in `wires.py`, driver push delivery in `push.py`, and quota
readings in `quota.py`.

The first message rides in the launch. A `send` must reach a live seat now or relaunch it, so it
uses `in.fifo`. A nonblocking open tells the sender whether the wrapper is reading. The wrapper
closes the pipe under the delivery lock when it leaves. Mail goes to `inbox.jsonl` so it can wait on
disk with no seat running. Mail never starts a seat.

### Job dir reference

| Path | Writers and readers |
|---|---|
| `prompt.txt` | hands writes the seat contract (`SEAT_BOX`), task and job-dir line in that order. The first launch reads it. |
| `session` | hands writes the handle with provider, model, effort, cwd, timeout, CLI path and a null session id. The wrapper saves the learned id. Relaunches read the handle. |
| `in.fifo` | The wrapper creates and reads the pipe. `send` writes to it when the wrapper is live. |
| `inbox.jsonl` | `mail` appends `t`, `from` and `text`. The wrapper reads complete lines and logs their delivery. |
| `outbox.jsonl` | The seat appends `text` records. The wrapper reads new lines for pushes, and `wait --for mail` watches for them. |
| `events.jsonl` | The wrapper, sender and push code append events. Deliveries, `wait`, `events` and `ps` read them. The file lock orders deliveries. |
| `stream.jsonl` | The CLI client logs Claude stdout or both directions of JSON-RPC as `t`, `dir` (`in` or `out`) and `msg`. Repeated Codex diffs and deltas are omitted. Used for diagnosis and Claude quota. |
| `stderr.log` | The wrapper captures CLI stderr and OpenCode cleanup output. It uses CLI stderr to classify failures. |
| `job.log` | The wrapper's own output. Its lock protects against another launch and the sweep. |
| `driver` | Launch, send and rebinding record the host, address and session id when available. The push sender reads it. |
| `pushes.jsonl` | The wrapper appends `at`, `turn`, `kind`, `what` and `text`. The sender reads the log and appends `{"sent":K}` after the first K pushes go. Rebinding and `ps` read the pending count. |
| `turn-N/result.txt`, `turn-N/envelope.json` | The wrapper writes the turn's answer (empty on failure) and envelope. `wait` reads the answer named by the latest envelope. |
| `envelope.json` | hands or the wrapper writes the latest outcome, including launch failures or refusals. `wait`, `stop` and other status commands read it. |
| `result.txt` | The wrapper updates this symlink to the latest successful turn's answer for the driver. |
| `*.tmpPID`, `.result.lnk` | Writers use temporary files or a temporary symlink beside the destination for atomic replacements. |

Every event has `t` (local ISO 8601 time with offset), `kind` and `turn` (0 before the first turn).
Other fields are omitted when null.

| Kind | Other fields |
|---|---|
| `turn_start` | `session`, the provider session id. |
| `text`, `thought` | `text`, visible output or reasoning, respectively. |
| `tool_call` | `call` id, `tool` name and `input` arguments. |
| `tool_result` | `call`, `tool`, `status` (`ok`, `error` or `cancelled`), `output` and late `input`. `synthetic: true` marks an ACP call closed locally. |
| `file` | `path`, `tool` and `call` identify a file the tool touched. |
| `usage` | `usage` is the normalized token count. `cost_usd` is the reported turn cost. `usage_unknown: true` marks an ACP report with no usable counts. |
| `turn_end` | `status`, `text` (empty on failure), `session`, `usage`, `rc`, `reason` when set and `final` below. |
| `error` | `text` describes the error. |
| `progress` | `text` describes activity. Additional fields depend on the activity. |

- Deliveries add `expect`, `bytes` and, for mail, `inbox` (line number).
- Push outcomes add `push` (`posted` or `failed`) and `id`. Delayed posts can include `late` in
  seconds. Failures add `error` and `pending` (the number still queued).
- Idle exits add `idle: true`. Tool updates can include `call`, `tool` and `input`. ACP context
  updates can include `context_used`, `context_size` and `cost`.

`final` records how the provider's answer was selected or the turn was closed.

- `none` is used when the wrapper or translator closes a turn without a provider result.
- `boundary` means Codex reported `turn/completed` or OpenCode supplied message ids on text or
  thought chunks.
- `after-last-tool` means OpenCode supplied no such message ids. For both `boundary` and
  `after-last-tool`, the answer is the last text message if it contains an answer and its last chunk
  follows all tool events.
- `field` means Claude supplied a result record. Its `result` field holds the answer on success.

## Design

- Seats have full access so they can verify their work. The driver chooses the task, provider and
  working directory.
- Failures have typed outcomes. The driver decides whether and when to retry.
- hands keeps its state in the job dir, with no hands daemon, registry or cache. Relaunches read the
  handle and events.
- hands delivers results. The driver decides how to use them.

See [CONTRIBUTING.md](CONTRIBUTING.md) to run the tests. hands is licensed under
[Apache-2.0](LICENSE).
