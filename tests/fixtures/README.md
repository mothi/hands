# fixtures — recorded provider output the suites replay

Every file a test reads lives here, with a row below naming where it came from and which tests
read it; no test reads a path outside the repo.

**Sanitized.** Every recording was stripped of personal data before it was committed: work paths
became `/w`, the home dir `/Users/u` and the host name `host.local`; timestamps are in UTC;
thinking signatures and machine ids are emptied or zeroed; Claude init lines keep only core tools;
model catalogs keep only OpenCode's own providers; MCP server names became `mcp-a`, `mcp-b`, ...;
plan tiers and some quota values were re-valued. Structure, ids, event order, statuses and usage are
otherwise as recorded. New recordings get the same treatment (see CONTRIBUTING.md).

**What a recording holds.** A JSON-RPC recording is a seat's `stream.jsonl`, which leaves out
streamed fragments and Codex's turn diff (`repeats()` in `wires.py`). The Codex recording here
predates that and still holds its fragments; the suites replay it the same either way.

**Error wording.** `FailureText` in `tests/test_seat.py` asserts that each provider error string
classifies to its typed status. When a provider changes its wording, add the new error text here
the day it is found.

The test classes named below are in `tests/test_seat.py` unless another file is named.

| fixture | origin | read by |
|---|---|---|
| claude-session-limit | live seat (agent notice wording, time zone set to UTC) | `FailureText`, with its reset |
| codex-usage-limit | wording approximated from a live record | `FailureText`, with its reset; `CodexSeats`: failures typed from error fields |
| claude-api-credit-low | Anthropic's documented credit-balance error, as Claude Code prints an API error (approximated, not recorded) | `FailureText`; `ClaudeSeats`: an API-key seat out of credit |
| codex-api-insufficient-quota | OpenAI's documented `insufficient_quota` body behind a 429 (approximated, not recorded) | `FailureText`; `CodexSeats`: an API-key seat out of credit |
| opencode-openrouter-402 | OpenRouter's insufficient-credits wording in an ACP error (approximated, not recorded) | `FailureText`; `AcpLaunch`: a usage limit answering a prompt |
| claude-oauth-expired | a seat that died on an expired login (its result.txt) | `FailureText` |
| opencode-missing-api-key | an OpenCode ACP seat on an empty store, never logged in: the error answering `session/prompt` | `AcpLaunch`: a missing login is auth-expired |
| opencode-go-no-credential | an OpenCode 2.0.22 buy with no Go key in its store | `FailureText` |
| opencode-acp-seat-sample | opencode 1.18.30 `opencode acp` seat (opencode-go/deepseek-v4.1-flash, variant low), 4 turns on one process: two replies, a shell `sleep` the client cancelled, a file write; `{t,dir,msg}` lines | the opencode stub's default transcript: `AcpSeats`, `AcpLaunch`, `AcpClose`, `SlowLaunch` |
| opencode-acp-resume-sample | the same session reattached by a new process: initialize, session/resume, effort set again, one prompt | `AcpSeats`: a send to a stopped OpenCode seat; `AcpClose`: an abandoned turn, a seat that ends with its turn; `Pushes`: a relaunch |
| opencode-acp-sample | a 4-turn ACP transcript at the CLI's default effort | `tests/test_wires.py` `AcpClient`: the round trip |
| claude-stdin-sample | claude 2.1.274 `--input-format stream-json` seat (haiku), 3 turns on one process with Bash tool calls | `ClaudeSeats`: a recorded three-turn seat, last answer `ZEBRA-42` |
| codex-appserver-sample | codex-cli 0.160.0 app-server on stdio (read-only, approvals never), two processes, four turns: one steered mid-turn (a wrong-id steer refused), one interrupted, one after it, one on a new process's `thread/resume` | `CodexSeats`: a recorded wire across a relaunch (ok/cancelled/ok/ok; strays; per-turn usage); the stream log leaves out fragments and the re-sent diff (the test adds two `turn/diff/updated` to turn 1) |
| opencode-seat-events-sample | derived, not recorded: the `events.jsonl` (115 lines) of a stub seat replaying opencode-acp-seat-sample through `tests/stubs/opencode` as `AcpSeats` runs it (start with `--idle 60`, three sends, stop), under `TZ=UTC` | `tests/test_cli.py` `EventsView`: a long stream renders to a few readable lines |
| claude-events-sample | a 3-turn seat's `events.jsonl` on Claude (43 lines, a mail round) | `tests/test_cli.py` `EventsView` |
| opencode-events-sample | the same 3 turns on OpenCode (34 lines); tool inputs arrive in a later update | `tests/test_cli.py` `EventsView`: a late input rides one ↳ line |
| claude-stream-sample | `claude -p --output-format stream-json`, one turn | `ClaudeSeats`: a recorded claude stream |
| claude-stream-ratelimit.jsonl | a claude 2.1.300 haiku probe (hook events before init) | test_quota.py test_41: 5h 12.0, week 37.0; `resetsAt` shifted by one constant so the reading predates no reset |
| claude-stream-fable-scoped.jsonl | the last `rate_limit_event` of a fable seat | test_quota.py test_48: `seven_day_overage_included` 97 limits one model and is listed apart; week 58 leads the line |

## Quota gauge bodies (`tests/test_quota.py`)

When a provider's usage response changes shape, add the new body here the day it is found. Codex
identity values are fake by construction; a test asserts they never reach `quota.py` output.

| fixture | origin | expected |
|---|---|---|
| quota-codex-happy.json | an app-server `account/rateLimits/read` answer (codex-cli 0.160); identities and free text faked | ok; week 40 |
| quota-codex-two-window.json | a 5-hour primary and weekly secondary window, in the app-server shape; identities faked | test_43, test_45: windows named by their length, never by the provider's key |
| quota-codex-absent-pct.json | synthetic: a window with no used percent | unknown, never a made-up 0 |
| quota-codex-no-login.json | an app-server answer with an empty `CODEX_HOME` (JSON-RPC error) | unknown; `why` is its message (test_13) |
| quota-claude-429.json | an Anthropic error envelope (shape approximated); served by the opencode stub, since the parse is the same for every pool | unknown; `why` is the provider message |
| quota-opencode-happy.json | OpenCode Go usage shape with distinct synthetic values | rolling 0, weekly 8, monthly 22 |
| quota-opencode-404.html | the page OpenCode's site serves for a missing endpoint | unknown; the reply is not JSON |
