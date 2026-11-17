# Test fixtures

These files preserve recorded provider output and documented test inputs. The tables give
the provenance of each fixture.

**Sanitization.** Work paths became `/w`, the home dir `/Users/u` and the host name `host.local`.
Timestamps are in UTC. Thinking signatures and machine ids are emptied or zeroed. Claude init
lines keep only core tools. Model catalogs keep only OpenCode's own providers. MCP servers
became `mcp-a`, `mcp-b` and so on. Plan tiers and some quota values were re-valued.
Structure, ids, event order, statuses and usage are otherwise as recorded. See
[CONTRIBUTING.md](../../CONTRIBUTING.md) for new recordings.

JSON-RPC recordings use the seat's `stream.jsonl` format. The Codex recording preserves
streamed fragments. The current logger omits fragments and repeated turn diffs.

## Provider streams, errors and events

| Fixture | Origin |
|---|---|
| `claude-session-limit.txt` | live seat (agent notice wording, time zone set to UTC) |
| `codex-usage-limit.txt` | wording approximated from a live record |
| `claude-api-credit-low.txt` | Approximation of Anthropic's documented credit-balance error in the format Claude Code uses for API errors. |
| `codex-api-insufficient-quota.txt` | Approximation of OpenAI's documented `insufficient_quota` response body for HTTP 429. |
| `opencode-openrouter-402.txt` | Approximation of OpenRouter's insufficient-credits wording in an ACP error. |
| `claude-oauth-expired.txt` | The `result.txt` from a seat that died on an expired login. |
| `opencode-missing-api-key.txt` | The `session/prompt` error from an OpenCode ACP seat on an empty store without a login. |
| `opencode-go-no-credential.txt` | an OpenCode 2.0.22 one-shot run with no Go key in its store |
| `opencode-acp-seat-sample.jsonl` | OpenCode 1.18.30 ACP seat with `opencode-go/deepseek-v4.1-flash` at low effort. Four turns in one process cover two replies, a shell `sleep` the client cancelled and a file write. The records have `t`, `dir` and `msg`. |
| `opencode-acp-resume-sample.jsonl` | The `opencode-acp-seat-sample.jsonl` session reattached by a new process. It initializes, resumes, sets effort again and runs one prompt. |
| `opencode-acp-sample.jsonl` | a 4-turn ACP transcript at the CLI's default effort |
| `claude-stdin-sample.jsonl` | claude 2.1.274 `--input-format stream-json` seat (haiku), 3 turns on one process with Bash tool calls |
| `codex-appserver-sample.jsonl` | Codex CLI 0.160.0 app-server on stdio with read-only access and approvals set to never. Two processes ran four turns. The first was steered and also refused a wrong-id steer. The second was interrupted, the third followed it and the fourth used `thread/resume` in a new process. |
| `opencode-seat-events-sample.jsonl` | Derived from `opencode-acp-seat-sample.jsonl` by replaying it with `tests/stubs/opencode` under `TZ=UTC`. Its 115 event lines came from a start with `HANDS_IDLE=60`, three sends and a stop. |
| `claude-events-sample.jsonl` | a 3-turn seat's `events.jsonl` on Claude (43 lines, a mail round) |
| `opencode-events-sample.jsonl` | The same three turns on OpenCode (34 lines). Tool inputs arrive in a later update. |
| `claude-stream-sample.jsonl` | `claude -p --output-format stream-json`, one turn |
| `claude-stream-ratelimit.jsonl` | A Claude Code 2.1.300 Haiku probe with hook events before init. The `resetsAt` values were shifted by the same constant to place the reading before both resets. |
| `claude-stream-fable-scoped.jsonl` | The last `rate_limit_event` of a Fable seat, including the `seven_day_overage_included` model limit. |

## Quota response bodies

Codex identity fields in these fixtures are synthetic.

| Fixture | Origin |
|---|---|
| `quota-codex-happy.json` | A Codex CLI 0.160 app-server `account/rateLimits/read` answer with synthetic identities and free text. |
| `quota-codex-two-window.json` | A 5-hour primary and weekly secondary window in the app-server format with synthetic identities. |
| `quota-codex-absent-pct.json` | synthetic: a window with no used percent |
| `quota-codex-no-login.json` | an app-server answer with an empty `CODEX_HOME` (JSON-RPC error) |
| `quota-claude-429.json` | Approximation of an Anthropic error envelope, served by the OpenCode stub for the shared response parser. |
| `quota-opencode-happy.json` | OpenCode Go usage format with distinct synthetic values for rolling, weekly and monthly windows. |
| `quota-opencode-404.html` | the page OpenCode's site serves for a missing endpoint |
