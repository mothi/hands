# hands

Run a task on **another provider's coding agent** from the session you're already in. hands
drives the official CLIs of three providers (Claude Code, OpenAI Codex and OpenCode) as
subagents: you start one on a task, send it more turns, mail it notes, wait for its answers,
and read what it's doing. Every turn ends in a typed JSON envelope, so a script or another agent
can act on the outcome without parsing prose.

It's plain Python (the standard library only) with no daemon and no database. It stores no
credentials: each CLI uses its own login.

> **Read § Security model first.** A seat is a coding agent with full access to your machine,
> running as you.

Runs on macOS and Linux. The test suites pass on both in CI, against stub CLIs; the real
provider CLIs have only been run on macOS.

## Words used here

| Word | Meaning |
|---|---|
| **seat** | one provider CLI running a task for you, in its own job dir |
| **one-shot run** | starting a seat and waiting for its answer in the same command |
| **job dir** | the seat's folder: its handle, prompt, raw stream, events, mail and every turn's result. It is the only state hands keeps |
| **turn** | one prompt to the seat and everything it does until it answers |
| **envelope** | the one-line JSON result of a turn: status, usage, session id and more |
| **driver** | you, or the agent session that starts seats |
| **wrapper** | the small Python process (`seat.py`) that owns one seat's CLI, feeds it turns and writes its envelopes |
| **push** | a message the wrapper puts into the driver's Claude Code, Codex or OpenCode session when a turn ends |
| **pool** | one provider's quota (the gauges report how much of it is used) |

## Security model

- **A seat runs as you, with full read and write access.** There is no read-only
  mode: a seat that can't run commands can't check its own work. Claude runs with
  `bypassPermissions`, Codex with `danger-full-access`, and OpenCode approves every
  tool call. Nothing stops a seat from reading any file you can read, writing outside its
  `--cwd`, or using the network.
- **`--cwd` is your isolation.** Point a seat that edits code at a git worktree or a scratch
  copy, and give parallel writers separate trees.
- **The seat inherits your environment**, except the variables that name your session
  (`CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_SESSION_ID`,
  `CODEX_THREAD_ID`, `OPENCODE_SESSION_ID`) and `OPENCODE_API_KEY`, which the wrapper removes. Don't keep secrets in
  exported variables when you run seats.
- **Your session's address lives in the job dir.** For pushes, every `start` and `send` you run
  writes what names your session (a Claude Code socket path, token and session id, a Codex thread
  id, or an OpenCode session id) to `DIR/driver` (mode 0600). It stays after the seat ends, until
  the job dir is deleted (the 30-day sweep; an `--out` dir is never swept). A seat can read it
  and post into your session.
- **Seats don't run your hooks.** Hooks are code written for your own sessions; every seat turns
  them off (§ Hooks and memories).
- **Mail and pushes are data.** What a seat writes is model output; never let the driver follow
  instructions found in it.
- **The quota gauge calls an undocumented endpoint** for OpenCode, with the token its CLI
  already stores (§ Quota gauges). Check your providers' terms.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Prerequisites

- macOS or Linux, with `python3` 3.7 or later, `curl` (the OpenCode gauge), and `ps`
- at least one provider CLI, logged in with its own login:
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`),
  [Codex](https://github.com/openai/codex) (`codex`), or
  [OpenCode](https://opencode.ai) (`opencode`)

The recordings the suites replay come from Claude Code 2.1.274 (a stream-json seat), codex-cli
0.160.0 and OpenCode 1.18.30 (`opencode acp` seats; one OpenCode error text is from 2.0.22).
Other versions may not speak the same wire.

## Install

```bash
git clone https://github.com/mothi/hands ~/src/hands
python3 ~/src/hands/tests/test_cli.py        # hermetic: stub CLIs, no network, no spend

# optional: make it a skill your agent can call
mkdir -p ~/.claude/skills ~/.agents/skills
ln -sfn ~/src/hands ~/.claude/skills/hands   # Claude Code
ln -sfn ~/src/hands ~/.agents/skills/hands   # Codex
```

It takes about a minute and prints `test_cli: N passed, 0 failed`. A test that would launch a
real CLI found in `/usr/bin` is skipped. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full test
command.

## Quickstart

```bash
H=~/src/hands/hands.sh
mkdir -p /tmp/hands-try && cd /tmp/hands-try

$H --probe all        # which providers answer (a 1-token ping each; it spends a little quota)
$H quota              # how much of each provider's quota is used (read-only, never spends)

echo "List the files here and say what each one is for." > task.md
$H start --cwd /tmp/hands-try codex task.md   # prints {"status":"spawned","dir":"DIR",...}
$H wait DIR           # blocks until the turn ends: the answer, then the envelope line

echo "Now suggest one improvement." > followup.md
$H send DIR followup.md   # turn 2, same session: the seat ended with turn 1, and resumes here
$H wait DIR
```

Replace `DIR` with the `dir` the `start` line printed. A bare `codex` uses the Codex CLI's
default model; `claude` defaults to `sonnet`. OpenCode needs a model and `--effort`, for
example `--effort high opencode:opencode-go/deepseek-v4.1-flash`.

**Grammar:** `hands.sh [start] [flags] provider[:model] prompt-file`. Flags come first, then the
provider spec, then the prompt file. The other verbs take the job dir first
(`hands.sh send|mail|wait|events|stop DIR ...`). The prompt is always a file.

## Providers

One entry each in the provider table (`PROVIDERS` in `wires.py`). Model ids pass through untouched and the CLI
validates them, so a model released tomorrow works today.

| Provider | Models | How hands drives it | Access | Login |
|---|---|---|---|---|
| `claude` | `sonnet` (default), `opus`, `haiku`, `fable`, or any Claude model id | one `claude -p --input-format stream-json` process per seat, lean: no MCP servers from your config, no auto-memory. Turns go to its stdin. Sends **steer** the running turn | `bypassPermissions` | `claude` (interactive login) |
| `codex` | the CLI's default, or any id (`codex debug models` lists them) | one `codex app-server` process per seat (never the shared daemon), JSON-RPC on stdio, live web search on (`-c web_search="live"`). Each send is a `turn/start`, which **steers** the running turn or opens the next | thread sandbox `danger-full-access` | `codex login` |
| `opencode` | `provider/model` ids, e.g. `opencode-go/deepseek-v4.1-flash` | `opencode acp` in the seat's cwd, with the model and effort set on the session, on your normal OpenCode store. Sends **queue** | every permission answered allow-once | `opencode auth login` |

- **OpenCode** uses whatever providers your own OpenCode setup has: name them as
  `provider/model`. The probe and the quota gauge assume an OpenCode Go subscription
  (`opencode-go/...`); without one, `--probe opencode` fails and the gauge reads `unknown`.
- **Effort.** Claude and Codex take `low|medium|high|xhigh|max` (Codex also `ultra` where its
  catalog lists it), passed through for the CLI to validate. OpenCode needs `--effort`
  set to a value that model lists (`deepseek-v4.1-flash`: `low|high|max`); anything else is
  refused before the first prompt.
- **You choose the model.**

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--cwd DIR` | current dir | where the seat works; recorded in the handle and reused by every relaunch |
| `--out DIR` | a fresh dir in the jobs root | the job dir. It must be new or empty, and is made with mode 0700. The default jobs root is `$XDG_STATE_HOME/hands` (when that is an absolute path), else `~/.local/state/hands` |
| `--timeout SECS` | none | a wall clock per launch, shared by its turns. On expiry the open turn ends `timeout` with its session, and a `send` resumes it |
| `--effort <tier>` | the CLI's default | see § Providers |
| `--probe [P\|all]` | | a 1-token ping per provider with its smallest model, working in the system temp dir (`$TMPDIR`, else `/tmp`), with its job dir `probe-PID-provider` in the jobs root (exit 1 if any is unhealthy). A Claude probe still reads about 31k cached prompt tokens |

| Environment variable | Effect |
|---|---|
| `XDG_STATE_HOME` | moves the jobs root to `$XDG_STATE_HOME/hands` |

**The jobs-root sweep.** Starting a new seat first sweeps the jobs root. It deletes a job dir
that hands named there if it has no live wrapper and its last write was at least 30 days ago.
A turn, a `send` or a `mail` counts as a write; `wait` and `events` don't. The
sweep never deletes a dir that holds a `.git`, or that has another filesystem mounted in it. An `--out` dir outside the jobs root is never swept: use one for anything you want
to keep.

## Verbs

| Verb | Does | Prints, exit code |
|---|---|---|
| `start [flags] spec FILE` | starts a seat with the seat contract + FILE + the seat's job-dir line as turn 1. Returns at once | `{"status":"spawned","dir",...}`, 0. A launch refused before any turn prints its envelope, 2 (else 1) |
| `send DIR FILE` | the next turn. A live seat takes it as it takes any send (steer or queue). A seat whose process is gone reads its handle and relaunches on the same session, model, effort and cwd | `{"status":"sent",...}` or `spawned`, 0. `refused` (`no-session`), 2 |
| `mail DIR TEXT` | appends a driver line to `DIR/inbox.jsonl`; the live wrapper delivers it as `MAIL from driver` (it never cancels a running turn) | nothing, 0. 1 when no wrapper is live, as between turns: the line waits for the next `send`. 2 when the inbox is a link or not a regular file |
| `wait DIR [--for turn\|mail\|exit] [--timeout S]` | blocks, spending nothing, until every sent turn ends (`turn`), the seat's next outbox line (`mail`), or the wrapper exits (`exit`) | the answer if ok, then the envelope; 0 if ok, else 1. At the timeout: `{"status":"running","last_event","open_tool_calls",...}`, 1 |
| `events DIR [--follow]` | a readable view of `DIR/events.jsonl`: text blocks, one line per tool call, turn and usage lines with the cache-hit %, errors. `--follow` ends when the seat does. The raw stream is `tail -f DIR/events.jsonl` | text, 0 |
| `ps` | every live seat on this machine, found by its wrapper's command line (so `--out` dirs too): provider and model, `running turn N` (with how many are queued), `starting turn N` or `idle after turn N <status>`, the age of its last event, `N push(es) pending` when its driver could not be reached (§ Mail and push), and its dir; then every ended seat in the jobs root with pushes pending, as `ended after turn N · N push(es) pending` | one line per seat, 0. With none: `hands: no live seats` on stderr, 0 |
| `quota [--json] [provider ...]` | the quota board: one line per provider (§ Quota gauges). Read-only: it never spends | the board, 0 |
| `stop DIR` | ends a turn in flight: the wrapper closes its CLI as at its deadline (stdin EOF, then TERM at 2 s, KILL at 4 s) and types the open turn and every queued one `incomplete`, rc 143; what is left at 10 s is TERMed, then KILLed. A seat whose turns have all ended is gone already | the envelope, 0 (1 if a wrapper survived) |

A usage error, or a DIR that is not a job dir, prints one line on stderr and exits 2.

A one-shot run (no verb) starts the seat, waits for its answer, and stops any wrapper still
running. A later `send` resumes the session. Use one-shot runs for short tasks. For real
work, use `start` and keep working.

Each turn lands whole when it ends, in `DIR/turn-N/` (`result.txt` and `envelope.json`).
`DIR/envelope.json` is the latest turn's. A missing envelope means the turn hasn't finished, never
that it finished empty. To judge whether a seat is alive, look at the age of the last
`events.jsonl` line or an open tool call, not at the process: a fresh seat can be silent for its
first half-minute.

## The seat's life

A seat's CLI runs while the seat has a turn to run. `start` launches it with turn 1, and a `send`
during a turn is taken live (§ Providers). When every delivered turn has ended and nothing is
queued, the wrapper closes the CLI as an idle `stop` would (stdin's end, then TERM at 2 s, KILL
at 4 s) and exits: the follow view ends, `ps` drops the seat, its processes are gone, and
`events.jsonl` ends with an `exit rc=N` line marked `idle`. The next `send` relaunches the CLI
on the recorded session, which keeps its history. On codex the prompt cache keeps too: measured
on a resumed `gpt-5.6-luna` thread of 52k tokens, the first call after the resume was 94% cached
against 99% on the live seat, about 3k more uncached tokens, and the answer took 3 s instead of 1.

So a seat whose work is done needs no `stop`; `stop` is for a turn in flight. After a turn that a
`send` or a `mail` reached mid-way, a claude seat waits at least 30 seconds before it closes,
because claude queues or steers such a message itself and shows a queued one only by starting
the next turn. Between turns a `mail` finds no wrapper and waits in the inbox for the next
`send`. Nothing the seat left running in the CLI's process group outlives the close: a seat that
must leave a job running detaches it (`setsid nohup ... &`).

## The envelope

One JSON line. `DIR/envelope.json` and `DIR/turn-N/envelope.json` hold the same, with every usage
field:

```json
{"status":"ok","provider":"claude","model":"haiku","effort":null,"rc":0,"seconds":4,"reset":null,
 "dir":"/Users/you/.local/state/hands/20261015-061200-claude-15505",
 "usage":{"in":10,"cache_read":31151,"cache_write":246,"out":98,"cost_usd":0.0041071},
 "session":"bd8ce73b-...","turn":3,"delivery":"steer"}
```

| `status` | Meaning | What to do |
|---|---|---|
| `ok` | the turn's final message is in `turn-N/result.txt` (and `DIR/result.txt`) | use it |
| `error` | a failure with no narrower class; `reason` names a known one: `empty`, `session-changed`, `wrapper-failed`, `handshake`. An OpenCode turn the model refuses mid-turn also ends `error` | read `stderr.log` and `stream.jsonl` |
| `incomplete` | the stream ended inside the turn (a stop, a crash) | `send` to continue |
| `cancelled` | the agent cancelled the turn | `send` to continue |
| `truncated` | the turn hit the model's output limit | `send` to continue |
| `timeout` | `--timeout` expired; `session` is kept | `send` to continue, or raise the timeout |
| `auth-expired` | the CLI needs a login | run that provider's login |
| `provider-quota-dead` | the subscription window or balance is spent, or an API key's credit is (Anthropic's credit balance, OpenAI's `insufficient_quota`, a gateway's insufficient credits) | if `reset` names a time, wait until then and don't retry before it. `reset: null` (an API key out of credit) needs a top-up |
| `not-installed` | the CLI isn't on your PATH | install it |
| `refused` | typed before any prompt; `reason`: `model-mismatch`, `effort-unsupported`, `model-required`, `no-session` | correct the command |

`usage` is the provider's own token count for the turn, normalized as `{in, out, cache_read,
cache_write, reasoning}` with no overlap (`in` is uncached input), plus `cost_usd` when the
provider reports it (Claude). A Codex turn's usage is the sum of its model calls, including
calls by collaborator agents while the turn is open. The printed line trims `reasoning`; the files
keep it, and the raw vendor usage stays in `stream.jsonl`.

Exit codes: `0` ok, `1` any other status, `2` refused or a usage error.

Nothing is retried for you. A failed resume keeps the original session, so you can send again
later. A launch that failed before its first turn has no session (`send` is refused
`no-session`): start a fresh seat.

## Mail and push

Every new seat's prompt starts with the **seat contract** (`SEAT_BOX` in `hands.py`): the same
text for every seat, telling it how to report and how to mail. The seat's own job-dir line comes
last, after your task, so seats given one task share a prompt prefix.

- **Seat to driver:** a seat appends `{"text":...}` to `DIR/outbox.jsonl`.
  `wait DIR --for mail` returns on it, and a push carries it.
- **Driver to seat:** `hands.sh mail DIR TEXT`. The seat's wrapper delivers each line exactly
  once, as `MAIL from driver`. Mail never launches a seat: to a seat with no live wrapper, the
  line waits in its inbox for the next launch.

The engine opens inboxes and outboxes without following links, regular files only.

**Push.** Every `start` and `send` records the session it runs in, from the variables the host
gives every command it runs: the address that reaches it, and its id. Each turn end and outbox line then pushes one message
into that session, the way its host takes messages from outside:

| Driver | Names its session | A push is |
|---|---|---|
| Claude Code | `CLAUDE_CODE_MESSAGING_SOCKET` and `_TOKEN` (the address), `CLAUDE_CODE_SESSION_ID` (the id) | one post to the session's inbox socket |
| Codex | `CODEX_THREAD_ID` | `codex queue --thread ID --message TEXT`, to the thread on your shared app-server, where the Codex TUI runs unless started with `--no-daemon` |
| OpenCode | `OPENCODE_SESSION_ID` | `opencode api post /api/session/ID/prompt` with `{"text": TEXT, "delivery": "queue"}`, to your background service, where the OpenCode TUI runs unless started with `--standalone` |
| A shell, CI | nothing | none: use `wait` |

The message is line 1 `provider:model tN status: <clip>`, line 2 the job dir, line 3 the id
`<dir>:<turn>`, then the seat's text (up to 4,000 characters) marked as data. Each push's outcome
is a progress event in `events.jsonl`: `posted` (the host took it, which is all any host tells)
or `failed` with the reason. A Codex or OpenCode push command gets 15 seconds. When more than one
host's variables are set, the first row wins.

**When the driver moves.** A Claude Code restart (an app update, a quit and reopen) gives the
resumed session a new socket and token under the same session id, and deletes the old socket.
Each push is logged in `DIR/pushes.jsonl` before it is sent, with a cursor that counts the pushes
the host took. A push that fails stays pending: its event says `failed`, with `pending` counting
what waits, and `ps` shows `N push(es) pending`, for an ended seat in the jobs root too. Pending
pushes go in order, marked `(late Ns)` or `(late Nm)` on line 1, with the seat's next push, when
its wrapper exits or next launches, and with any `hands.sh` command the driver session runs,
which first points the session's seats at its address now (it says so on stderr). So after a
restart, run `hands.sh ps` once. An ended seat started with `--out` outside the jobs root is
found only by its next launch. A seat started before its `DIR/driver` recorded a session id
can't be rebound: `send` to it records the session again.

A seat's own CLI names the seat's session to the commands it runs, so a seat that starts a seat
records no driver (a live seat's session is never pushed to) and gets its answers by `wait`. A
`--probe` never pushes. Claude seats run with `crossSessionInbound: refuse`, so other sessions
can't message them.

Codex and OpenCode pushes are so far tested against stub CLIs only: that a push starts a turn in
an idle Codex or OpenCode session has not been checked live.

## Hooks and memories

Hooks are your code, written for your own sessions, so no seat runs them.
Claude gets `disableAllHooks` in its one `--settings`, and Codex `--disable hooks`.
OpenCode 2.x runs no Claude hooks; its own hooks are plugins, which hands
can't turn off without moving its whole config dir, so check `opencode plugin list` if you add
one. Codex seats also run with `--disable memories`, and Claude seats with auto-memory off.

## OpenCode seats and your background service

An OpenCode seat runs on your normal OpenCode store, the one `opencode serve --service` uses. A
turn killed midway leaves an execution claim, which that background service would resume at its
next start, re-running the seat's tools unseen. hands prevents that two ways:

- **With `--timeout`**, at the deadline the wrapper ends the agent politely (stdin EOF first, TERM
  after a grace period). The turn ends `timeout` with its session, and `send` resumes it.
- **An abandoned turn** (a `stop` mid-turn, a crash, a kill) has its session deleted
  (`opencode session delete <id> --standalone`) before its envelope lands. A later `send` to that
  seat ends `error` (`handshake`): start a fresh seat.

**The one known gap:** if the machine restarts, or the wrapper itself is SIGKILLed, while an
OpenCode seat is mid-turn, your OpenCode background service may continue that turn at its next
start.

## Quota gauges

`hands.sh quota` prints one line per provider: each usage window's used share, longest window
first, and when it resets; `--json` prints the same as one document. It only reads: it never
spends quota and never starts a seat. The provider enforces quota at launch or during a turn.
A seat started with a spent pool ends
`provider-quota-dead` with the reset time.

- **Claude and Codex** need no credential and no extra endpoint: Claude's reading is the newest
  `rate_limit_event` in a Claude seat's own stream, and Codex's is its CLI's app-server answering
  `account/rateLimits/read`. The Claude line says how old its reading is; a window that has reset
  since is dropped, and `hands.sh --probe claude` writes a fresh reading.
- **OpenCode** has no such surface, so `quota.py` reads the token its CLI already keeps and
  calls its **undocumented** usage endpoint (`opencode.ai/zen/go/v1/usage`, the OpenCode Go
  plan). The token goes to `curl` on stdin, never on the command line. Raw responses are never
  printed or stored. This endpoint can change or disappear; a failed reading is `unknown`, never an error.

## Using it from an agent

- **Claude Code:** with the skill linked, say "hands: ask codex to review this" or run `/hands`.
  Turn ends, mail and errors arrive as pushes, so the session doesn't poll. Run real work with
  `start` and never block the session on a foreground `wait`. The skill has the agent run
  `events DIR --follow` as a background command after each `start`, so every seat is a row in
  the task list with its live view, ending when the seat stops.
- **Codex:** skills load from `~/.agents/skills`; invoke `$hands`, or run the script. Turn ends
  arrive as pushes (§ Mail and push).
- **OpenCode:** it reads Claude Code's skills, so the `~/.claude/skills` link serves it too. Turn
  ends arrive as pushes.
- **Any other shell or CI:** it's a plain script. Use `wait` instead of pushes.
- **Anywhere:** `hands.sh ps` lists the live seats, and `hands.sh events DIR --follow` in a
  terminal shows one as it works.

Saying just **`hands`** in a host session runs a guided setup: the quota board, then a few
questions (the task, the seat, the `--cwd` when it's unclear, the effort for an ACP or Codex
seat), and the exact command is shown before it runs.

## Cost

- A seat pays its CLI's start-up prompt once per conversation (about 34k tokens for a lean
  Claude seat, mostly cached). `send` continues the conversation, so a follow-up costs a turn, not
  a new seat.
- `usage.cache_read` shows what each turn read from the provider's prompt cache.
- Pools are per provider: N parallel seats on one provider drain its pool N times as fast. Spread
  parallel work across providers.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `provider-quota-dead` with a `reset` time | wait until then |
| `provider-quota-dead`, `reset: null` (an API key) | the key's account is out of credit; add credits in that provider's billing console |
| `auth-expired` | run `claude`, `codex login` or `opencode auth login` |
| `wait` prints `running` | the turn is still going. A long tool call is alive; a stale `last_event` with no open tool call is stuck: `stop`, then `send` |
| `refused`, `effort-unsupported` or `model-mismatch` | pass an effort the model lists, and a model the agent lists (the refusal names them) |
| `--out ... is not empty` | name a new or empty dir, or drop `--out` for a fresh one |
| an OpenCode `send` after a stop or crash ends `error` (`handshake`) | the abandoned turn's session was deleted: start a fresh seat |
| results gone after a month | the sweep deleted the idle dir; pass `--out` outside the jobs root for keepers |
| Claude warns "workspace not trusted" in `stderr.log` | harmless; trust the dir once interactively to silence it |

## How it works

A seat is three processes: a wrapper, the provider's CLI, and a small reaper between them that
ends the CLI's whole process group when the CLI goes. Starting one:

```
hands.sh start codex task.md
└─ hands.py   makes the job dir, writes prompt.txt and the handle,
   │          then runs `seat.py spawn`
   └─ seat.py spawn ── forks ──► the wrapper (seat.py, detached; it holds DIR/job.log's lock)
      │                          ├─ wires.launch(): the CLI under `seat.py reap`, and its handshake
      │                          ├─ one select loop: in.fifo and inbox.jsonl in,
      │                          │  the CLI's output out to events.jsonl
      │                          └─ each turn's end: turn-N/result.txt, envelope.json, a push
      │                             (logged in pushes.jsonl, then sent)
      └─ prints {"status":"spawned",...} once the wrapper has the first message
```

The first message rides in the launch. A `send` must reach a live seat now or relaunch it, so
it uses `in.fifo`. A nonblocking open tells the sender at once whether the wrapper is reading,
and the wrapper closes the pipe under the delivery lock when it leaves. `mail` writes to
`inbox.jsonl` so it can wait on disk with no seat running. Mail never starts a seat.

| File | What it holds |
|---|---|
| `hands.sh` | the launcher: runs `hands.py` |
| `hands.py` | the command line and its flags, the job dir and its handle, the seat contract (`SEAT_BOX`), the verbs, the events view, the live-seat list (`ps`), the probe and the 30-day sweep |
| `seat.py` | the wrapper loop, deliveries and the delivery lock, turn envelopes and failure typing, outbox reading and push timing, live-seat discovery, and the reaper |
| `push.py` | driver addresses and rebinding, the push log and sender, and the driver host table (`DRIVER_HOSTS`) with its transports |
| `wires.py` | the provider table (`PROVIDERS`), each CLI's command and handshake, the provider clients and event translators, the shared file helpers, and the reaper argument builder |
| `quota.py` | the quota board behind `hands.sh quota` |
| `SKILL.md` | the short guide agent hosts read |
| `tests/` | `test_cli.py` (hands.py end to end), `test_seat*.py` (provider seats and shared wrapper behavior), `test_push.py` (driver pushes), `test_wires.py` (clients and translators), and `test_quota.py`. The tests use `stubs/`, `harness.py`, and the recorded provider output in `fixtures/`. |

To follow one seat through the code, read `main()`, `start_seat()` and `launch()` in `hands.py`, then
`spawn()` and `Wrapper.run()` in `seat.py`, then `launch()` in `wires.py`.

## Job dir reference

These are the files hands and the seat use. Paths are relative to the job dir.

| Path | Who writes it and who reads it |
|---|---|
| `prompt.txt` | hands writes the seat contract, task and job-dir line. The first launch reads it as its message. |
| `session` | The handle. hands writes provider, model, effort, cwd, timeout, CLI path and a null session id. The wrapper saves the id when it learns it. Relaunches read the handle. |
| `in.fifo` | The wrapper creates and reads this named pipe. `send` writes messages for the live wrapper. |
| `inbox.jsonl` | `mail` appends `t`, `from` and `text`. The wrapper reads each new line and logs its line number in the delivery event. |
| `outbox.jsonl` | The seat appends lines with `text`. The wrapper reads them for pushes, and `wait --for mail` reads new lines. |
| `events.jsonl` | The wrapper, sender and push code append the events below. Deliveries, `wait`, `events` and `ps` read them. Its lock orders deliveries. |
| `stream.jsonl` | The provider client logs Claude's stdout or JSON-RPC records with `t`, `dir` (`in` or `out`) and `msg`. Streamed fragments and repeated Codex turn diffs are skipped. Used for diagnosis and Claude quota. |
| `stderr.log` | The wrapper captures CLI stderr and OpenCode session cleanup output. It reads stderr to classify failures. |
| `job.log` | The wrapper's own output. Launch and sweep use its lock to tell whether a wrapper holds the job dir. |
| `driver` | Launch and send record the driver's host, address and session id when available. Pushes read it and rebinding may refresh it. |
| `pushes.jsonl` | The wrapper logs pushes with `at`, `turn`, `kind`, `what` and `text`. The sender reads them and appends `{"sent":K}` as they go. `ps` and rebinding read the pending count. |
| `turn-N/result.txt`, `turn-N/envelope.json` | The wrapper writes the answer (empty on failure) and envelope for turn N. `wait` reads the answer for the turn named in the latest envelope. |
| `envelope.json` | The wrapper writes the latest turn's envelope. hands and the wrapper also use this path for refusals and early failures. hands reads it to report the result or status. |
| `result.txt` | The wrapper points this symlink at the last successful turn's answer for the driver. |
| `*.tmpPID`, `.result.lnk` | Atomic updates use a temporary file beside the destination or a temporary symlink. The writer renames it into place. |

Every event has `t` (local ISO 8601 time with offset), `kind` and `turn` (0 before the
first turn). Fields whose value is null are omitted.

| `kind` | Other fields |
|---|---|
| `turn_start` | `session` is the provider session id. |
| `text`, `thought` | `text` is visible output or reasoning, respectively. |
| `tool_call` | `call` is the call id, `tool` its name and `input` its arguments. |
| `tool_result` | `call`, `tool`, `status` (`ok`, `error` or `cancelled`), `output`, and `input` if learned at completion. `synthetic: true` marks an ACP tool call the client closed locally. |
| `file` | `path`, `tool` and `call` identify a file touched by a tool. |
| `usage` | `usage` has normalized token counts (see [The envelope](#the-envelope)). `cost_usd` is the reported turn cost. `usage_unknown: true` marks an ACP report with no usable counts. |
| `turn_end` | `status`, `text` (the answer, empty on failure), `session`, `usage`, `rc`, `reason` when set, and `final` below. |
| `error` | `text` describes the error. |
| `progress` | `text` describes a delivery, push, tool update, exit or other activity. Additional fields are listed below. |

Progress fields depend on what happened:

- Deliveries add `expect` (the turn due to answer), `bytes` and, for mail, `inbox` (the line number).
- Push outcomes add `push` (`posted` or `failed`) and `id`. Late posts add `late` (seconds).
  Failures add `error` and `pending` (the number still queued).
- The wrapper's idle exit adds `idle: true`. Tool updates may have `call`, `tool` or `input`.
  ACP context updates may have `context_used`, `context_size` or `cost`.

`final` records how the answer was selected or the turn was closed:

- `none` means the wrapper or translator closed a cut or unanswered turn without a provider result.
- `boundary` means Codex reported `turn/completed`, or OpenCode supplied message ids on
  text or thought chunks. For either provider the answer is the last text message if it
  contains an answer and follows any tool events.
- `after-last-tool` means OpenCode supplied no such message ids. The same rule picks the
  answer from the last text message after any tool events.
- `field` means Claude supplied a result record. Its `result` field is the answer on success.

## Design

1. **Every seat has full access.** A seat that can't run commands can't verify its work. Isolation
   comes from the `--cwd`, never from a read-only mode.
2. **Typed failure over retry loops.** Quota errors carry their reset time; waiting beats polling.
3. **The job dir is the only state.** No daemon, no registry, no cache: a relaunch reads the
   handle, the events file is the truth, and `stop` still writes the envelope.
4. **Judgment stays with the driver.** hands collects results; it never votes or routes.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under Apache-2.0: see [LICENSE](LICENSE).
