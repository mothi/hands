# fixtures — recorded provider output the suites replay

Every file a test reads lives here, with a row below naming where it came from and which rows read
it; no test reads a path outside the repo.

**Sanitized.** Every recording was stripped of personal data before it was committed: work paths
became `/w`, the home dir `/Users/u` and the host name `host.local`; timestamps are in UTC;
thinking signatures and machine ids are emptied or zeroed; Claude init lines keep only core tools;
model catalogs keep only OpenCode's own providers; MCP server names became `mcp-a`, `mcp-b`, ...;
plan tiers and some quota values were re-valued. Structure, ids, event order, statuses and usage are
otherwise as recorded. New recordings get the same treatment (see CONTRIBUTING.md).

**Error wording.** `tests/selftest.sh` asserts that each provider error string classifies to its typed
status. When a provider changes its wording, add the new error text here the day it is found.

| fixture | origin | read by |
|---|---|---|
| grok-402-balance | live seat (verbatim error) | provider-quota-dead |
| grok-403-spending | live probe (verbatim error) | provider-quota-dead |
| claude-session-limit | live seat (agent notice wording, time zone set to UTC) | provider-quota-dead + reset |
| codex-usage-limit | wording approximated from a live record | provider-quota-dead + reset |
| grok-auth-expired | grok's documented login wording | auth-expired |
| claude-oauth-expired | a seat that died on an expired login (its result.txt) | auth-expired |
| opencode-missing-api-key | an OpenCode ACP seat on an empty store, never logged in: the error answering `session/prompt` | auth-expired (row 129) |
| opencode-go-no-credential | an OpenCode 2.0.22 buy with no Go key in its store | auth-expired (row 15c) |
| grok-acp-seat-sample | grok 1.0.41 `grok agent stdio` seat (grok-4.6, effort low), 4 turns on one process: two replies, a shell `sleep` the client cancelled, a file write; `{t,dir,msg}` lines | hands.sh rows 100-106; rows 107, 110, 111, 202 and 205 derive from it |
| grok-acp-resume-sample | the same session reattached by a new process: initialize, session/resume, effort set again, one prompt | row 107: a send to a gone grok seat |
| opencode-acp-seat-sample | opencode 1.18.30 `opencode acp` seat (opencode-go/deepseek-v4.1-flash, variant low), the same 4 turns | rows 101-106: usage excludes reasoning; a cancel's zeros read as unknown usage; OpenCode closes its own aborted tool |
| opencode-acp-resume-sample | the same for OpenCode: a new process reattaching the seat's session | row 122: a send to a gone OpenCode seat |
| grok-acp-sample, opencode-acp-sample | 4-turn ACP transcripts at the CLI's default effort | `test_acp.py` (the ACP client and translation) |
| claude-stdin-sample | claude 2.1.274 `--input-format stream-json` seat (haiku), 3 turns on one process with Bash tool calls | row 84: three ok turn ends, last answer `ZEBRA-42` |
| codex-appserver-sample | codex-cli 0.160.0 app-server on stdio (read-only, approvals never), two processes, four turns: one steered mid-turn (a wrong-id steer refused), one interrupted, one after it, one on a new process's `thread/resume` | rows 114 and 158 (test-stub.py `CX_REPLAY`): turn ends ok/cancelled/ok/ok; items after their turn completed are strays; a turn's usage is the sum of its model calls' `last` |
| grok-events-sample | a 3-turn grok-4.6 seat's `events.jsonl` (228 lines, 3 tool calls, a mail round) | rows 135-136: the readable view is deterministic |
| claude-events-sample | the same 3 turns on Claude (43 lines) | row 136 |
| opencode-events-sample | the same 3 turns on OpenCode (34 lines); tool inputs arrive in a later update | row 136: a late input rides one ↳ line |
| claude-stream-sample | `claude -p --output-format stream-json`, one turn | row 49d: the final message and the session |
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
| quota-grok-happy.json | a live billing response | ok; weekly 71 |
| quota-grok-fresh-period.json | a weekly rollover: the gateway omits a zero usage field | ok; weekly used 0 |
| quota-claude-429.json | an Anthropic error envelope (shape approximated); served by the grok stub, since the parse is the same for every pool | unknown; `why` is the provider message |
| quota-opencode-happy.json | OpenCode Go usage shape with distinct synthetic values | rolling 0, weekly 8, monthly 22 |
| quota-opencode-404.html | the page OpenCode's site serves for a missing endpoint | unknown; the reply is not JSON |
