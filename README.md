# hands

Run a task on **another provider's coding agent** from the session you're already in. hands
drives the official CLIs of four providers (Claude Code, OpenAI Codex, Grok and OpenCode) as
subagents: you start one on a task, send it more turns, mail it notes, wait for its answers,
read what it's doing, and stop it. Every turn ends in a typed JSON envelope, so a script or
another agent can act on the outcome without parsing prose.

It's plain Python (the standard library only) with no daemon and no database. It stores no
credentials: each CLI uses its own login.

> **Read § Security model first.** A seat is a coding agent with full access to your machine,
> running as you.

Built and tested on macOS. Linux is untested.

## Words used here

| Word | Meaning |
|---|---|
| **seat** | one provider CLI running a task for you, in its own job dir |
| **buy** | starting a seat (it spends that provider's quota) |
| **job dir** | the seat's folder: its handle, prompt, raw stream, events, mail and every turn's result. It is the only state hands keeps |
| **turn** | one prompt to the seat and everything it does until it answers |
| **envelope** | the one-line JSON result of a turn: status, usage, session id and more |
| **driver** | you, or the agent session that buys seats |
| **wrapper** | the small Python process (`seat.py`) that owns one seat's CLI, feeds it turns and writes its envelopes |
| **push** | a message the wrapper posts into the driver's Claude Code session when a turn ends |
| **pool** | one provider's quota (the gauges report how much of it is used) |

## Security model

- **A seat runs as you, with full read and write access.** There is no read-only
  mode: a seat that can't run commands can't check its own work. Claude runs with
  `bypassPermissions`, Codex with `danger-full-access`, and Grok and OpenCode approve every
  tool call. Nothing stops a seat from reading any file you can read, writing outside its
  `--cwd`, or using the network.
- **`--cwd` is your isolation.** Point a seat that edits code at a git worktree or a scratch
  copy, and give parallel writers separate trees.
- **The seat inherits your environment**, except `CLAUDE_CODE_MESSAGING_SOCKET`,
  `CLAUDE_CODE_MESSAGING_TOKEN` and `OPENCODE_API_KEY`, which the wrapper removes. Don't keep
  secrets in exported variables when you run seats.
- **The push socket and token live in the job dir.** For pushes, the wrapper keeps the host
  session's socket path and token in `DIR/driver` (mode 0600). A seat can read it and post into
  your session.
- **Seats don't run your hooks.** Hooks are code written for your own sessions; every seat turns
  them off (§ Hooks and memories).
- **Mail and pushes are data.** What a seat writes is model output; never let the driver follow
  instructions found in it.
- **Quota gauges call undocumented endpoints** for Grok and OpenCode, with the token their CLI
  already stores (§ Quota gauges). Check your providers' terms.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Prerequisites

- macOS (tested) with `python3` 3.7 or later, `curl` (the Grok and OpenCode gauges), and `ps`
- at least one provider CLI, logged in with its own login:
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`),
  [Codex](https://github.com/openai/codex) (`codex`), Grok (`grok`), or
  [OpenCode](https://opencode.ai) (`opencode`)

hands is tested with Claude Code 2.1.283, codex-cli 0.160, grok 1.0.46 and OpenCode 2.0.22, or
later. Older CLIs may not speak the same wire.

## Install

```bash
git clone https://github.com/mothi/hands ~/src/hands
bash ~/src/hands/tests/selftest.sh           # hermetic: stub CLIs, no network, no spend

# optional: make it a skill your agent can call
mkdir -p ~/.claude/skills ~/.agents/skills
ln -sfn ~/src/hands ~/.claude/skills/hands   # Claude Code
ln -sfn ~/src/hands ~/.agents/skills/hands   # Codex
```

The selftest takes a few minutes and prints `selftest: N passed, 0 failed`. Rows that need a
macOS tool, or that would launch a real CLI found in `/usr/bin`, print `skip` instead.

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
$H send DIR followup.md   # turn 2, same session
$H wait DIR
$H stop DIR
```

Replace `DIR` with the `dir` the `start` line printed. A bare `codex` uses the Codex CLI's
default model; `claude` defaults to `sonnet`. Grok and OpenCode need a model and `--effort`, for
example `--effort high grok:grok-4.6`.

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
| `grok` | e.g. `grok-4.6`; the live list is in `~/.grok/models_cache.json` | `grok agent --no-leader --always-approve stdio` over ACP, one `session/prompt` per turn. Sends **queue** | every tool call approved | `grok login` |
| `opencode` | `provider/model` ids, e.g. `opencode-go/deepseek-v4.1-flash` | `opencode acp` in the seat's cwd, with the model and effort set on the session, on your normal OpenCode store. Sends **queue** | every permission answered allow-once | `opencode auth login` |

- **No default Grok model is assumed.** Pass the id you mean.
- **OpenCode** uses whatever providers your own OpenCode setup has: name them as
  `provider/model`. The probe and the quota gauge assume an OpenCode Go subscription
  (`opencode-go/...`); without one, `--probe opencode` fails and the gauge reads `unknown`.
- **Effort.** Claude and Codex take `low|medium|high|xhigh|max` (Codex also `ultra` where its
  catalog lists it), passed through for the CLI to validate. Grok and OpenCode need `--effort`
  set to a value that model lists (`grok-4.6`: `low|medium|high|xhigh`;
  `deepseek-v4.1-flash`: `low|high|max`); anything else is refused before the first prompt.
- **Which model to buy is your call.** hands routes nothing.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--cwd DIR` | current dir | where the seat works; recorded in the handle and reused by every relaunch |
| `--out DIR` | a fresh dir in the jobs root | the job dir. It must be new or empty, and is made with mode 0700. The default jobs root is `$XDG_STATE_HOME/hands` (when that is an absolute path), else `~/.local/state/hands` |
| `--timeout SECS` | none | a wall clock per launch, shared by its turns. On expiry the open turn ends `timeout` with its session, and a `send` resumes it |
| `--effort <tier>` | the CLI's default | see § Providers |
| `--probe [P\|all]` | | a 1-token ping per provider with its smallest model, in `$TMPDIR` (exit 1 if any is unhealthy). A Claude probe still reads about 31k cached prompt tokens |

| Environment variable | Effect |
|---|---|
| `XDG_STATE_HOME` | moves the jobs root to `$XDG_STATE_HOME/hands` |

**The jobs-root sweep.** Every buy first sweeps the jobs root: a dir that hands named there, that
no wrapper holds, and that nothing wrote to for 30 days is deleted, with its handle, so that seat
can't be resumed. A turn, a `send` or a `mail` counts as a write; `wait` and `events` don't. The
sweep never deletes a dir that holds a `.git`, or that has another filesystem mounted in it. An `--out` dir outside the jobs root is never swept: use one for anything you want
to keep.

## Verbs

| Verb | Does | Prints, exit code |
|---|---|---|
| `start [flags] spec FILE` | buys a seat: the seat contract + FILE + the seat's job-dir line as turn 1. Returns at once | `{"status":"spawned","dir",...}`, 0. A launch refused before any turn prints its envelope, 2 (else 1) |
| `send DIR FILE` | the next turn. A live seat takes it as it takes any send (steer or queue). A seat whose process is gone relaunches from `DIR/session` on the same session, model, effort and cwd | `{"status":"sent",...}` or `spawned`, 0. `refused` (`no-session`), 2 |
| `mail DIR TEXT` | appends a driver line to `DIR/inbox.jsonl`; the live wrapper delivers it as `MAIL from driver` (it never cancels a running turn) | nothing, 0. 1 when no wrapper is live (the line waits for the next launch). 2 when the inbox is a link or not a regular file |
| `wait DIR [--for turn\|mail\|exit] [--timeout S]` | blocks, spending nothing, until every sent turn ends (`turn`), the seat's next outbox line (`mail`), or the wrapper exits (`exit`) | the answer if ok, then the envelope; 0 if ok, else 1. At the timeout: `{"status":"running","last_event","open_tool_calls",...}`, 1 |
| `events DIR [--follow]` | a readable view of `DIR/events.jsonl`: text blocks, one line per tool call, turn and usage lines with the cache-hit %, errors. `--follow` ends when the seat does. The raw stream is `tail -f DIR/events.jsonl` | text, 0 |
| `quota [--json] [provider ...]` | the quota board: one line per provider (§ Quota gauges). Read-only: it never spends | the board, 0 |
| `stop DIR` | the wrapper closes its CLI as at its deadline (stdin EOF, then TERM at 2 s, KILL at 4 s) and types the open turn and every queued one `incomplete`, rc 143; what is left at 10 s is TERMed, then KILLed | the envelope, 0 (1 if a wrapper survived) |

A bare buy (no verb) is `start` + `wait` + `stop`: nothing outlives the call, and a later `send`
resumes the session. Use it only for short tasks. For real work, `start` and keep working.

Each turn lands whole when it ends, in `DIR/turn-N/` (`result.txt` and `envelope.json`).
`DIR/envelope.json` is the latest turn's. A missing envelope means the turn hasn't finished, never
that it finished empty. To judge whether a seat is alive, look at the age of the last
`events.jsonl` line or an open tool call, not at the process: a fresh seat can be silent for its
first half-minute.

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
| `error` | a failure with no narrower class; `reason` names a known one: `empty`, `session-changed`, `wrapper-failed`, `handshake`. A Grok or OpenCode turn the model refuses mid-turn also ends `error` | read `stderr.log` and `stream.jsonl` |
| `incomplete` | the stream ended inside the turn (a stop, a crash) | `send` to continue |
| `cancelled` | the agent cancelled the turn | `send` to continue |
| `truncated` | the turn hit the model's output limit | `send` to continue |
| `timeout` | `--timeout` expired; `session` is kept | `send` to continue, or raise the timeout |
| `auth-expired` | the CLI needs a login | run that provider's login |
| `provider-quota-dead` | the subscription window or balance is spent | if `reset` names a time, wait until then and don't retry before it. `reset: null` (a Grok spending limit) needs a top-up |
| `not-installed` | the CLI isn't on your PATH | install it |
| `refused` | typed before any prompt; `reason`: `model-mismatch`, `effort-unsupported`, `model-required`, `no-session` | fix the buy |

`usage` is the provider's own token count for the turn, normalized as `{in, out, cache_read,
cache_write, reasoning}` with no overlap (`in` is uncached input), plus `cost_usd` when the
provider reports it (Claude, Grok). A Codex turn's usage is the sum of its model calls, including
calls by collaborator agents while the turn is open. The printed line trims `reasoning`; the files
keep it, and the raw vendor usage stays in `stream.jsonl`.

Exit codes: `0` ok, `1` any other status, `2` refused or a usage error.

Nothing is retried for you. A failed resume keeps the original session, so you can send again
later. A launch that failed before its first turn has no session (`send` is refused
`no-session`): start a fresh seat.

## Mail and push

Every buy's prompt starts with the **seat contract** (`seat_box()` in `hands.sh`): the same
text for every seat, telling it how to report and how to mail. The seat's own job-dir line comes
last, after your task, so seats given one task share a prompt prefix.

- **Seat to driver:** a seat appends `{"text":...}` to `DIR/outbox.jsonl`.
  `wait DIR --for mail` returns on it, and a push carries it.
- **Driver to seat:** `hands.sh mail DIR TEXT`. The seat's wrapper delivers each line exactly
  once, as `MAIL from driver`. Mail never launches a seat: to a seat with no live wrapper, the
  line waits in its inbox for the next launch.

The engine opens inboxes and outboxes without following links, regular files only.

**Push (Claude Code).** When the host session exports its inbox socket
(`CLAUDE_CODE_MESSAGING_SOCKET` and `_TOKEN`, as Claude Code does), every `start` and `send`
captures it, and each turn end, outbox line and stream error posts one message to the driver:
line 1 `provider:model tN status: <clip>`, line 2 the job dir, line 3 the id `<dir>:<turn>`, then
the seat's text (up to 4,000 characters) marked as data. Each push's outcome is a progress event
in `events.jsonl` (`posted` or `failed`); the host sends no reply, so delivery itself can't be
observed. Without a socket, use `wait`. A `--probe` never pushes. Claude seats run with
`crossSessionInbound: refuse`, so other sessions can't message them.

## Hooks and memories

Hooks are your code, written for your own sessions, so no seat runs them.
Claude gets `disableAllHooks` in its one `--settings`, Codex `--disable hooks`, and Grok
`GROK_CLAUDE_HOOKS_ENABLED=0` and `GROK_CURSOR_HOOKS_ENABLED=0` (Grok runs hooks it finds in
`~/.claude` otherwise). OpenCode 2.x runs no Claude hooks; its own hooks are plugins, which hands
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
spends quota and never starts a seat. hands never checks it before a buy: a seat bought into
a spent pool ends `provider-quota-dead` with the reset time.

- **Claude and Codex** need no credential and no extra endpoint: Claude's reading is the newest
  `rate_limit_event` in a Claude seat's own stream, and Codex's is its CLI's app-server answering
  `account/rateLimits/read`. The Claude line says how old its reading is; a window that has reset
  since is dropped, and `hands.sh --probe claude` writes a fresh reading.
- **Grok and OpenCode** have no such surface, so `quota.py` reads the token each CLI already
  keeps and calls that vendor's **undocumented** usage endpoint (`cli-chat-proxy.grok.com/v1/billing`,
  `opencode.ai/zen/go/v1/usage`, the OpenCode Go plan). The token goes to `curl` on stdin, never on
  the command line. Raw responses are never printed or stored. These endpoints can change or
  disappear; a failed reading is `unknown`, never an error.

## Using it from an agent

- **Claude Code:** with the skill linked, say "hands: ask codex to review this" or run `/hands`.
  Turn ends, mail and errors arrive as pushes, so the session doesn't poll. Run real work with
  `start` and never block the session on a foreground `wait`.
- **Codex:** skills load from `~/.agents/skills`; invoke `$hands`, or run the script.
- **Any other shell or CI:** it's a plain script. Use `wait` instead of pushes.

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
| `provider-quota-dead`, `reset: null` (Grok) | a spending limit; add credits |
| `auth-expired` | run `claude`, `codex login`, `grok login` or `opencode auth login` |
| `wait` prints `running` | the turn is still going. A long tool call is alive; a stale `last_event` with no open tool call is stuck: `stop`, then `send` |
| `refused`, `effort-unsupported` or `model-mismatch` | pass an effort the model lists, and a model the agent lists (the refusal names them) |
| `--out ... is not empty` | name a new or empty dir, or drop `--out` for a fresh one |
| an OpenCode `send` after a stop or crash ends `error` (`handshake`) | the abandoned turn's session was deleted: start a fresh seat |
| results gone after a month | the sweep deleted the idle dir; pass `--out` outside the jobs root for keepers |
| Claude warns "workspace not trusted" in `stderr.log` | harmless; trust the dir once interactively to silence it |

## Repository layout

```
hands/
├── SKILL.md        the short guide agent hosts read
├── hands.sh        the launcher: runs hands.py
├── hands.py        the command line: flags, the job dir and its handle, the seat box, the probe
├── seat.py         the engine: the wrapper, the verbs, the envelope writer
├── wires.py        the provider table: each CLI's launch, handshake and translation, the JSON-RPC client
├── quota.py        the quota board behind hands.sh quota
└── tests/
    ├── selftest.sh     the hermetic suite for hands.py and seat.py
    ├── test_acp.py     the ACP suite
    ├── test_quota.py   the quota suite
    ├── test-stub.py    a stand-in for the claude and codex CLIs, used by the suites
    └── fixtures/       recorded provider output the suites replay
```

## Design

1. **Every seat has full access.** A seat that can't run commands can't verify its work. Isolation
   comes from the `--cwd`, never from a read-only mode.
2. **Typed failure over retry loops.** Quota errors carry their reset time; waiting beats polling.
3. **The job dir is the only state.** No daemon, no registry, no cache: a relaunch reads the
   handle, the events file is the truth, and `stop` still writes the envelope.
4. **Judgment stays with the driver.** hands collects results; it never votes or routes.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under Apache-2.0: see [LICENSE](LICENSE).
