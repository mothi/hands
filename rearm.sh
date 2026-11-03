#!/usr/bin/env bash
# rearm — sense-and-heal for the quota gauge. Reads quota.sh, fires the one minimal call
# that re-arms each `token-stale` pool, then prints the healed board. Composes per
# hands' capability freeze (new capability = a new script that composes; hands.sh
# and quota.sh are never edited). An operational fault with a RECORDED REMEDY is fixed
# in-line — a token-stale gauge is healed, never reported as news.
#
#   rearm.sh              sense, heal, print the healed board
#   rearm.sh --pre-buy    MECHANICAL routing check for every seat launch. SENSE AND WARN
#                         ONLY — it never heals and never fires inference. One bounded
#                         probe, one line per pool that is not `proceed` — `QUOTA GATE:`
#                         for would-gate (AT the hard stop: the provider itself would
#                         refuse), `QUOTA WARN:` for everything else (would-warn = hot but
#                         buyable, token-stale, UNKNOWN) — and NOTHING AT ALL when the
#                         board is clean. Never blocks — though it has a consumer
#                         that does: hands.sh's pre-flight refuses buys on GATE lines
#                         only; every WARN line passes through to stderr as advice.
#   rearm.sh --pools "P…" either mode, sensing (and healing) only those pools — quota.sh's
#                         own --pools; hands.sh's pre-flight passes the ONE pool it buys
#   rearm.sh --selftest   hermetic: PATH-farm CLI stubs + a stubbed gauge, zero spend
#
# WHY --pre-buy DOES NOT HEAL: a heal is an inference call,
# and on the mechanical launch path it was unbounded in time and unpinned in privilege. The
# warn line CARRIES ITS OWN REMEDY instead — a stale pool prints the exact rearm command —
# so the fix stays mechanical while the hot path
# holds zero inference. Healing happens in board mode, where a human is reading.
#
# WHY ONE CALL HEALS: the CLIs own token refresh, so a stale cache is re-armed by that
# provider's next inference call (quota.sh header), and the claude
# reading IS the rate_limit_event a claude call leaves in its job stream. rearm
# never mints or exchanges a credential itself. Every heal is `hands.sh --probe <pool>` —
# hands' own aliveness ping: its ONE invocation table (its pinned models, a neutral cwd, its
# bounded timeout that reaps the process group), never a second copy of any CLI's argv here.
# A claude probe writes <the jobs root>/probe-*-claude/stream.jsonl, exactly where the gauge
# reads, so the heal needs no state file of its own.
# NO CACHE, NO STATE: quota.sh probes live on every call. --pre-buy probes exactly once;
# board mode probes once to sense the stale pools and once more to print after any heal.
# Exit 0 whenever the mode's output was produced — the gauge's observe-only contract,
# inherited. Board mode exits non-zero ONLY when the gauge cannot be read; --pre-buy never
# does, because a routing hint must never be able to cost a seat. A refresh that fails is
# REPORTED, never fatal, and a pool that is not token-stale is never touched.
# Test seams: REARM_QUOTA_BIN (alternate gauge), REARM_PROBE_T (the --pre-buy probe's alarm,
# so a hang is testable in about a second). The selftest pins PATH, HOME, TMPDIR and XDG_STATE_HOME instead.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SELF="$HERE/$(basename "$0")"
QUOTA="${REARM_QUOTA_BIN:-$HERE/quota.sh}"
PROBE_T="${REARM_PROBE_T:-25}"
SELFTEST=0 MODE=board HEALED=0 JS="" STALE="" POOLS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --pre-buy) MODE=prebuy ;;
    --pools) POOLS="${2:-}"; shift ;;
    --selftest) SELFTEST=1 ;;
    -h|--help) sed -n '/^# rearm — /,/^set -u/p' "$0" | grep '^#'; exit 0 ;;
    *) echo "rearm: unknown arg $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

with_t() { perl -e 'alarm shift; exec @ARGV' "$@"; }   # hands.sh's own timeout idiom
say() { echo "$*"; }

sense() {  # $1 = optional alarm seconds → JS + STALE. rc 0 ok, 2 unreadable, 3 timed out.
  local qrc prc lim="${1:-}"
  # The alarm below bounds the GAUGE PROCESS, not a grandchild that inherited the stdout
  # pipe: such a child would hold the command substitution open past the kill. Latent
  # today — quota.sh backgrounds nothing — and recorded here rather than built against.
  if [ -n "$lim" ]; then JS="$(with_t "$lim" "$QUOTA" --json ${POOLS:+--pools "$POOLS"} 2>/dev/null)"; qrc=$?
  else                   JS="$("$QUOTA" --json ${POOLS:+--pools "$POOLS"} 2>/dev/null)"; qrc=$?; fi
  [ "$qrc" -eq 142 ] && return 3                      # perl alarm kill = the gauge hung
  STALE="$(printf '%s' "$JS" | python3 -c '
import json, sys
for p in json.load(sys.stdin).get("pools", []):
    if str(p.get("why") or "").startswith("token-stale"): print(p["pool"])
' 2>/dev/null)"; prc=$?
  [ "$qrc" -eq 0 ] && [ "$prc" -eq 0 ] || return 2
  return 0
}

heal() {  # one hands probe per stale pool (the WHY ONE CALL HEALS paragraph above)
  local pool t0 secs out st dir
  HEALED=0
  for pool in $STALE; do
    t0=$(date +%s)
    # the probe line is the verdict: {"status":"ok",...} healed; anything else is hands' own
    # typed envelope (not-installed, auth-expired, provider-quota-dead, timeout...) and its job dir.
    # A pool hands has no CLI for never reaches a call: it reads not-installed (fails closed).
    out="$(bash "$HERE/hands.sh" --probe "$pool" </dev/null 2>/dev/null | tail -1)"
    st="$(printf '%s' "$out" | jq -r '.status // empty' 2>/dev/null)"
    dir="$(printf '%s' "$out" | jq -r '.dir // empty' 2>/dev/null)"
    secs=$(( $(date +%s) - t0 ))
    if [ "$st" = ok ]; then
      HEALED=$((HEALED+1)); say "rearm: $pool token-stale → refreshed (${secs}s)"
    else
      say "rearm: $pool refresh FAILED (${st:-no envelope} after ${secs}s${dir:+; see $dir}) — board re-read below"
    fi
  done
}

run_board() {
  sense || return 2
  heal
  # a ping changed the world (the CLI refreshes its token before the call can fail), so the
  # board is always re-read for printing: one more zero-spend probe, ONE formatter (quota.sh)
  "$QUOTA" ${POOLS:+--pools "$POOLS"}
  return 0
}

run_prebuy() {  # SENSE AND WARN ONLY. No heal, no inference, bounded, never blocks.
  sense "$PROBE_T"
  case $? in
    0) : ;;
    3) echo "QUOTA WARN: board unreadable (gauge timeout) — routing blind"; return 0 ;;
    *) echo "QUOTA WARN: board unreadable (gauge unrunnable) — routing blind"; return 0 ;;
  esac
  printf '%s' "$JS" | SELF_E="$SELF" python3 -c '
import json, os, sys
self = os.environ["SELF_E"]
for p in json.load(sys.stdin).get("pools", []):
    # a stale pool carries its own remedy: the driver runs one command, nobody escalates
    if str(p.get("why") or "").startswith("token-stale"):
        print("QUOTA WARN: %s token-stale — run: bash %s" % (p["pool"], self)); continue
    if p.get("verdict") == "proceed": continue
    b = p.get("binding") or {}
    if p.get("status") == "ok" and b:
        # absent is never fabricated: a threshold only prints when the gauge has one.
        # the DURATION CLASS rides along, not just the window name — the
        # hands pre-flight refusal carries this line verbatim, and the driver reads
        # resets-soon (short window, park-and-retry) vs gated-for-window (weekly, reroute).
        # NO apostrophes here: this comment lives inside a single-quoted python -c string.
        if p.get("verdict") == "would-gate":
            sfx = ""
            if b.get("short") is True: sfx = ", resets-soon"
            elif b.get("short") is False: sfx = ", gated-for-window"
            print("QUOTA GATE: %s %s%% (thr %s, %s%s) — route away" % (p["pool"], b.get("used_pct"), b.get("threshold"), b.get("name") or "?", sfx))
        else:
            # would-warn: hot, still buyable (below its hard stop) — the
            # driver may still buy there and rebuys elsewhere if the seat dies
            print("QUOTA WARN: %s %s%% (thr %s, %s) — hot, buyable, may die mid-seat" % (p["pool"], b.get("used_pct"), b.get("threshold"), b.get("name") or "?"))
    else:
        print("QUOTA WARN: %s UNKNOWN — gauge blind, pool not known dead (last resort)" % p["pool"])
'
  return 0
}

# ---------- selftest: hermetic — PATH-farm CLI stubs, a stubbed gauge, zero model spend
run_selftest() {
  local SB PASS=0 FAIL=0 c E FX STALEWHY OK
  SB="$(mktemp -d "${TMPDIR:-/tmp}/rearm-selftest.XXXXXX")"
  mkdir -p "$SB/bin" "$SB/log" "$SB/tmp" "$SB/home"
  # HERMETIC: a suite run inside a Claude Code session inherits the driver's inbox socket — unset suite-wide, so
  # nothing this suite starts can capture it (heals once posted into the live session)
  unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
  # every stub heredoc below is QUOTED and fed through sed placeholders (@SB@ @C@ @HERE@): an
  # unquoted heredoc expands backticks while the stub is being written, and one such comment
  # ran the real claude --version three times before row 00 pinned PATH.
  for c in claude codex grok; do
    # one line per CALL, args joined by | — the line count is the call count AND the argv boundaries survive
    # ("$*" could not assert them: the prompt itself contains spaces). A heal is hands.sh --probe, so each
    # stub speaks its CLI's own seat wire (seat.py): claude stream-json lines on stdin (its answer carries a
    # rate_limit_event: the reading a real claude call leaves), codex an app-server (a turn per turn/start; its
    # thread/start params land in thread-codex), grok an ACP agent
    # (hands' own fake, test_acp.py --fake, replaying the recorded grok seat). --version answers first and
    # is never logged: it is the engine's version floor read, not a call.
    sed -e "s|@SB@|$SB|g" -e "s|@C@|$c|g" -e "s|@HERE@|$HERE|g" >"$SB/bin/$c" <<'STUB'
#!/usr/bin/env python3
import json, os, sys, time
c, a = '@C@', sys.argv[1:]
if a == ['--rearm-selftest-marker']:
    print('STUB-OK-' + c); sys.exit(0)
if a == ['--version']:
    print({'claude': '2.1.300 (Claude Code)', 'codex': 'codex-cli 0.160.0', 'grok': 'grok 1.0.46'}[c]); sys.exit(0)
open('@SB@/log/' + c, 'a').write(''.join('|' + w for w in a) + '\n')
open('@SB@/cwd-' + c, 'w').write(os.getcwd() + '\n')
if os.environ.get('REARM_STUB_ERR'):
    sys.stderr.write(os.environ['REARM_STUB_ERR'] + '\n')
if os.environ.get('REARM_STUB_RC'):
    sys.exit(int(os.environ['REARM_STUB_RC']))
out = lambda o: (sys.stdout.write(json.dumps(o) + '\n'), sys.stdout.flush())
n = int(time.time())
if c == 'grok':
    os.execvp('python3', ['python3', '@HERE@/test_acp.py', '--fake', '@HERE@/fixtures/grok-acp-seat-sample.jsonl', 'eof', '/dev/null'])
if c == 'codex':
    for line in sys.stdin:
        m = json.loads(line)
        meth, i, p = m.get('method'), m.get('id'), m.get('params') or {}
        if meth == 'thread/start':
            open('@SB@/thread-codex', 'w').write(json.dumps(p))
        if meth == 'initialize':
            out({'id': i, 'result': {}})
        elif meth in ('thread/start', 'thread/resume'):
            out({'id': i, 'result': {'thread': {'id': 'th-rearm'}}})
        elif meth == 'turn/start':
            out({'id': i, 'result': {'turn': {'id': 't1', 'status': 'inProgress'}}})
            for meth, p in (('turn/started', {'turn': {'id': 't1'}}),
                            ('item/completed', {'turnId': 't1', 'item': {'id': 'i1', 'type': 'agentMessage', 'text': 'OK'}}),
                            ('thread/tokenUsage/updated', {'turnId': 't1', 'tokenUsage': {'last': {'inputTokens': 1, 'outputTokens': 1}}}),
                            ('turn/completed', {'turn': {'id': 't1', 'status': 'completed'}})):
                out({'method': meth, 'params': dict(p, threadId='th-rearm')})
    sys.exit(0)
for line in sys.stdin:   # claude: one turn per message line
    out({'type': 'system', 'subtype': 'init', 'session_id': 's-rearm'})
    out({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
        'five_hour': {'utilization': 0.17, 'resetsAt': n + 3600}, 'seven_day': {'utilization': 0.29, 'resetsAt': n + 86400}}}})
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'OK', 'session_id': 's-rearm'})
STUB
    chmod +x "$SB/bin/$c"
  done
  # the probe counter lives OUTSIDE log/ — several rows assert log/ is empty for "no calls"
  sed -e "s|@SB@|$SB|g" >"$SB/bin/gauge" <<'STUB'
#!/bin/sh
[ -n "${REARM_HANG:-}" ] && sleep "$REARM_HANG"
n=$(cat "@SB@/probes" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" >"@SB@/probes"
echo "$*" >>"@SB@/gauge-args"
case " $* " in
  *--json*) cat "${REARM_FIX:-@SB@/fx-green.json}" ;;
  *)        echo "quota(observe): claude used=43.0% (seven_day, thr 90) verdict=proceed (fixture)" ;;
esac
exit 0
STUB
  chmod +x "$SB/bin/gauge"
  STALEWHY='"token-stale (cached JWT expired 2026-08-24T00:00:00+00:00). NOT an auth failure."'
  OK='{"pool":"claude","status":"ok","why":null,"verdict":"proceed","binding":{"name":"seven_day","used_pct":43.0,"threshold":75}}'
  printf '{"pools":[%s,{"pool":"grok","status":"unknown","why":%s,"verdict":"unknown","binding":null}]}' \
    "$OK" "$STALEWHY" >"$SB/fx-grok.json"
  printf '{"pools":[%s,{"pool":"grok","status":"ok","why":null,"verdict":"proceed","binding":{"name":"weekly","used_pct":12.0,"threshold":80}}]}' \
    "$OK" >"$SB/fx-green.json"
  printf '{"pools":[%s,{"pool":"grok","status":"ok","why":null,"verdict":"would-gate","binding":{"name":"weekly","used_pct":91.0,"threshold":80,"short":false}},{"pool":"codex","status":"unknown","why":"http-failed","verdict":"unknown","binding":null}]}' \
    "$OK" >"$SB/fx-warn.json"
  printf '{"pools":[%s,{"pool":"grok","status":"ok","why":null,"verdict":"would-warn","binding":{"name":"weekly","used_pct":93.0,"threshold":100,"short":false}},{"pool":"codex","status":"ok","why":null,"verdict":"proceed","binding":{"name":"primary","used_pct":40,"threshold":100,"short":false}}]}' \
    "$OK" >"$SB/fx-hot.json"
  printf '{"pools":[{"pool":"claude","status":"unknown","why":%s,"verdict":"unknown","binding":null},{"pool":"codex","status":"unknown","why":%s,"verdict":"unknown","binding":null},{"pool":"grok","status":"unknown","why":%s,"verdict":"unknown","binding":null}]}' \
    "$STALEWHY" "$STALEWHY" "$STALEWHY" >"$SB/fx-all.json"
  printf '{"pools":[{"pool":"faxmodem","status":"unknown","why":%s,"verdict":"unknown","binding":null}]}' \
    "$STALEWHY" >"$SB/fx-alien.json"

  # PATH is pinned to the farm plus the system dirs for every row, so no real provider CLI is
  # reachable (hermetic by construction); HOME, TMPDIR and XDG_STATE_HOME are the sandbox's, so a heal's
  # probe job dirs land in $SB/state/hands (the jobs root) — never among the real job streams the claude gauge reads.
  E="REARM_QUOTA_BIN='$SB/bin/gauge' PATH='$SB/bin:/usr/bin:/bin' HOME='$SB/home' TMPDIR='$SB/tmp' XDG_STATE_HOME='$SB/state'"
  t() { local n="$1" d="$2"; shift 2
        if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); echo "ok  $n $d"
        else FAIL=$((FAIL+1)); echo "FAIL $n $d"; fi; }

  # ---- ROW 00, GATING. Every row below runs under a PATH pinned to the farm plus the
  # system dirs. A suite that can spend real quota when its own seam breaks must REFUSE TO
  # RUN rather than run wrong (three rows once fired REAL CLIs), so this row
  # gates: the farm answers, and nothing but the farm resolves for any provider name.
  seam=1
  for c in claude codex grok; do
    [ "$(PATH="$SB/bin:/usr/bin:/bin" "$c" --rearm-selftest-marker 2>/dev/null)" = "STUB-OK-$c" ] || seam=0
    [ "$(PATH="$SB/bin:/usr/bin:/bin" command -v "$c" 2>/dev/null)" = "$SB/bin/$c" ] || seam=0
  done
  if [ "$seam" = 1 ]; then
    PASS=$((PASS+1))
    echo "ok  00 hermetic seam live: the farm shadows all three real CLIs and nothing else resolves on the suite PATH"
  else
    echo "FAIL 00 hermetic seam"
    echo "ABORT: hermetic seam inactive — refusing to run (would hit real CLIs)"
    rm -rf "$SB"
    return 1
  fi

  t 01 "token-stale grok → the grok CLI fires ONCE, as hands.sh --probe grok (its ACP agent argv, its job dir, status ok)" \
    sh -c "rm -rf '$SB/log/'* '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-grok.json' '$SELF' >'$SB/o1.txt' 2>&1 \
      && [ \$(wc -l <'$SB/log/grok') -eq 1 ] && grep -qx '|agent|--no-leader|--always-approve|stdio' '$SB/log/grok' \
      && jq -e '.status==\"ok\" and .provider==\"grok\"' '$SB'/state/hands/probe-*-grok/envelope.json \
      && grep -q '^rearm: grok token-stale → refreshed' '$SB/o1.txt'"
  t 02 "…and a pool that is NOT token-stale is never touched (claude reads ok in that fixture)" \
    sh -c "[ ! -e '$SB/log/claude' ] && [ ! -e '$SB/log/codex' ]"
  t 03 "no stale pool → ZERO CLI calls, board still printed, no action line, exit 0" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-green.json' '$SELF' >'$SB/o3.txt' 2>&1 \
      && [ -z \"\$(ls -A '$SB/log')\" ] && grep -q 'quota(observe): claude' '$SB/o3.txt' \
      && ! grep -q '^rearm:' '$SB/o3.txt'"
  t 04 "ALL THREE stale → all three fire ONCE each through hands' probe: claude haiku, codex a full-access thread, grok its ACP agent" \
    sh -c "rm -rf '$SB/log/'* '$SB/cwd-'* '$SB/thread-codex' '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-all.json' '$SELF' >'$SB/o4.txt' 2>&1 \
      && [ \$(grep -c 'token-stale → refreshed' '$SB/o4.txt') -eq 3 ] \
      && [ \$(cat '$SB/log/claude' '$SB/log/codex' '$SB/log/grok' | wc -l) -eq 3 ] \
      && grep -q '|--model|haiku|' '$SB/log/claude' \
      && jq -e '.sandbox==\"danger-full-access\"' '$SB/thread-codex' >/dev/null && grep -qx '|agent|--no-leader|--always-approve|stdio' '$SB/log/grok'"
  # 26: THE CLAUDE HEAL LEAVES THE READING. The gauge reads the newest claude job
  # stream, so a heal that pinged claude without leaving a stream there would heal nothing: this
  # row runs the REAL quota.sh over the sandbox jobs root after the heal. Falsified by pinging the
  # claude CLI directly (the older heal): no job stream, the gauge stays token-stale.
  t 26 "the claude heal leaves a claude job stream in the jobs root, and the REAL gauge reads it: claude ok 17/29" \
    sh -c "rm -rf '$SB/log/'* '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-all.json' '$SELF' >/dev/null 2>&1 \
      && ls '$SB'/state/hands/probe-*-claude/stream.jsonl >/dev/null \
      && env PATH='$SB/bin:/usr/bin:/bin' HOME='$SB/home' TMPDIR='$SB/tmp' XDG_STATE_HOME='$SB/state' '$HERE/quota.sh' --json --pools claude \
         | jq -e '.pools[0] | .status==\"ok\" and ([.windows[].used_pct]==[17,29])' >/dev/null"
  t 05 "refresh FAILURE (stub rc 7) → a FAILED line naming hands' typed status and job dir, the board still prints, exit is STILL 0" \
    sh -c "rm -rf '$SB/log/'* '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-grok.json' REARM_STUB_RC=7 '$SELF' >'$SB/o5.txt' 2>&1; rc=\$?; \
      [ \$rc -eq 0 ] && grep -q \"grok refresh FAILED (error after [0-9]*s; see \$(cd '$SB/state' && pwd -P)/hands/probe-\" '$SB/o5.txt' && grep -q 'quota(observe)' '$SB/o5.txt'"
  t 05b "…a FAILED ping still RE-READS the board (the CLI refreshed its token before the call failed) and the line names WHY it failed (the 402 that hid behind the stale remedy, typed provider-quota-dead by hands)" \
    sh -c "rm -rf '$SB/log/'* '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-grok.json' REARM_STUB_RC=7 REARM_STUB_ERR='API Error: 402 {\"error\":\"Grok Build usage balance exhausted\"}' '$SELF' >'$SB/o5b.txt' 2>&1 \
      && [ \"\$(cat '$SB/probes')\" = 2 ] && grep -q 'grok refresh FAILED (provider-quota-dead after' '$SB/o5b.txt'"
  t 07 "gauge unrunnable in BOARD mode → exit 2 (a refresh failure never is)" \
    sh -c "REARM_QUOTA_BIN='$SB/no-such-gauge' '$SELF' >/dev/null 2>&1; [ \$? -eq 2 ]"
  # --pre-buy runs at EVERY seat launch: silence when clean, one bounded probe, and NEVER
  # an inference call. Each of those is a way this could go wrong, so each is asserted.
  t 08 "--pre-buy on a clean board → EMPTY output, exit 0, and exactly ONE probe" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-green.json' '$SELF' --pre-buy >'$SB/o8.txt' 2>&1 \
      && [ ! -s '$SB/o8.txt' ] && [ -z \"\$(ls -A '$SB/log')\" ] && [ \"\$(cat '$SB/probes')\" = 1 ]"
  t 09 "--pre-buy on a GATED board → one line per off-proceed pool: QUOTA GATE for would-gate, QUOTA WARN for unknown" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-warn.json' '$SELF' --pre-buy >'$SB/o9.txt' 2>&1 \
      && [ \$(grep -c '^QUOTA ' '$SB/o9.txt') -eq 2 ] \
      && grep -q '^QUOTA GATE: grok 91.0% (thr 80, weekly, gated-for-window) — route away$' '$SB/o9.txt' \
      && grep -q '^QUOTA WARN: codex UNKNOWN — gauge blind, pool not known dead (last resort)$' '$SB/o9.txt' \
      && ! grep -q claude '$SB/o9.txt'"
  t 24 "--pre-buy on a HOT board (would-warn) → QUOTA WARN advisory only: no GATE line, no route-away, no duration suffix" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-hot.json' '$SELF' --pre-buy >'$SB/o24.txt' 2>&1 \
      && [ \$(grep -c '^QUOTA ' '$SB/o24.txt') -eq 1 ] \
      && grep -q '^QUOTA WARN: grok 93.0% (thr 100, weekly) — hot, buyable, may die mid-seat$' '$SB/o24.txt' \
      && ! grep -q 'GATE\|route away' '$SB/o24.txt'"
  t 10 "--pre-buy NEVER heals: a stale pool gets a warn CARRYING THE REMEDY, zero CLI calls, one probe" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-grok.json' '$SELF' --pre-buy >'$SB/o10.txt' 2>&1 \
      && [ -z \"\$(ls -A '$SB/log')\" ] && [ \"\$(cat '$SB/probes')\" = 1 ] \
      && grep -q \"^QUOTA WARN: grok token-stale — run: bash $SELF\$\" '$SB/o10.txt'"
  t 11 "--pre-buy on a HUNG gauge → the routing-blind line, exit 0, no heal (bounded probe)" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-grok.json' REARM_HANG=2 REARM_PROBE_T=1 '$SELF' --pre-buy >'$SB/o11.txt' 2>&1; rc=\$?; \
      [ \$rc -eq 0 ] && grep -q '^QUOTA WARN: board unreadable (gauge timeout) — routing blind$' '$SB/o11.txt' \
      && [ -z \"\$(ls -A '$SB/log')\" ]"
  # 25: the pre-flight senses ONLY the pool it buys: --pools threads to the gauge in both
  # of rearm's gauge calls. Falsification: drop the threading in sense() → the gauge sees bare --json.
  t 25 "--pre-buy --pools grok asks the gauge for grok ONLY (--json --pools grok), one probe; board mode threads it to both reads" \
    sh -c "rm -f '$SB/log/'* '$SB/probes' '$SB/gauge-args'; env $E REARM_FIX='$SB/fx-green.json' '$SELF' --pre-buy --pools grok >/dev/null 2>&1 \
      && [ \"\$(cat '$SB/gauge-args')\" = '--json --pools grok' ] \
      && rm -f '$SB/gauge-args' && env $E REARM_FIX='$SB/fx-green.json' '$SELF' --pools grok >/dev/null 2>&1 \
      && [ \"\$(tr '\\n' '|' <'$SB/gauge-args')\" = '--json --pools grok|--pools grok|' ]"
  t 17 "board mode with nothing to heal → TWO zero-spend probes (sense, then the printed board) and zero CLI calls" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-green.json' '$SELF' >/dev/null 2>&1 \
      && [ \"\$(cat '$SB/probes')\" = 2 ] && [ -z \"\$(ls -A '$SB/log')\" ]"
  t 18 "a stale pool hands has no CLI for makes NO call, never guessed at (fails closed: not-installed)" \
    sh -c "rm -f '$SB/log/'* '$SB/probes'; env $E REARM_FIX='$SB/fx-alien.json' '$SELF' >'$SB/o18.txt' 2>&1 \
      && [ -z \"\$(ls -A '$SB/log')\" ] && grep -q '^rearm: faxmodem refresh FAILED (not-installed after' '$SB/o18.txt'"
  # hands' probe runs in TMPDIR (its CWD, canonical): every seat process is launched there, and codex also
  # gets it as its thread's cwd (thread/start) — the caller's repo is never a ping's workspace.
  t 23 "every fire happens in a NEUTRAL cwd (TMPDIR), never the caller's repo" \
    sh -c "cd '$HERE' && rm -rf '$SB/log/'* '$SB/cwd-'* '$SB/thread-codex' '$SB/probes' '$SB/state/hands'; env $E REARM_FIX='$SB/fx-all.json' '$SELF' >/dev/null 2>&1 \
      && T=\"\$(cd '$SB/tmp' && pwd -P)\" && for c in claude codex grok; do [ \"\$(cat '$SB/cwd-'\$c)\" = \"\$T\" ] || exit 1; done \
      && jq -e --arg t \"\$T\" '.cwd==\$t' '$SB/thread-codex' >/dev/null && ! grep -qF '$HERE' '$SB/log/claude' '$SB/log/codex' '$SB/log/grok'"
  # 27: falsification: drop the suite-wide unset and run the suite with a fake socket exported -> red.
  t 27 "HERMETIC SUITE: the suite's env carries neither driver-inbox name, whatever env the suite was started in" \
    sh -c "[ -z \"\${CLAUDE_CODE_MESSAGING_SOCKET+x}\${CLAUDE_CODE_MESSAGING_TOKEN+x}\" ]"

  rm -rf "$SB"
  echo "selftest: $PASS passed, $FAIL failed"
  [ "$FAIL" -eq 0 ]
}

if [ "$SELFTEST" = 1 ]; then run_selftest; exit $?; fi
case "$MODE" in prebuy) run_prebuy ;; *) run_board ;; esac
exit $?
