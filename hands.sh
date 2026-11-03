#!/usr/bin/env bash
# hands — run a task on another provider's coding-agent CLI (claude, codex, grok, opencode) as a subagent seat.
# Each CLI keeps its own login; hands stores no credentials (a job dir keeps the host's push socket path and
# token while you use it: README § Security model).
#
#   hands.sh start [opts] provider[:model] prompt-file   buy a seat; returns at once: {"status":"spawned","dir":DIR,...}
#   hands.sh [opts] provider[:model] prompt-file         sync: start + wait + stop
#   hands.sh send DIR FILE        the next turn (a seat whose process is gone relaunches on its session)
#   hands.sh mail DIR TEXT        a driver line the seat's live wrapper delivers
#   hands.sh wait DIR [--for turn|mail|exit] [--timeout S]   blocks until the turn ends: the answer, then the envelope
#   hands.sh events DIR [--follow]   a readable view of DIR/events.jsonl
#   hands.sh stop DIR             close the seat as its deadline does (stdin EOF, TERM, KILL); the envelope still lands
#   hands.sh --probe [claude|codex|grok|opencode|all]   a 1-token aliveness ping per provider (spends a little quota)
#   hands.sh quota [--json] [provider ...]   the quota board (quota.sh): read-only, it never spends
#   hands.sh --selftest           hermetic tests: stub CLIs, no network, no spend
#
# opts (all before the provider spec; the prompt is always a file):
#   --cwd DIR           where the seat works (default: the current dir). A seat has full read and write access,
#                       as you: point it at a worktree or a scratch dir
#   --out DIR           the job dir, new or empty, made 0700 (default: a fresh dir in $XDG_STATE_HOME/hands, else
#                       ~/.local/state/hands, where every buy sweeps dirs idle for 30 days)
#   --timeout SECS      a wall clock per launch (default: none; the seat runs until its work ends or a stop)
#   --effort <tier>     claude, codex: low|medium|high|xhigh|max, passed through (codex adds ultra where it lists it);
#                       grok, opencode: required, a value the model lists (grok-4.6: low|medium|high|xhigh)
#
# envelope: {"status":"ok|error|incomplete|cancelled|truncated|timeout|auth-expired|provider-quota-dead|not-installed|refused",
#   "provider","model","effort","rc","seconds","reset","dir","usage","session","turn","delivery",...}
#   exit 0 ok, 1 any other status, 2 refused or a usage error. README.md is the manual;
#   CONTRIBUTING.md has the engine's rules.
set -u

# NO SEAT INHERITS THE DRIVER'S MESSAGING SOCKET/TOKEN: a process holding both can post into
# the driver's session as its own child. NOT unset here -- the push needs them: seat.py's wrapper reads both
# (capture(): DIR/driver) and drops them from its env before any seat process starts (spawn). A CLI started
# outside a wrapper drops them where it starts: run_probe (a probe never pushes).

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
CWD="$PWD" TIMEOUT="" OUT="" EFFORT="" PROBE="" SELFTEST=0 VERB=""
SEATPY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/seat.py"   # the wire layer
case "${1:-}" in   # the seat verbs come first; start is the buy that returns at once
  start) VERB=start; shift ;;
  send|mail|wait|events|stop) VERB="$1"; shift; VERB_ARGS=("$@"); set -- ;;
  quota) shift; exec bash "${SEATPY%/*}/quota.sh" "$@" ;;
esac

cli_path() {  # resolve a provider CLI even when it's not on PATH
  case "$1" in
    opencode) if command -v opencode >/dev/null 2>&1; then command -v opencode
              elif [ -x "$HOME/.opencode/bin/opencode" ]; then echo "$HOME/.opencode/bin/opencode"; fi ;;
    *) command -v "$1" 2>/dev/null ;;
  esac
}

need() { [ $# -ge 2 ] || { echo "hands: $1 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) need "$@"; CWD="$2"; shift ;;
    --timeout) need "$@"; TIMEOUT="$2"; shift ;;
    --out) need "$@"; OUT="$2"; shift ;;
    --effort) need "$@"; EFFORT="$2"; shift ;;
    --probe) if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then PROBE="$2"; shift; else PROBE=all; fi ;;
    --selftest) SELFTEST=1 ;;
    -h|--help) sed -n '/^# hands — /,/^set -u/{/^set -u/!p;}' "$0"; exit 0 ;;
    --) shift; break ;;
    -*) echo "hands: unknown flag $1" >&2; exit 2 ;;
    *) break ;;
  esac
  shift
done

# TIMEOUT KILL, group form (an orphan incident): the old one-liner
# (perl alarm + exec) signalled only the process it exec-ed — but `codex` on PATH is an
# npm shim that SPAWNS the vendor binary as a child, so a timed-out seat kept building
# for 2h+ AFTER its "timeout" envelope was written (the vendor binary was
# orphaned to ppid 1, tree edits 17 minutes post-alarm, driver misled into a rebuy).
# Now the seat runs as the leader of its OWN process group; on alarm the whole group
# gets TERM first (a graceful window, so a CLI can flush its session/rollout state for
# a later resume), a <=4s drain poll, then KILL — the envelope is written only after
# the group is dead, so "status":"timeout" is a true death certificate again.
# rc mapping preserved: timeout=142, child killed by signal N=128+N, else the child rc.
wt_alarm() { # $1 secs (0 = no alarm), rest argv — run argv in its own pgroup under a wall-clock alarm
  perl -e '
    use Errno; use POSIX ();
    my $t = shift @ARGV;
    my $pid = fork; defined $pid or exit 127;
    if ($pid == 0) { setpgrp(0, 0); exec @ARGV; exit 127; }
    # ONE reaper for every way out — alarm, forwarded signal, natural exit: TERM the seat
    # pgroup, give it $_[0] tenths to drain, KILL the rest. The natural-exit call is what
    # keeps a seat from leaving processes behind: a child it backgrounded and forgot dies
    # here, not at the next reboot (a `log stream` and a probe script once outlived
    # their seat by hours). What escapes the pgroup — launchd-owned simulators, anything the
    # seat setsid-ed (nohup does NOT leave the group) — is the seat contract, not this.
    sub reap { kill(15, -$pid); for (1..$_[0]) { last unless kill(0, -$pid); select(undef, undef, undef, 0.1); } kill(9, -$pid); }
    my $to = 0;
    $SIG{ALRM} = sub { $to++; kill(($to > 1 ? 9 : 15), -$pid); alarm 5; };
    # forward INT/TERM/HUP to the group: the old exec-form CLI sat in the callers
    # foreground pgroup, so ^C reached it and its shim-spawned children; the private
    # pgroup would otherwise orphan them on ^C — the same defect on a different signal
    # ...but NEVER catch what the caller already ignored: `nohup` (a background buy)
    # sets HUP/INT to SIG_IGN precisely so a seat outlives a terminal
    # teardown, and the old exec form honoured that by inheritance. A handler installed
    # over SIG_IGN would kill the very seats those guards exist to protect.
    my %sn = (INT => 2, TERM => 15, HUP => 1);
    for my $s (keys %sn) { my $no = $sn{$s};
      my $oa = POSIX::SigAction->new; POSIX::sigaction($no, undef, $oa);
      next if defined $oa->handler && $oa->handler eq 'IGNORE';
      $SIG{$s} = sub { reap(20); exit(128 + $no); }; }
    alarm $t;
    my $r;
    do { $r = waitpid($pid, 0) } while ($r == -1 && $!{EINTR});
    alarm 0;
    my $st = $?;
    if ($to) { reap(40); exit 142; }
    reap(20);
    exit(($st & 127) ? 128 + ($st & 127) : $st >> 8);
  ' "$@"
}

# classify a failure blob -> "status<TAB>reset". Pure function so --selftest can feed the
# fixture corpus (fixtures/) through the exact production regexes — ONE copy, seat.py's
# classify(), which the persistent wrapper types its turns with. Statuses only ever
# assigned on failure paths.
classify_blob() { printf '%s' "$1" | python3 "$SEATPY" classify; }

# THE GO KEY'S FILE — the ONE copy of its path, for quota.sh's Go gauge (it sources this function): opencode's own
# data dir, resolved as the CLI resolves it, XDG_DATA_HOME first. No seat gets a key from hands: an opencode
# seat runs on the user's normal opencode store and login, and seat.py's wrapper drops
# OPENCODE_API_KEY from every seat.
opencode_auth() { printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json"; }

# ---------- THE SEAT BOX — the seat contract, ONE copy ----------
# run_single prepends it to every buy's task, a probe's ping too. It names no job dir, so
# every seat's prompt shares one prefix through the task end (the provider's prompt cache);
# seat_dir() is the one per-seat line, after the task. A heredoc in a plain function: bash 3.2
# cannot parse an apostrophe inside a heredoc inside $(...).
seat_box() { cat <<'EOF'
SEAT BOX. Run every command to completion under an explicit timeout, never as a
background task — a seat that ends its turn waiting on one parks until its wall clock
and returns nothing. Verify through the repo's own entry points, not ad-hoc probes; a
fix ships a test that fails with the fix reverted — show both runs. Shut down whatever
you booted. Put your report IN the final message, never before a later verification
step. If a file you must edit is not yours, stop and report BLOCKED with the handoff as
your first line — never "done".
MAIL. To message the driver, append one JSON line {"text":"..."} to the outbox.jsonl in
your job dir (the last line of this prompt names it). Mail from the driver arrives as a
message that starts "MAIL from driver".
EOF
}
seat_dir() { printf 'YOUR JOB DIR: %s (your outbox: %s/outbox.jsonl).\n' "$1" "$1"; }

# ---------- single call ----------
run_single() { # $1 provider[:model]  $2 prompt-file
  local spec="$1" pf="$2" prov model dir jobs
  prov="${spec%%:*}"; model=""; case "$spec" in *:*) model="${spec#*:}";; esac
  # the provider names the default job dir, so only a plain lowercase name ever reaches a path
  case "$prov" in ''|*[!a-z0-9]*) echo "hands: bad provider '$prov' (claude|codex|grok|opencode)" >&2; return 2 ;; esac
  jobs="$(python3 "$SEATPY" sweep 2>/dev/null)"   # THE RETENTION first; it prints the jobs root (seat.py JOBS: its one copy)
  if [ -n "$OUT" ]; then dir="$OUT"   # a NEW or EMPTY dir: a buy never writes over files it did not make
    if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
      echo "hands: --out $dir is not empty — name a new or empty dir, or omit --out for a fresh one in the jobs root" >&2; return 2
    fi
    mkdir -p "$(dirname "$dir")" "$dir" 2>/dev/null; chmod 700 "$dir" 2>/dev/null   # its parents keep your umask; the dir is 0700, an existing one too
  elif [ -n "$jobs" ]; then dir="$jobs/$(date +%Y%m%d-%H%M%S)-$prov-$$"; ( umask 077; mkdir -p "$dir" )   # 0700 (XDG): prompts live here
  else echo "hands: $SEATPY sweep printed no jobs root" >&2; return 2; fi
  # ONE canonical job dir from here on: the seat's job-dir line, the store, the handle. A dir that could not be made
  # refuses the buy: an empty name would make the caller's cwd the job dir
  dir="$(cd "$dir" 2>/dev/null && pwd -P)"
  [ -n "$dir" ] || { echo "hands: cannot create the job dir ${OUT:-in $jobs}" >&2; return 2; }
  if [ ! -f "$pf" ]; then
    echo "hands: prompt file not found: $pf" >&2
    case "$pf" in -*) echo "hands: '$pf' is a FLAG in the prompt-file position — options must PRECEDE the provider positional (hands.sh [opts] provider[:model] prompt-file); flags after it are read as the prompt file" >&2;; esac
    emit "$dir" error "$prov" "$model" 2; return 2
  fi
  # THE SEAT BOX rides every buy: prompt.txt = seat_box + a blank line + the task + a blank
  # line + seat_dir (the task ended by a newline first), and it is the ONE file every provider
  # branch below reads — the exact seat input. seat_dir names DIR/outbox.jsonl and mail lands in
  # DIR/inbox.jsonl, so both exist before the seat starts.
  { seat_box; echo; cat "$pf"; [ -z "$(tail -c 1 "$pf")" ] || echo; echo; seat_dir "$dir"; } >"$dir/prompt.txt"
  touch "$dir/inbox.jsonl" "$dir/outbox.jsonl"
  pf="$dir/prompt.txt"

  # a name with no wire (a typo, an unsupported provider such as gemini) is refused here
  case "$prov" in
    claude|codex|grok|opencode) ;;   # seat.py owns the process, the stream and every envelope
    *) echo "hands: unknown provider '$prov' (claude|codex|grok|opencode)" >&2
       emit "$dir" error "$prov" "$model" 2; return 2 ;;
  esac
  seat_launch "$prov" "$model" "$dir"
}

emit() { # dir status prov model rc [reset [reason]] -> DIR/envelope.json, printed. ONE envelope writer: seat.py's,
  # which the wrapper writes every turn with. effort = the buy's --effort (empty: null, the CLI default as the buy ran)
  EFFORT="${EFFORT:-}" python3 "$SEATPY" emit "$1" "$2" "$3" "$4" "$5" "${6:-}" "${7:-}"; cat "$1/envelope.json"
}
refuse() { emit "$1" refused "$2" "$3" 2 "" "$4"; return 2; }   # dir prov model reason: typed before any spend

# ---------- seats: the handle, then seat.py ----------
# The CLI runs in ONE canonical physical cwd (a relative --cwd would resolve twice). DIR/session is the
# handle every later send resumes with: provider, model, effort, cwd, timeout, the
# CLI path, and the session id seat.py records at first sight.
seat_launch() { # $1 prov  $2 model  $3 dir
  local prov="$1" model="$2" dir="$3" bin acwd rc ack
  bin="$(cli_path "$prov")"
  [ -n "$bin" ] || { emit "$dir" not-installed "$prov" "$model" 0; return 1; }
  acwd="$(cd "$CWD" 2>/dev/null && pwd -P)" || { echo "hands: --cwd $CWD unresolvable" >&2; acwd=""; }
  if [ -z "$acwd" ]; then
    emit "$dir" error "$prov" "$model" 2; return 2
  fi
  jq -n -c --arg p "$prov" --arg m "$model" --arg e "$EFFORT" --arg c "$acwd" --arg t "$TIMEOUT" \
     --arg b "$bin" '{provider:$p, model:(if $m=="" then null else $m end),
     effort:(if $e=="" then null else $e end), cwd:$c, timeout:(if $t=="" then null else ($t|tonumber) end),
     cli:$b, session:null}' >"$dir/session" 2>/dev/null \
    || { echo "hands: --timeout takes seconds" >&2; emit "$dir" error "$prov" "$model" 2; return 2; }
  if [ "$VERB" = start ]; then python3 "$SEATPY" spawn "$dir" "$dir/prompt.txt"; return $?; fi
  SEAT_DIR="$dir"; trap 'seat_abort 130' INT; trap 'seat_abort 143' TERM; trap 'seat_abort 129' HUP
  ack="$(python3 "$SEATPY" spawn "$dir" "$dir/prompt.txt")"; rc=$?
  if [ "$rc" -eq 2 ]; then printf '%s\n' "$ack"   # refused before any prompt: that envelope is the answer
  else python3 "$SEATPY" wait "$dir"; rc=$?; python3 "$SEATPY" stop "$dir" >/dev/null; fi
  trap - INT TERM HUP
  return "$rc"
}
seat_abort() { python3 "$SEATPY" stop "$SEAT_DIR" >/dev/null 2>&1; exit "$1"; }   # a signalled sync buy stops its seat

seat_send() { # $1 dir  $2 file — a live seat takes the turn now; a gone one resumes its recorded session
  local dir="" prov model rc
  [ -f "$1/session" ] && dir="$(cd "$1" && pwd -P)"
  [ -n "$dir" ] || { echo "hands: $1 is not a seat dir (it holds no session handle)" >&2; return 2; }
  [ -f "$2" ] || { echo "hands: send file not found: $2" >&2; return 2; }
  python3 "$SEATPY" send "$dir" "$2"; rc=$?
  [ "$rc" -eq 3 ] || return "$rc"
  # the process is gone: relaunch it on the SAME model, effort and cwd
  prov="$(jq -r .provider "$dir/session")"; model="$(jq -r '.model // empty' "$dir/session")"
  EFFORT="$(jq -r '.effort // empty' "$dir/session")"
  [ -n "$(jq -r '.session // empty' "$dir/session")" ] || {
    echo "hands: $dir recorded no session (its seat never reached the model) — start a fresh seat" >&2
    refuse "$dir" "$prov" "$model" no-session; return 2; }
  python3 "$SEATPY" spawn "$dir" "$2"
}

# ---------- probe ----------
run_probe() {
  # a probe is INTERNAL (an aliveness ping): it never captures the driver's inbox socket, so it never pushes
  unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
  local list="$1" p pf rc=0 lines jobs
  jobs="$(python3 "$SEATPY" sweep 2>/dev/null)"   # the jobs root (seat.py JOBS), where every probe's dir goes
  [ -n "$jobs" ] || { echo "hands: $SEATPY sweep printed no jobs root" >&2; return 1; }
  [ "$list" = all ] && list="claude codex grok opencode"
  lines="$(mktemp "${TMPDIR:-/tmp}/hands-probe-lines.XXXXXX")" && pf="$(mktemp "${TMPDIR:-/tmp}/hands-probe.XXXXXX")" || return 1
  printf 'Reply with exactly: OK' >"$pf"
  for p in $list; do
    if [ -z "$(cli_path "$p")" ]; then
      printf '{"status":"not-installed","provider":"%s","model":null,"rc":0,"seconds":0,"reset":null,"dir":null,"usage":null}\n' "$p" | tee -a "$lines"; continue
    fi
    local pspec="$p" peff=""
    # the smallest model wins: a ping proves the CLI and its auth, not a model. claude's probe also
    # refreshes the quota board — its stream's rate_limit_event IS the claude reading, and the windows
    # are the account's, whatever model wrote them. opencode: 2.x lists no working free model (on
    # 2.0.22 one hung 4 min, one "unavailable"), so the cheapest Go model at low. An ACP seat always
    # names an effort its model lists.
    case "$p" in
      claude)   pspec="claude:haiku" ;;
      opencode) pspec="opencode:opencode-go/deepseek-v4.1-flash"; peff=low ;;
      grok)     peff=low ;;
    esac
    # explicit --out per probe: the envelope is found by path, never by tmp-glob
    local pdir="$jobs/probe-$$-$p"
    rm -rf "$pdir"
    # a ping runs in a NEUTRAL cwd: it has no business reading, or being blamed for, the caller's repo
    TIMEOUT=240 OUT="$pdir" EFFORT="$peff" CWD="${TMPDIR:-/tmp}" run_single "$pspec" "$pf" >/dev/null 2>&1 \
      && { printf '{"status":"ok","provider":"%s"}\n' "$p" | tee -a "$lines"; } \
      || { { cat "$pdir/envelope.json" 2>/dev/null \
             || printf '{"status":"error","provider":"%s","model":null,"rc":1,"seconds":0,"reset":null,"dir":null,"usage":null}' "$p"; \
           } | tee -a "$lines"; rc=1; }
  done
  rm -f "$pf" "$lines"; return $rc
}

# ---------- selftest (hermetic: zero model calls, zero network, own cache+tmp) ----------
run_selftest() {
  local HERE PASS=0 FAIL=0 SB SBP
  HERE="$(cd "$(dirname "$0")" && pwd)"
  SB="$(mktemp -d "${TMPDIR:-/tmp}/hands-selftest.XXXXXX")"
  SBP="$(cd "$SB" && pwd -P)"   # as a wrapper's argv names its dir: a row looks up its own seat, never another suite's
  unset CODEX_ARGV_LOG  # externally-exported sentinels must not reach stub calls
  unset ACP_REPLAY ACP_MODE ACP_REC ACP_ARGV_LOG ACP_ENV_LOG ACP_PIDFILE OC_SESSION_LOG
  unset CL_ARGV_LOG CL_STUB_MODE CL_STDIN_LOG
  # PUSH SEAM: a suite run inside a Claude Code session inherits the host's inbox socket — unset,
  # so no hermetic seat ever posts to it (the push rows arm a throwaway socket of their own)
  unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
  # THE CALLER'S STORE SEAM: opencode resolves its store from XDG_DATA_HOME, so EVERY row -- including those that call
  # "$SELF" directly -- runs on a sandbox store, never the real one. It holds a FAKE Go key, as a real store does: a seat
  # that seeded or swapped a store from it would show (row 39s proves a seat runs on it unchanged).
  mkdir -p "$SB/oc-store/opencode"; printf '{"opencode-go":{"key":"sk-test"}}' >"$SB/oc-store/opencode/auth.json"
  export XDG_DATA_HOME="$SB/oc-store"
  # THE JOBS ROOT SEAM: a buy without --out (and every probe) makes its dir in $XDG_STATE_HOME/hands, so EVERY row runs
  # on a sandbox root, never the real one, and no buy's sweep can reach a real dir (rows 39w, 156, 188, 189, 191, 192, 193 name their own)
  export XDG_STATE_HOME="$SB/state"
  # the probe rows that run with PATH=/usr/bin:/bin skip where a provider CLI lives there (they would launch it) or jq
  # and python3 do not
  SKIP=0 SYS_ROWS=""
  for c in claude codex grok opencode; do [ -n "$(PATH=/usr/bin:/bin command -v "$c")" ] && SYS_ROWS=" 04b 20 20b "; done
  for c in jq python3; do [ -n "$(PATH=/usr/bin:/bin command -v "$c")" ] || SYS_ROWS=" 04b 20 20b "; done
  t() { # t <num> <desc> <cmd...> — passes when cmd exits 0
    local n="$1" d="$2"; shift 2
    case "$SYS_ROWS" in *" $n "*) SKIP=$((SKIP+1)); echo "skip $n $d"; return 0 ;; esac
    if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); echo "ok  $n $d"
    else FAIL=$((FAIL+1)); echo "FAIL $n $d"; fi
  }
  rc_is() { local want="$1"; shift; "$@" >/dev/null 2>&1; [ $? -eq "$want" ]; }
  # the arg-surface rows run before the stubs exist: a shim per provider sits first on their PATH, so a regression there
  # fails loudly (rc 97) instead of reaching a real CLI
  mkdir -p "$SB/tmp" "$SB/cache" "$SB/deny"
  for c in claude codex grok opencode; do printf '#!/bin/sh\necho "selftest reached a real %s" >&2; exit 97\n' "$c" >"$SB/deny/$c"; chmod +x "$SB/deny/$c"; done
  env_run() { PATH="$SB/deny:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$0" "$@"; }
  echo "task" > "$SB/q.md"
  seat_box >"$SB/box.txt"; local BOXN; BOXN="$(wc -l <"$SB/box.txt" | tr -d ' ')"

  # arg surface
  t 00 "help prints full usage (marker-based, immune to header growth)" sh -c "'$0' --help | grep -q -- '--effort <tier>' && '$0' --help | grep -q '^# envelope:'"
  t 01 "unknown flag exits 2"            rc_is 2 env_run --bogus x "$SB/q.md"
  t 02 "no args exits 2"                 rc_is 2 env_run
  t 03 "unknown provider exits 2"        rc_is 2 env_run --out "$SB/o3" badprov "$SB/q.md"
  t 04 "unknown provider leaves typed envelope" sh -c "jq -e '.status==\"error\" and .provider==\"badprov\"' '$SB/o3/envelope.json'"
  # 04b: an unsupported provider name such as gemini dies through the SAME unknown-provider path as any other
  # name — no special case. PATH=/usr/bin:/bin keeps the row hermetic under falsification.
  PATH=/usr/bin:/bin TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$0" --out "$SB/o4b" gemini:gemini-3.8-flash-high "$SB/q.md" >/dev/null 2>"$SB/o4b.err"; G4B=$?
  t 04b "an unsupported gemini:<model> spec is refused by the unknown-provider path: rc 2, typed error envelope naming it, the provider list it prints omits gemini" \
    sh -c "[ $G4B -eq 2 ] && jq -e '.status==\"error\" and .provider==\"gemini\" and .model==\"gemini-3.8-flash-high\" and .rc==2' '$SB/o4b/envelope.json' >/dev/null && grep -qF \"unknown provider 'gemini' (claude|codex|grok|opencode)\" '$SB/o4b.err'"
  t 05 "missing prompt file exits 2"     rc_is 2 env_run --out "$SB/o5" claude "$SB/nope.md"
  # 05b: the provider names the default job dir, so a name that is not plain lowercase is refused before any dir is
  # made. Falsification: drop the check -> mkdir makes $SB/state/esc-<pid> outside the jobs root
  env_run 'x/../../esc:m' "$SB/q.md" >/dev/null 2>"$SB/o5b.err"; R05B=$?
  t 05b "a provider name that is not plain lowercase is refused (rc 2) before any dir is made: none escapes the jobs root" \
    sh -c "[ $R05B -eq 2 ] && grep -q \"bad provider 'x/../../esc'\" '$SB/o5b.err' && ! ls -d '$SB'/state/esc-* >/dev/null 2>&1"
  # 05d: a job dir that cannot be made refuses the buy. Falsification: drop the empty-dir guard -> cd \"\" is the caller's
  # cwd, and the session handle (and prompt) land there
  mkdir -p "$SB/cwd05d"; ( cd "$SB/cwd05d" && PATH="$SB/deny:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" \
    "$SELF" --out /dev/null/x claude "$SB/q.md" >/dev/null 2>"$SB/o5d.err" ); R05D=$?
  t 05d "an --out that cannot be made refuses the buy (rc 2, named) and writes nothing into the caller's cwd" \
    sh -c "[ $R05D -eq 2 ] && grep -q 'cannot create the job dir /dev/null/x' '$SB/o5d.err' && [ -z \"\$(ls -A '$SB/cwd05d')\" ]"
  # 05e: a value flag with no value is a usage error. Falsification: drop need() -> bash dies on an unbound \$2
  env_run --cwd >/dev/null 2>"$SB/o5e.err"; R05E=$?
  t 05e "a value flag given last (--cwd) is a usage error naming the flag (rc 2), never an unbound-variable crash" \
    sh -c "[ $R05E -eq 2 ] && grep -q 'hands: --cwd needs a value' '$SB/o5e.err' && ! grep -q unbound '$SB/o5e.err'"
  t 05c "flag-in-prompt-file position names order fix" sh -c "env TMPDIR='$SB/tmp' XDG_CACHE_HOME='$SB/cache' '$0' --out '$SB/o5c' claude --effort 2>&1 | grep -q 'must PRECEDE the provider positional'"
  t 06 "envelope schema complete"        sh -c "jq -e 'has(\"status\") and has(\"provider\") and has(\"model\") and has(\"effort\") and has(\"rc\") and has(\"seconds\") and has(\"reset\") and has(\"dir\") and has(\"usage\")' '$SB/o5/envelope.json'"
  t 06b "envelope records the buy effort (@effort BINDS: every relaunch runs at it); absent → null" \
    sh -c "env TMPDIR='$SB/tmp' XDG_CACHE_HOME='$SB/cache' '$0' --effort xhigh --out '$SB/o6b' claude '$SB/nope.md' >/dev/null 2>&1; jq -e '.effort==\"xhigh\"' '$SB/o6b/envelope.json' >/dev/null && jq -e '.effort==null' '$SB/o5/envelope.json' >/dev/null"
  t 07 "out-dir reuse guard exits 2"     rc_is 2 env_run --out "$SB/o5" claude "$SB/q.md"
  # 07b: --out is a NEW or EMPTY dir: a buy never writes over files it did not make. Falsification: drop the
  # non-empty check -> prompt.txt (and the handle) land beside the user's file
  mkdir -p "$SB/o7b"; printf keep >"$SB/o7b/notes.txt"; env_run --out "$SB/o7b" claude "$SB/q.md" >/dev/null 2>"$SB/o7b.err"; R07B=$?
  t 07b "a non-empty --out is refused (rc 2, named): the user's file stays and nothing is written beside it" \
    sh -c "[ $R07B -eq 2 ] && grep -q 'is not empty' '$SB/o7b.err' && [ \"\$(ls -A '$SB/o7b')\" = notes.txt ] && [ \"\$(cat '$SB/o7b/notes.txt')\" = keep ]"

  # grok + opencode are ACP seats (seat.py + acp.py). ONE fake agent answers both: test_acp.py --fake
  # replays a recorded transcript request by request (a method out of order exits 3). Seams: ACP_REPLAY
  # (default: that CLI's recorded 4-turn seat, fixtures/*-acp-seat-sample.jsonl, minus the client's
  # cancel, so its turn 3 ends cancelled by itself), ACP_MODE, ACP_REC (the client lines it received),
  # ACP_ARGV_LOG, ACP_ENV_LOG, ACP_PIDFILE (one line per launch),
  # OC_SESSION_LOG (the session branch). chk.py holds the multi-line checks.
  mkdir -p "$SB/bin"
  cat >"$SB/chk.py" <<'PY'
import json, os, re, subprocess, sys, time
J = os.path.join
def evs(d):
    return [json.loads(l) for l in open(J(d, 'events.jsonl'))]
def rpc(f):   # the client requests an ACP fake received, in order: [(method, params)]
    return [(m['method'], m.get('params') or {}) for m in map(json.loads, open(f)) if 'method' in m]
def derive(src, dst, how):   # a transcript for the ACP fake: nocancel | hang (turn 1's updates, never its answer) | err:<file> (it errors)
    rows = [json.loads(l) for l in open(src)]   # | model:<id> (the agent's session starts on <id>; the client sets the recorded one)
    m = lambda r: r['msg'] if isinstance(r['msg'], dict) else {}   # | gone (session/resume answers an error: no such session)
    if how == 'gone':
        j = next(j for j, r in enumerate(rows) if r['dir'] == 'out' and m(r).get('method') == 'session/resume')
        rows = rows[:j + 1] + [{'dir': 'in', 'msg': {'jsonrpc': '2.0', 'id': rows[j]['msg']['id'], 'error': {'code': -32602, 'message': 'Session not found'}}}]
        open(dst, 'w').writelines(json.dumps(r) + '\n' for r in rows)
        return True
    i = next(i for i, r in enumerate(rows) if r['dir'] == 'out' and m(r).get('method') == 'session/prompt')
    if how.startswith('model:'):
        n = next(n for n, r in enumerate(rows) if r['dir'] == 'out' and m(r).get('method') == 'session/new')
        a = next(a for a in range(n + 1, len(rows)) if 'configOptions' in (m(rows[a]).get('result') or {}))
        b = next(b for b in range(a + 1, len(rows)) if rows[b]['dir'] == 'out')
        opts = m(rows[a])['result']['configOptions']
        want = next(o for o in opts if o['id'] == 'model')['currentValue']
        rows[a] = json.loads(json.dumps(rows[a]))
        next(o for o in m(rows[a])['result']['configOptions'] if o['id'] == 'model')['currentValue'] = how[6:]
        sid = m(rows[a])['result'].get('sessionId')
        rows[b:b] = [{'dir': 'out', 'msg': {'jsonrpc': '2.0', 'id': 9001, 'method': 'session/set_config_option',
                                             'params': {'sessionId': sid, 'configId': 'model', 'value': want}}},
                     {'dir': 'in', 'msg': {'jsonrpc': '2.0', 'id': 9001, 'result': {'configOptions': opts}}}]
        open(dst, 'w').writelines(json.dumps(r) + '\n' for r in rows)
        return True
    out = ([r for r in rows if not (r['dir'] == 'out' and m(r).get('method') == 'session/cancel')] if how == 'nocancel' else
           rows[:next(j for j in range(i + 1, len(rows)) if rows[j]['dir'] == 'in' and m(rows[j]).get('id') == rows[i]['msg']['id'])]
           if how == 'hang' else rows[:i + 1] + [{'dir': 'in', 'msg': {'jsonrpc': '2.0', 'id': rows[i]['msg']['id'],
           'error': {'code': -32603, 'message': open(how[4:]).read().strip()}}}])
    open(dst, 'w').writelines(json.dumps(r) + '\n' for r in out)
    return True
def twoturns(d):
    ack, r1, r2 = json.load(open(d + '.ack')), open(J(d, 'turn-1', 'result.txt')).read(), open(J(d, 'turn-2', 'result.txt')).read()
    e1, e2 = json.load(open(J(d, 'turn-1', 'envelope.json'))), json.load(open(J(d, 'envelope.json')))
    return (ack['status'] == 'spawned' and ack['expect'] == 1 and ack['delivery'] == 'steer' and r1.startswith('turn 1 pid ')
            and r2.startswith('turn 2 pid ') and r1.split()[-1] == r2.split()[-1] and open(J(d, 'result.txt')).read() == r2
            and (e1['turn'], e2['turn'], e2['delivery'], e2['status']) == (1, 2, 'steer', 'ok'))
def seat(d, prov, sid, opt):   # an ACP seat's launch + its first two turns (ONE agent process across all four)
    ack, calls, h = json.load(open(d + '.ack')), rpc(d + '.rec'), json.load(open(J(d, 'session')))
    argv, env = open(d + '.argv').read().split('\n')[:-1], open(d + '.env').read().split('\n')[:-1]
    ends = [json.load(open(J(d, 'turn-%d' % n, 'envelope.json'))) for n in (1, 2)]
    ok = ((ack['status'], ack['expect'], ack['delivery']) == ('spawned', 1, 'queue') and len(open(d + '.pids').read().split()) == 1
          and [c for c, _ in calls] == ['initialize', 'session/new', 'session/set_config_option'] + ['session/prompt'] * 4
          and (calls[2][1].get('configId'), calls[2][1].get('value')) == (opt, 'low')
          and all((e['status'], e['session'], e['effort'], e['delivery']) == ('ok', sid, 'low', 'queue') for e in ends)
          and all(open(J(d, 'turn-%d' % n, 'result.txt')).read() == 'PONG' for n in (1, 2)))
    ok = ok and env[3] == h['cwd']   # the agent runs IN the seat's canonical cwd (the process cwd, every ACP wire)
    if prov == 'grok':
        return ok and argv == ['agent', '--no-leader', '--always-approve', '-m', 'grok-4.6', 'stdio'] and env[2] == '1'
    return ok and argv == ['acp'] and env[1] == 'UNSET'   # its store and key: row 39s
def modelset(rec, d):   # the seat's model set as a session option BEFORE the effort, then the turn runs on it
    calls = rpc(rec)
    return ([c for c, _ in calls][:5] == ['initialize', 'session/new'] + ['session/set_config_option'] * 2 + ['session/prompt']
            and [(p.get('configId'), p.get('value')) for _, p in calls[2:4]] == [('model', 'opencode-go/deepseek-v4.1-flash'), ('effort', 'low')]
            and json.load(open(J(d, 'turn-1', 'envelope.json')))['status'] == 'ok' and open(J(d, 'turn-1', 'result.txt')).read() == 'PONG')
def firstprompt(d):   # the agent's first session/prompt text IS prompt.txt, byte for byte
    return next(p for c, p in rpc(d + '.rec') if c == 'session/prompt')['prompt'] == [{'type': 'text', 'text': open(J(d, 'prompt.txt')).read()}]
def vocab(d, need, statuses, last):
    V = {'turn_start', 'text', 'thought', 'tool_call', 'tool_result', 'file', 'usage', 'turn_end', 'error', 'progress'}
    es = evs(d)
    ends = [e for e in es if e['kind'] == 'turn_end']
    calls = {e.get('call') for e in es if e['kind'] == 'tool_call'}
    return (all(isinstance(e.get('t'), str) and e.get('kind') in V and isinstance(e.get('turn'), int) for e in es)
            and set(need.split(',')) <= {e['kind'] for e in es}
            and [e['turn'] for e in ends] == list(range(1, len(ends) + 1)) and [e['status'] for e in ends] == statuses.split(',')
            and all(e.get('session') and e.get('final') for e in ends)
            and all(e.get('call') in calls for e in es if e['kind'] in ('tool_result', 'file'))
            and all(any(x['kind'] == 'usage' and x['turn'] == e['turn'] for x in es[:i]) for i, e in enumerate(es) if e['kind'] == 'turn_end')
            and ends[-1]['text'].strip() == last and open(J(d, 'result.txt')).read() == ends[-1]['text'])
def isolated(d, n, ans):   # the turn said narration, then a tool, then the answer: the turn keeps the answer only
    said = ''.join(e.get('text', '') for e in evs(d) if e['turn'] == int(n) and e['kind'] == 'text')
    return said.strip() != ans and said.rstrip().endswith(ans) and open(J(d, 'turn-%s' % n, 'result.txt')).read() == ans
def cancelled(d, n, how):   # synthetic: the agent left its tool open, the client closed it | own: the agent closed it
    es = [e for e in evs(d) if e['turn'] == int(n)]
    env = json.load(open(J(d, 'turn-%s' % n, 'envelope.json')))
    calls, use = [e['call'] for e in es if e['kind'] == 'tool_call'], [e for e in es if e['kind'] == 'usage']
    res = [(e['call'], e['status'], e.get('synthetic')) for e in es if e['kind'] == 'tool_result']
    return ((env['status'], env['rc']) == ('cancelled', 1) and open(J(d, 'turn-%s' % n, 'result.txt')).read() == ''
            and len(calls) == 1 and len(use) == 1 and res == [(calls[0], 'cancelled', True) if how == 'synthetic' else (calls[0], 'ok', None)]
            and ((env['usage'] == dict(use[0]['usage'], cost_usd=use[0]['cost_usd']) and env['usage']['in'] > 0) if how == 'synthetic'
                 else (use[0].get('usage_unknown') is True and env['usage'] is None)))
def onefile(d, n, here):   # the raw wire reports the edit twice (translate alone yields two file events); the seat records one
    sys.path.insert(0, here)
    import acp
    ups = [(r['msg'].get('params') or {}).get('update') for r in map(json.loads, open(J(d, 'stream.jsonl'))) if r['dir'] == 'in' and isinstance(r['msg'], dict)]
    raw = [x for u in ups if isinstance(u, dict) and 'sessionUpdate' in u for x in acp.translate(u, 0) if x['kind'] == 'file']
    got = [e for e in evs(d) if e['kind'] == 'file' and e['turn'] == int(n)]
    return len(raw) == 2 and len(got) == 1 and (got[0]['call'], got[0]['path']) == (raw[0]['call'], raw[0]['path'])
def resumed(d, ack, rec, argv, sid):
    calls, h, e = rpc(rec), json.load(open(J(d, 'session'))), json.load(open(J(d, 'envelope.json')))
    return (json.load(open(ack))['expect'] == 5 and len(open(d + '.pids').read().split()) == 2
            and [c for c, _ in calls] == ['initialize', 'session/resume', 'session/set_config_option', 'session/prompt']
            and (calls[1][1]['sessionId'], calls[1][1]['cwd'], calls[2][1]['value']) == (sid, h['cwd'], 'low')
            and open(argv).read().split('\n')[:-1][-3:] == ['-m', 'grok-4.6', 'stdio']
            and (e['turn'], e['status'], e['session']) == (5, 'ok', sid) and open(J(d, 'turn-5', 'result.txt')).read() == 'PONG')
def ocresumed(d, ack, rec, argv, envf, sid):   # resumed()'s opencode twin (its store and key: row 39s)
    calls, h, e = rpc(rec), json.load(open(J(d, 'session'))), json.load(open(J(d, 'envelope.json')))
    env = open(envf).read().split('\n')[:-1]
    return (json.load(open(ack))['expect'] == 5 and len(open(d + '.pids').read().split()) == 2
            and [c for c, _ in calls] == ['initialize', 'session/resume', 'session/set_config_option', 'session/prompt']
            and (calls[1][1]['sessionId'], calls[1][1]['cwd'], calls[2][1]['value']) == (sid, h['cwd'], 'low')
            and open(argv).read().split('\n')[:-1] == ['acp'] and env[3] == h['cwd'] and env[1] == 'UNSET'
            and (e['turn'], e['status'], e['session']) == (5, 'ok', sid) and open(J(d, 'turn-5', 'result.txt')).read() == 'PONG')
def callerstore(d, store, *runs):   # an opencode seat on its CALLER's store: per launch, ONE run of the CLI (argv acp, no
    for argv, envf in zip(runs[::2], runs[1::2]):   # auth import before it), XDG_DATA_HOME as the caller's, no key in its env
        env = open(envf).read().split('\n')[:-1]
        if not (open(argv).read().split('\n')[:-1] == ['acp'] and env[0] == store and env[4] == 'key='):
            return False
    return len(open(d + '.pids').read().split()) == len(runs) // 2 and not os.path.exists(J(d, 'xdg'))
def forgot(d, log, sid, cwd, sent, rc):   # an abandoned opencode turn: its session deleted ONCE (session delete <sid>
    es, h = evs(d), [l for l in open(log).read().split('\n') if l]   # --standalone, in the seat's cwd) BEFORE its
    at = lambda f: next((i for i, e in enumerate(es) if f(e)), -1)   # turn_end; a later send fails typed (error handshake, rc 1)
    gone = at(lambda e: e['kind'] == 'progress' and str(e.get('text', '')).startswith('session %s deleted (rc 0)' % sid))
    env, last = json.load(open(J(d, 'turn-1', 'envelope.json'))), json.loads(open(sent).read().strip().split('\n')[-1])
    return (h == ['session delete %s --standalone' % sid, cwd] and -1 < gone < at(lambda e: e['kind'] == 'turn_end')
            and (env['status'], env['rc'], env['session']) == ('incomplete', 143, sid)
            and rc == '1' and (last['status'], last['reason'], last['session']) == ('error', 'handshake', sid))
def refused(*specs):   # rc:dir:reason -- typed before any prompt reached the agent it asked
    for spec in specs:
        rc, d, why = spec.split(':')
        e, calls = json.load(open(J(d, 'envelope.json'))), [c for c, _ in rpc(d + '.rec')]
        if not (rc == '2' and (e['status'], e['rc'], e['reason']) == ('refused', 2, why) and 'initialize' in calls
                and 'session/prompt' not in calls and not os.path.exists(J(d, 'turn-1'))):
            return False
    return True
def queued(d, sent):   # a send while turn 1 runs: acked expect 2 at once, its marker precedes turn 1's end, turn 2 opens after it
    es, s = evs(d), json.load(open(sent))
    at = lambda f: next((i for i, e in enumerate(es) if f(e)), -1)
    mark, end1 = at(lambda e: e['kind'] == 'progress' and e.get('expect') == 2), at(lambda e: e['kind'] == 'turn_end' and e['turn'] == 1)
    return ((s['status'], s['expect'], s['delivery']) == ('sent', 2, 'queue') and -1 < mark < end1 < at(lambda e: e['kind'] == 'turn_start' and e['turn'] == 2)
            and [(e['turn'], e['status']) for e in es if e['kind'] == 'turn_end'] == [(1, 'ok'), (2, 'ok')]
            and (len(open(d + '.pids').read().split()) == 1 if os.path.exists(d + '.pids') else True))
def private(*argvs):   # each seat's own process, never a shared server: grok --no-leader, codex --no-daemon, opencode a bare acp
    for f in argvs:
        a = open(f).read().split('\n')[:-1]
        if not (('stdio' in a and '--no-leader' in a[:a.index('stdio')] and not {'--leader', '--leader-socket', 'leader', 'serve', 'headless'} & set(a))
                or (a[-1:] == ['app-server'] and '--no-daemon' in a and not {'--remote', '--remote-auth-token-env', '--listen', 'daemon', 'proxy', 'remote-control'} & set(a))
                or a == ['acp']):
            return False
    return len(argvs) == 6
def rpcin(f):   # the client lines a codex stub received (CX_STDIN_LOG), one list per launch: [(method, params)]; <EOF> = its stdin closed
    runs = []
    for ln in open(f).read().split('\n'):
        m = {'method': '<EOF>'} if ln == '<EOF>' else json.loads(ln) if ln.strip() else None
        if m and m.get('method') == 'initialize':
            runs.append([])
        if m:
            runs[-1].append((m.get('method'), m.get('params') or {}))
    return runs
CXARGV = ['--no-daemon', '--disable', 'memories', '--disable', 'hooks', '-c', 'web_search="live"', 'app-server']
def codexseat(d, cwd, sendfile):   # ONE app-server for both turns: initialize, initialized, thread/start, then a turn/start per turn
    a, runs, calls = open(d + '.argv').read().split('\n')[:-1], rpcin(d + '.stdin'), open(d + '.calls').read().split()
    e1, e2 = [json.load(open(J(d, 'turn-%d' % n, 'envelope.json'))) for n in (1, 2)]
    k1, k2 = json.load(open(d + '.ack')), json.load(open(d + '.ack2'))
    turn = lambda f: {'threadId': 'th_stub123', 'input': [{'type': 'text', 'text': open(f).read()}], 'effort': 'high'}
    use = {'in': 6, 'out': 3, 'cache_read': 4, 'reasoning': 1}   # the stub's two model calls, summed, the cache out of in
    return (a == CXARGV and calls == ['1'] and len(runs) == 1 and [m for m, _ in runs[0]] == ['initialize', 'initialized', 'thread/start', 'turn/start', 'turn/start', '<EOF>']
            and runs[0][2][1] == {'cwd': cwd, 'sandbox': 'danger-full-access', 'approvalPolicy': 'never', 'model': 'gpt-5.6-terra', 'ephemeral': False}
            and [p for _, p in runs[0][3:5]] == [turn(J(d, 'prompt.txt')), turn(sendfile)]
            and open(d + '.cwd').read().split() == [cwd] and os.path.exists(d + '.idle')
            and (k1['status'], k1['expect'], k1['delivery'], k2['status'], k2['expect']) == ('spawned', 1, 'steer', 'sent', 2)
            and (e1['status'], e1['session'], e2['status'], e2['session'], e2['turn']) == ('ok', 'th_stub123', 'ok', 'th_stub123', 2)
            and open(J(d, 'turn-1', 'result.txt')).read() == open(J(d, 'turn-2', 'result.txt')).read() == 'stub ok'
            and e1['usage'] == e2['usage'] == use)
RP_TH = '01a193b2-8163-7903-9113-176b78d910aa'   # the recorded thread (fixtures/codex-appserver-sample.jsonl)
def codexreplay(d):   # the recorded wire's four turns: a steered turn, an interrupted one, one more, one on a resumed thread
    es, calls = evs(d), open(d + '.calls').read().split()
    strays = [e['text'] for e in es if e['kind'] == 'progress' and 'belongs to no open turn' in e.get('text', '')]
    late = 'exec-fb7fdd53-11e2-4278-b1ed-c5d03bdf103c'   # turn 1's command: codex finished it 14 s after turn 1 completed
    return ([(e['turn'], e['status']) for e in es if e['kind'] == 'turn_end'] == [(1, 'ok'), (2, 'cancelled'), (3, 'ok'), (4, 'ok')]
            and [result(d, n) for n in (1, 2, 3, 4)] == ['SECOND', '', 'AFTER', 'STEERED'] and calls == ['2']
            and all(json.load(open(J(d, 'turn-%d' % n, 'envelope.json')))['session'] == RP_TH for n in (1, 2, 3, 4))
            and [x.split(' belongs')[0] for x in strays] == ['item/completed commandExecution', 'thread/tokenUsage/updated']
            and [e['turn'] for e in es if e.get('call') == late] == [1] and not [e for e in es if e['kind'] == 'text' and e['turn'] == 1 and e['text'] != 'SECOND'])
def codexusage(d):   # each turn's OWN tokens: the sum of its model calls' `last` (turn 1 made two), never the thread's total
    want = [{'in': 4995, 'out': 36, 'cache_read': 29568, 'cache_write': 0, 'reasoning': 0},
            {'in': 5441, 'out': 39, 'cache_read': 17152, 'cache_write': 0, 'reasoning': 0},
            {'in': 312, 'out': 6, 'cache_read': 22400, 'cache_write': 0, 'reasoning': 0},
            {'in': 3411, 'out': 7, 'cache_read': 22528, 'cache_write': 0, 'reasoning': 0}]
    got = [json.load(open(J(d, 'turn-%d' % n, 'envelope.json')))['usage'] for n in (1, 2, 3, 4)]
    return got == want and [e['usage'] for e in evs(d) if e['kind'] == 'usage'] == want
def codexfiles(d):   # fileChange items -> one file event per change path, paired to its call; a failed command is an error result
    es = evs(d)
    return ([(e['call'], e['path']) for e in es if e['kind'] == 'file'] == [('fc-7', '/w/file-1.txt'), ('fc-11', '/w/file-2.txt'), ('fc-11', '/w/file-1.txt')]
            and [e['call'] for e in es if e['kind'] == 'tool_result' and e['status'] == 'error'] == ['exec-8']
            and json.load(open(J(d, 'envelope.json')))['status'] == 'ok' and open(J(d, 'result.txt')).read() == 'FINAL-3: the audit')
def steered(d, sendfile):   # a send to a busy codex seat JOINS its turn: a turn/start the app-server steers in, one turn answering both
    runs, sent, ends = rpcin(d + '.stdin'), json.load(open(d + '.sent')), [(e['turn'], e['status']) for e in evs(d) if e['kind'] == 'turn_end']
    said = [open(J(d, 'prompt.txt')).read(), open(sendfile).read()]
    return ((sent['status'], sent['expect'], sent['delivery']) == ('sent', 1, 'steer') and len(runs) == 1
            and [m for m, _ in runs[0]] == ['initialize', 'initialized', 'thread/start', 'turn/start', 'turn/start', '<EOF>']
            and runs[0][4][1] == {'threadId': 'th_stub123', 'input': [{'type': 'text', 'text': said[1]}]}
            and ends == [(1, 'ok')] and result(d, 1).split(': ', 1)[1] == ' | '.join(said) and not os.path.exists(J(d, 'turn-2')))
def staled(d, sendfile, waited):   # a send its turn outran opens the next turn, and wait follows the input there; the
    # command turn 1 left running completes inside turn 2, with its usage: both are strays, never turn 2's
    runs, sent, w, es = rpcin(d + '.stdin'), json.load(open(d + '.sent')), json.loads(open(waited).read().strip().split('\n')[-1]), evs(d)
    said = open(sendfile).read()
    strays = [e['text'].split(' belongs')[0] for e in es if e['kind'] == 'progress' and 'belongs to no open turn' in e.get('text', '')]
    return ((sent['status'], sent['expect']) == ('sent', 1) and [m for m, _ in runs[0]][3:] == ['turn/start', 'turn/start', '<EOF>']
            and runs[0][4][1]['input'] == [{'type': 'text', 'text': said}] and result(d, 1) == 'turn 1: ' + open(J(d, 'prompt.txt')).read()
            and result(d, 2) == 'turn 2: ' + said and (w['turn'], w['status']) == (2, 'ok')
            and [e['turn'] for e in es if e.get('call') == 'exec-late-1'] == [1] and strays == ['item/completed commandExecution', 'thread/tokenUsage/updated']
            and json.load(open(J(d, 'turn-2', 'envelope.json')))['usage'] == {'in': 6, 'out': 3, 'cache_read': 4, 'reasoning': 1})
CXCOLLAB = {'in': 506, 'out': 53, 'cache_read': 4, 'reasoning': 1}   # the main turn's two calls (10/4/3/1) + its collaborator's (500/50)
def collab(d):   # a collaborator codex spawned reports on its own thread: one seat turn, the main answer, none of the
    # collaborator's text, thought or calls -- but its tokens are the turn's, and its items and turns show the seat alive
    es = evs(d)
    return ([(e['turn'], e['status']) for e in es if e['kind'] == 'turn_end'] == [(1, 'ok')] and result(d, 1) == 'FINAL: the main answer'
            and not [e for e in es if 'SUB' in (e.get('text') or '') or e.get('call') == 'exec-sub']
            and [(e['turn'], e['text']) for e in es if e['kind'] == 'progress' and e.get('text', '').startswith('collaborator')]
            == [(1, 'collaborator turn/started'), (1, 'collaborator item/completed reasoning'), (1, 'collaborator item/completed commandExecution'),
                (1, 'collaborator item/completed agentMessage'), (1, 'collaborator turn/completed')]
            and json.load(open(J(d, 'turn-1', 'envelope.json')))['usage'] == CXCOLLAB)
def interrupted(d, pids, status, rc):   # the polite close of an rpc seat mid-turn (its deadline, a stop): turn/interrupt
    # naming the open turn, then stdin EOF; the turn typed by the close, and no process left
    runs, e = rpcin(d + '.stdin'), json.load(open(J(d, 'turn-1', 'envelope.json')))
    alive = [p for p in open(pids).read().split() if subprocess.run(['kill', '-0', p], capture_output=True).returncode == 0]
    return ([(m, p) for m, p in runs[0][-2:]] == [('turn/interrupt', {'threadId': 'th_stub123', 'turnId': 'turn-1'}), ('<EOF>', {})]
            and (e['status'], e['rc'], e['session']) == (status, int(rc), 'th_stub123') and not alive)
def cxresumed(d, ack, cwd, bad, badout):   # a relaunch resumes the handle's thread (never a new one); a resume refused is a typed launch failure
    runs, k, e = rpcin(d + '.stdin'), json.load(open(ack)), json.load(open(J(d, 'turn-2', 'envelope.json')))
    b = json.loads(open(badout).read().strip().split('\n')[-1])
    return (len(runs) == 2 and [m for m, _ in runs[1]] == ['initialize', 'initialized', 'thread/resume', 'turn/start', '<EOF>']
            and runs[1][2][1] == {'threadId': 'th_stub123', 'cwd': cwd, 'sandbox': 'danger-full-access', 'approvalPolicy': 'never', 'model': 'gpt-5.6-terra'}
            and runs[1][3][1] == {'threadId': 'th_stub123', 'input': [{'type': 'text', 'text': 'turn two\n'}], 'effort': 'high'}
            and (k['status'], k['expect']) == ('spawned', 2) and (e['status'], e['session'], e['turn']) == ('ok', 'th_stub123', 2)
            and bad == '1' and (b['status'], b['reason']) == ('error', 'handshake') and 'no rollout found for thread id th_stub123' in b['reset']
            and not os.path.exists(J(d, 'turn-3')))
def lsep(d, out):   # a turn_end whose text holds a raw U+2028 is one line to wait, never two fragments
    end = [ln for ln in open(J(d, 'events.jsonl'), 'rb').read().split(b'\n') if b'"turn_end"' in ln]
    env = json.loads(open(out).read().strip().split('\n')[-1])
    return (len(end) == 1 and '\u2028'.encode() in end[0] and (env['status'], env['turn']) == ('ok', 1)
            and '\u2028' in open(J(d, 'turn-1', 'result.txt')).read())
def mails(log, d, n, last):   # every mail push for d, in order: exactly n, the last one carrying the text `last` whole
    cs = pushes(log, d, 'mail to driver')
    return len(cs) == int(n) and '\n'.join(cs[-1][2][4:]) == last
def follows(sh, d):   # events --follow on a live seat ends within 2 s of its stop, the closing turn_end drained first
    import subprocess, time
    f = subprocess.Popen([sh, 'events', d, '--follow'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    if f.poll() is not None:   # it must FOLLOW a live seat, not print once and leave
        return False
    subprocess.run([sh, 'stop', d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        out = f.communicate(timeout=2)[0].decode()
    except subprocess.TimeoutExpired:
        f.kill()
        f.communicate()
        return False
    return f.returncode == 0 and re.findall(r'^── turn (\d+) ([a-z-]+)', out, re.M) == [('1', 'incomplete')]   # the rendered turn_end
def pushed(d, kind, out, err):   # each push records its outcome as ONE progress event: posted, or failed with the error
    ps = [e for e in evs(d) if e['kind'] == 'progress' and 'push' in e]
    return ([(e['text'], e['push']) for e in ps] == [('push %s %s' % (kind, out), out)] and ps[0]['turn'] == 1
            and (err in (ps[0].get('error') or '') if err else not {'error', 'reply'} & set(ps[0])))
def unwaited(d):   # a silent connection the host leaves open costs the push nothing: its event lands within 1 s of the turn_end
    import datetime
    at = lambda f: datetime.datetime.fromisoformat(next(e['t'] for e in evs(d) if f(e)))
    return (at(lambda e: e['kind'] == 'progress' and e.get('push') == 'posted') - at(lambda e: e['kind'] == 'turn_end')).total_seconds() <= 1
def nocancel(d, launches, rec):   # a send to a BUSY seat never cancels its turn: turn 1 ends ok with its own answer, the
    es = evs(d)                    # busy send's message is answered after it, on no extra launch, and no cancel reaches the CLI
    ends = [(e['turn'], e['status']) for e in es if e['kind'] == 'turn_end']
    e1, r1 = json.load(open(J(d, 'turn-1', 'envelope.json'))), open(J(d, 'turn-1', 'result.txt')).read()
    n = len(open(d + '.pids').read().split()) if os.path.exists(d + '.pids') else int(open(d + '.calls').read())
    sent = json.load(open(d + '.sent'))   # delivered INTO the running turn (steer: expect 1) or behind it (queue: expect 2)
    ok = ends == [(1, 'ok'), (2, 'ok')] and (e1['status'], e1['rc']) == ('ok', 0) and bool(r1.strip()) and n == int(launches)
    ok = ok and sent['status'] == 'sent' and sent['expect'] == (1 if sent['delivery'] == 'steer' else 2)
    if rec.endswith('.rec'):     # ACP: the client lines the agent received -- prompts only, never session/cancel
        ok = ok and [c for c, _ in rpc(rec) if c.startswith('session/') and c != 'session/set_config_option'] == ['session/new', 'session/prompt', 'session/prompt']
    else:                        # stdin: every line the CLI read is a user message, never a control request; then the stop's EOF
        ok = ok and [ln if ln == '<EOF>\n' else json.loads(ln).get('type') for ln in open(rec)] == ['user', 'user', '<EOF>\n']
    return ok
def grokview(out):   # the grok 228-line recorded stream (fixtures/grok-events-sample.jsonl) as the readable view
    ls = open(out, encoding='utf-8').read().split('\n')[:-1]
    tools = [ln for ln in ls if ln.startswith('$ ')]
    return (20 <= len(ls) <= 25 and not any(ln.startswith('{') for ln in ls) and len([ln for ln in ls if ln.startswith('> t')]) == 3
            and [ln.split(' {')[0] for ln in tools] == ['$ read_file', '$ read_file', '$ run_terminal_command'] and all(len(ln) <= 340 for ln in tools)
            and re.findall(r'^── turn (\d) ([a-z]+) · \d\d:\d\d:\d\d$', '\n'.join(ls), re.M) == [('1', 'ok'), ('2', 'ok'), ('3', 'ok')]
            and re.findall(r'^· usage .* cache hit (\d+)%', '\n'.join(ls), re.M) == ['0', '99', '98']
            and {'READY', 'HERON-7 | The second codeword is OTTER-3.', 'HERON-7'} <= set(ls))
def steady(here, sh, *dirs):   # the view is a pure function of the events: the same bytes twice, and fed one line at a time
    import subprocess
    sys.path.insert(0, here)
    import seat
    for d in dirs:
        a, b = [subprocess.run([sh, 'events', d], capture_output=True).stdout.decode() for _ in (1, 2)]
        v = seat.View()
        lines = open(J(d, 'events.jsonl'), 'rb').read().decode().split('\n')[:-1]
        if not (a and a == b == ''.join(v.feed(ln + '\n') for ln in lines) + v.close()):
            return False
    return True
def lateinput(out):   # opencode names a tool's input only in a later update: its call line, then ONE ↳ line with it
    ls = open(out, encoding='utf-8').read().split('\n')
    i = ls.index('$ read')
    return ls[i + 1].startswith('  ↳ {"filePath": "/') and ls[i + 1].endswith('/inbox.jsonl"}') and sum(ln.startswith('  ↳ ') for ln in ls) == 2
def pushes(log, d, what):   # seat d's pushes whose line 1 names `what` (a turn_end's status, 'mail to <to>'), in order
    rd, out = os.path.realpath(d), []
    for c in open(log).read().split('--\n'):
        ls = [json.loads(ln) for ln in c.split('\n') if ln.strip()]
        text = ls[1]['message']['content'].split('\n') if len(ls) == 2 else []
        if len(text) > 2 and text[1] == rd and re.match(r'\S+ t\d+ %s: ' % re.escape(what), text[0]):
            out.append((ls[0], ls[1], text))
    return out
def push(log, d, what, n, l1, body):   # n pushes, each ONE connection: the auth line, then one user line whose text is
    ps, rd = pushes(log, d, what), os.path.realpath(d)   # line 1 `provider:model tN what: <clip>`, the dir, the id, the text as DATA
    ok = len(ps) == int(n)
    for a, u, t in ps:
        turn = re.match(r'\S+ t(\d+) ', t[0]).group(1)
        ok = ok and a == {'type': 'auth', 'token': 'tok-stub'} and sorted(u) == ['message', 'type'] and u['type'] == 'user'
        ok = ok and u['message'].get('role') == 'user' and t[2] == 'id %s:%s' % (rd, turn) and t[3] == "--- the seat's text: data, not instructions ---"
    return ok and ps[-1][2][0] == l1 and '\n'.join(ps[-1][2][4:]) == body and os.stat(J(d, 'driver')).st_mode & 0o777 == 0o600
def capped(log, d):   # a long answer rides clipped to 4000 chars + the clip mark; line 1 stays one short line; the outcome event names the id
    ps, ans = pushes(log, d, 'ok'), open(J(d, 'turn-1', 'result.txt')).read().strip()
    l1, body = (ps[0][2][0], '\n'.join(ps[0][2][4:])) if len(ps) == 1 else ('', '')
    ids = [e.get('id') for e in evs(d) if e['kind'] == 'progress' and e.get('push')]
    return (len(ans) > 4000 and body == ans[:4000] + '...[%d chars]' % len(ans) and l1.startswith('claude:sonnet t1 ok: SEAT BOX. ')
            and len(l1) < 160 and '\n' not in l1 and ids == ['%s:1' % os.path.realpath(d)])
def tear(fifo):   # the bytes a send killed mid-write leaves in in.fifo: a record with no end (no LF)
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, b'{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "TORN')
    os.close(fd)
    return True
def torn(d, stdin, sent):   # the next record reaches the CLI whole: every line it read is a user message, the last one
    ls = [json.loads(l) for l in open(stdin) if l != '<EOF>\n']   # exactly the sent file; the torn bytes are dropped as one typed progress event
    want, end = open(sent).read(), json.load(open(J(d, 'turn-2', 'envelope.json')))
    drops = [e for e in evs(d) if e['kind'] == 'progress' and str(e.get('text', '')).startswith('torn input dropped: ')]
    return (all(isinstance(x, dict) and x.get('type') == 'user' for x in ls) and len(ls) == 2
            and ls[-1]['message']['content'][0]['text'] == want and (end['status'], end['turn']) == ('ok', 2)
            and open(J(d, 'turn-2', 'result.txt')).read() == want and len(drops) == 1 and 'TORN' in drops[0]['text'])

def relaunch1(d, calls, a1, a2):   # two sends racing to a gone seat: ONE launch answers both, one acks spawned, the other sent
    acks = sorted(json.loads(open(f).read().strip().split('\n')[-1])['status'] for f in (a1, a2))
    rs = [open(J(d, 'turn-%d' % n, 'result.txt')).read() for n in (1, 2, 3)]
    pids = [r.split()[-1] for r in rs]
    ends = [(e['turn'], e['status']) for e in evs(d) if e['kind'] == 'turn_end']
    return (open(calls).read().strip() == '1' and acks == ['sent', 'spawned'] and ends == [(1, 'ok'), (2, 'ok'), (3, 'ok')]
            and pids[1] == pids[2] != pids[0])
def trimmed(out, d):   # E10: the envelope line stdout carries = envelope.json with its usage trimmed to in, cache_read,
    line = open(out).read().rstrip('\n').split('\n')[-1]   # cache_write, out and the turn's cost; the file keeps reasoning
    env, disk = json.loads(line), json.load(open(J(d, 'envelope.json')))
    want = {'in': 10, 'cache_read': 31151, 'cache_write': 246, 'out': 98, 'cost_usd': 0.0041071}   # turn 3 of the recorded seat
    costs = [json.load(open(J(d, 'turn-%d' % n, 'envelope.json')))['usage'].get('cost_usd') for n in (1, 2, 3)]
    return (list(env['usage']) == ['in', 'cache_read', 'cache_write', 'out', 'cost_usd'] and env['usage'] == want
            and disk['usage'] == dict(want, reasoning=87) and dict(env, usage=None) == dict(disk, usage=None)
            and costs == [0.059393, 0.0156844, 0.0041071] and len(json.dumps(env['usage'], separators=(',', ':'))) <= 100
            and len(line.encode()) < len(open(J(d, 'envelope.json'), 'rb').read().rstrip(b'\n')))
def settings(*argvs):   # every claude launch carries ONE --settings, the inbound refusal + hooks off, and nothing else sets settings
    for f in argvs:
        a = open(f).read().split('\n')[:-1]
        if not (a.count('--settings') == 1 and a[a.index('--settings') + 1] == '{"crossSessionInbound":"refuse","disableAllHooks":true}'
                and not any(w.startswith('--setting-sources') for w in a)):
            return False
    return len(argvs) == 2
def inbox(d):
    return [json.loads(l) for l in open(J(d, 'inbox.jsonl'))]
def disabled(feature, *argvs):   # every codex argv disables the feature as a ROOT option (before app-server)
    pre = [open(f).read().split('\n')[:-1] for f in argvs]
    pre = [a[:a.index('app-server')] if 'app-server' in a else [] for a in pre]
    return bool(pre) and all(any(a[i:i + 2] == ['--disable', feature] for i in range(len(a))) for a in pre)
def marks(d):   # the inbox lines the delivery markers name, in order
    return [e['inbox'] for e in evs(d) if e['kind'] == 'progress' and e.get('inbox')]
def gone(d, *pids):   # within 5 s: no wrapper of job dir d and none of pids left running. A zombie is dead (kill -0 still
    # answers for one until it is reaped), and a process mid-exit gets the moment it needs on a loaded machine
    mine = re.compile(r'seat\.py spawn %s ' % re.escape(os.path.realpath(d)))
    for _ in range(50):
        ps = [r.split(None, 2) for r in subprocess.run(['ps', '-A', '-ww', '-o', 'pid=,stat=,command='], capture_output=True,
                                                       text=True).stdout.splitlines()]
        live = [r for r in ps if len(r) == 3 and not r[1].startswith('Z')]
        if not any(r[0] in pids or mine.search(r[2]) for r in live):
            return True
        time.sleep(0.1)
    return False
def result(d, n):
    return open(J(d, 'turn-%s' % n, 'result.txt')).read()
def regone(b, calls0, calls, rc):   # driver mail to a gone seat: rc 1, NO launch; the line waits; a relaunch delivers it
    b = os.path.realpath(b)            # ONCE, after the send's own message; a second restart delivers none
    want = {3: 'turn two\n', 4: 'MAIL from driver:\ndriver while out', 5: 'turn two\n'}
    return (rc == '1' and open(calls0).read().strip() == '1' and open(calls).read().strip() == '3'
            and marks(b) == [1, 2] and len(inbox(b)) == 2 and all(result(b, n) == t for n, t in want.items())
            and not os.path.exists(J(b, 'turn-6')))
def busymail(c, calls, stdin):   # driver mail to a BUSY codex seat joins its running turn (steered in) and never cancels it
    c, es = os.path.realpath(c), evs(c)
    at = lambda f: next((i for i, e in enumerate(es) if f(e)), -1)
    mark, end1 = at(lambda e: e['kind'] == 'progress' and e.get('inbox') == 1), at(lambda e: e['kind'] == 'turn_end' and e['turn'] == 1)
    e1, mail = json.load(open(J(c, 'turn-1', 'envelope.json'))), 'MAIL from driver:\nwhile you work'
    steer = [p for m, p in rpcin(stdin)[0] if m == 'turn/start'][1:]
    return (-1 < mark < end1 and (e1['status'], e1['rc']) == ('ok', 0) and result(c, 1).endswith(' | ' + mail)
            and [(e['turn'], e['status']) for e in es if e['kind'] == 'turn_end'] == [(1, 'ok')] and open(calls).read().strip() == '1'
            and [p['input'] for p in steer] == [[{'type': 'text', 'text': mail}]])
sys.exit(0 if globals()[sys.argv[1]](*sys.argv[2:]) else 1)
PY
  cat >"$SB/acp-stub" <<'STUB'
#!/bin/sh
# `session ...` (opencode: the wrapper deletes an abandoned turn's session): OC_SESSION_LOG appends argv and the cwd per
# call -- after a second, as a real delete takes a moment (its standalone server) that nothing may cut
if [ "${1:-}" = session ]; then
  sleep 1; [ -n "${OC_SESSION_LOG:-}" ] && { echo "$*"; pwd -P; } >>"$OC_SESSION_LOG"; exit 0
fi
[ -n "${ACP_ARGV_LOG:-}" ] && printf '%s\n' "$@" >"$ACP_ARGV_LOG"
[ -n "${ACP_ENV_LOG:-}" ] && { printf '%s\n%s\n%s\n' "${XDG_DATA_HOME:-UNSET}" "${OPENCODE_CONFIG_CONTENT:-UNSET}" "${GROK_DISABLE_AUTOUPDATER:-UNSET}"; pwd -P
  echo "key=${OPENCODE_API_KEY+SET}"; echo "hooks=${GROK_CLAUDE_HOOKS_ENABLED:-UNSET},${GROK_CURSOR_HOOKS_ENABLED:-UNSET}"; } >"$ACP_ENV_LOG"
[ -n "${ACP_PIDFILE:-}" ] && echo $$ >>"$ACP_PIDFILE"
exec python3 @TEST@ --fake "${ACP_REPLAY:-@SEAT@}" "${ACP_MODE:-eof}" "${ACP_REC:-/dev/null}"
STUB
  for c in grok opencode; do
    python3 "$SB/chk.py" derive "$HERE/fixtures/$c-acp-seat-sample.jsonl" "$SB/$c-seat.jsonl" nocancel
    sed -e "s|@TEST@|'$HERE/test_acp.py'|" -e "s|@SEAT@|$SB/$c-seat.jsonl|" "$SB/acp-stub" >"$SB/bin/$c"; chmod +x "$SB/bin/$c"
  done
  python3 "$SB/chk.py" derive "$HERE/fixtures/grok-acp-seat-sample.jsonl" "$SB/grok-hang.jsonl" hang
  python3 "$SB/chk.py" derive "$HERE/fixtures/grok-acp-seat-sample.jsonl" "$SB/grok-402.jsonl" "err:$HERE/fixtures/grok-402-balance.txt"
  python3 "$SB/chk.py" derive "$HERE/fixtures/opencode-acp-seat-sample.jsonl" "$SB/oc-nokey.jsonl" "err:$HERE/fixtures/opencode-missing-api-key.txt"
  stub_run() { PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$0" "$@"; }
  # claude and codex seats (seat.py): ONE stub, test-stub.py, answers claude's stdin wire line by line as
  # the real CLI does (one message line in, one turn out) and codex's app-server over JSON-RPC — its modes
  # and seams (P_ARGV_LOG one argv word per line, P_STDIN_LOG, P_STUB_CALLS, ...) are in its header.
  for c in claude codex; do cp "$HERE/test-stub.py" "$SB/bin/$c"; chmod +x "$SB/bin/$c"; done
  # 202 (checked at the end, so its minute overlaps the suite): a launch slower than a minute -- an ACP handshake whose
  # initialize and session/new answer 35 s late each, both inside the 45 s a handshake call may take -- still acks
  # spawned while its wrapper runs. Falsification: the old 60 s ack window -> "never delivered", rc 1
  python3 -c 'import json, sys
n, out = 0, open(sys.argv[2], "w")
for l in open(sys.argv[1]):
    out.write(l); r = json.loads(l)
    if r["dir"] == "out" and n < 2: n += 1; out.write(json.dumps({"dir": "sleep", "msg": 35}) + "\n")' "$SB/grok-seat.jsonl" "$SB/grok-slow.jsonl"
  ACP_REPLAY="$SB/grok-slow.jsonl" ACP_REC="$SB/a202.rec" PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$0" start \
    --effort low --cwd "$SB/tmp" --out "$SB/a202" grok:grok-4.6 "$SB/q.md" >"$SB/a202.ack" 2>/dev/null & A202=$!
  # 205 (checked at the end, beside 202): A STOP DURING THE LAUNCH, an ACP seat still in its handshake. One handshake ends
  # 3 s in: the wrapper skips its loop, MSG never reaches the agent, and turn 1 closes incomplete, rc 143. One still
  # answering at 10 s (202's): stop TERMs its agent then, the handshake fails, and the launch reads incomplete, rc 143,
  # never a broken CLI. The stop also ends the spawn caller, so the ack finds no reader. Falsification: type early() by
  # its error -> the slow one reads error/handshake; drop acked()'s guard -> a BrokenPipe traceback in job.log.
  python3 -c 'import json, sys
n, out = 0, open(sys.argv[2], "w")
for l in open(sys.argv[1]):
    out.write(l); r = json.loads(l)
    if r["dir"] == "out" and n < 1: n += 1; out.write(json.dumps({"dir": "sleep", "msg": 3}) + "\n")' "$SB/grok-seat.jsonl" "$SB/grok-slow3.jsonl"
  ( for v in 3 70; do f="$SB/grok-slow3.jsonl"; [ $v = 70 ] && f="$SB/grok-slow.jsonl"
      ACP_REPLAY="$f" ACP_REC="$SB/a205-$v.rec" stub_run start --effort low --cwd "$SB/tmp" --out "$SB/a205-$v" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1 & S205=$!
      for i in $(seq 1 100); do [ -s "$SB/a205-$v.rec" ] && break; sleep 0.1; done   # the wrapper is in the handshake
      stub_run stop "$SB/a205-$v" >/dev/null 2>&1; echo $? >"$SB/a205-$v.rc"; wait $S205; done ) & A205=$!
  printf 'turn two\n' >"$SB/q81.md"
  mkdir -p "$SB/gdir"; GD="$(cd "$SB/gdir" && pwd -P)"
  # THE ACP SEATS: start + three sends = each CLI's recorded 4 turns on ONE agent (rows 100-106 read them)
  ACP_PIDFILE="$SB/a100.pids" ACP_REC="$SB/a100.rec" ACP_ARGV_LOG="$SB/a100.argv" ACP_ENV_LOG="$SB/a100.env" \
    stub_run start --effort low --cwd "$SB/tmp" --out "$SB/a100" grok:grok-4.6 "$SB/q.md" >"$SB/a100.ack" 2>/dev/null
  ( cd "$SB" && OPENCODE_API_KEY=sk-caller-env OC_SESSION_LOG="$SB/a101.del" ACP_PIDFILE="$SB/a101.pids" ACP_REC="$SB/a101.rec" ACP_ARGV_LOG="$SB/a101.argv" ACP_ENV_LOG="$SB/a101.env" \
    PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$SELF" start --effort low --cwd "$SB/gdir" --out a101 opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >"$SB/a101.ack" 2>/dev/null )
  for d in a100 a101; do for i in 2 3 4; do stub_run wait "$SB/$d" >/dev/null 2>&1; stub_run send "$SB/$d" "$SB/q81.md" >/dev/null 2>&1; done
    stub_run wait "$SB/$d" >/dev/null 2>&1; done
  t 100 "ACP grok seat: start acks (spawned, expect 1, queue); the handshake sets reasoning_effort low BEFORE the first prompt; four turns on ONE agent process; argv agent --no-leader --always-approve -m grok-4.6 stdio (web on: no --disable-web-search), GROK_DISABLE_AUTOUPDATER=1; turns 1-2 ok PONG on the recorded session" \
    python3 "$SB/chk.py" seat "$SB/a100" grok 01a0d38a-6416-74d3-82f8-80174c82f422 reasoning_effort
  t 101 "ACP opencode seat: the same over a bare opencode acp (2.0 has no --cwd: the agent runs IN the seat's canonical cwd, as grok's does); the variant is set EXPLICITLY (set_config_option effort low before the first prompt), no OPENCODE_CONFIG_CONTENT (2.x takes no model from it on ACP: row 101m); its store and key: row 39s" \
    python3 "$SB/chk.py" seat "$SB/a101" opencode ses_f2c74432dffeoPDAL0Eq6RHjaI effort
  t 102 "EVENTS, both ACP wires (the recorded 4-turn seats): every line {t,kind,turn} from the ten kinds, tool_result and file pair a tool_call, usage precedes each turn_end, turn_ends ok,ok,cancelled,ok with session + final, result.txt = the last ok answer" \
    sh -c "python3 '$SB/chk.py' vocab '$SB/a100' turn_start,text,thought,tool_call,tool_result,file,usage,turn_end,progress ok,ok,cancelled,ok DONE && python3 '$SB/chk.py' vocab '$SB/a101' turn_start,text,thought,tool_call,tool_result,file,usage,turn_end,progress ok,ok,cancelled,ok DONE"
  t 103 "turn_end text isolation: grok narrates, writes the file, then answers — turn 4 keeps DONE only, the narration before the tool is never the answer" \
    python3 "$SB/chk.py" isolated "$SB/a100" 4 DONE
  t 104 "a CANCELLED turn is status cancelled (rc 1, no answer): grok leaves its tool open and the client closes it (synthetic tool_result cancelled; usage real), opencode closes its own (usage all zeros -> usage_unknown, envelope usage null)" \
    sh -c "python3 '$SB/chk.py' cancelled '$SB/a100' 3 synthetic && python3 '$SB/chk.py' cancelled '$SB/a101' 3 own"
  t 105 "grok reports one edit TWICE on the wire (translate alone yields two file events); the seat records ONE file event per call and path" \
    python3 "$SB/chk.py" onefile "$SB/a100" 4 "$HERE"
  t 106 "usage on both wires: out = everything billed as output, reasoning included — grok's outputTokens already carry it (28 of which 26 reasoning; input minus cache), opencode's do not (3 + 82 thought = 85); grok's turn cost rides the usage event and the envelope's usage" \
    sh -c "jq -e '.usage=={\"in\":20434,\"out\":28,\"cache_read\":128,\"cache_write\":0,\"reasoning\":26,\"cost_usd\":0.013974}' '$SB/a100/turn-1/envelope.json' >/dev/null && jq -e '.usage=={\"in\":301,\"out\":85,\"cache_read\":12672,\"reasoning\":82}' '$SB/a101/turn-4/envelope.json' >/dev/null && grep -q '\"cost_usd\": 0.013974' '$SB/a100/events.jsonl'"
  for d in a100 a101; do stub_run stop "$SB/$d" >/dev/null 2>&1; done
  ACP_REPLAY="$HERE/fixtures/grok-acp-resume-sample.jsonl" ACP_PIDFILE="$SB/a100.pids" ACP_REC="$SB/a107.rec" ACP_ARGV_LOG="$SB/a107.argv" \
    stub_run send "$SB/a100" "$SB/q81.md" >"$SB/a107.ack" 2>/dev/null
  stub_run wait "$SB/a100" >/dev/null 2>&1; stub_run stop "$SB/a100" >/dev/null 2>&1
  t 107 "send to a DEAD ACP seat RESUMES it: a second launch, session/resume on the recorded id and cwd (never session/new, never a replaying session/load), the model checked (-m grok-4.6) and the effort RE-SET low before its prompt; turn 5 answers on the same session" \
    python3 "$SB/chk.py" resumed "$SB/a100" "$SB/a107.ack" "$SB/a107.rec" "$SB/a107.argv" 01a0d38a-6416-74d3-82f8-80174c82f422
  OPENCODE_API_KEY=sk-caller-env OC_SESSION_LOG="$SB/a101.del" ACP_REPLAY="$HERE/fixtures/opencode-acp-resume-sample.jsonl" ACP_PIDFILE="$SB/a101.pids" ACP_REC="$SB/a122.rec" \
    ACP_ARGV_LOG="$SB/a122.argv" ACP_ENV_LOG="$SB/a122.env" stub_run send "$SB/a101" "$SB/q81.md" >"$SB/a122.ack" 2>/dev/null
  stub_run wait "$SB/a101" >/dev/null 2>&1; stub_run stop "$SB/a101" >/dev/null 2>&1
  t 122 "…and opencode: the relaunch (a bare opencode acp, IN the recorded cwd), session/resume on the recorded id and cwd, the variant re-set low, no OPENCODE_CONFIG_CONTENT; turn 5 answers on the same session" \
    python3 "$SB/chk.py" ocresumed "$SB/a101" "$SB/a122.ack" "$SB/a122.rec" "$SB/a122.argv" "$SB/a122.env" ses_f2c74432dffeoPDAL0Eq6RHjaI
  # 39s: an opencode seat runs on its CALLER's opencode store and login, as any opencode session does --
  # no private store, no key seeded into one. a101 started from a RELATIVE --out with OPENCODE_API_KEY exported, and
  # relaunched (a122). Falsification: a private store (XDG_DATA_HOME = DIR/xdg) -> the store half; seed it (`auth import`
  # before the agent) -> three CLI runs for two launches; export the key into the seat -> key=SET; write the job-dir line
  # before the dir is canonical -> a relative path here.
  RD="$(cd "$SB/a101" && pwd -P)"
  t 39s "an opencode seat runs on its CALLER's store: across start + relaunch, ONE CLI run per launch (a bare acp: no auth import before it), XDG_DATA_HOME the caller's own (unchanged), no DIR/xdg, no OPENCODE_API_KEY in the agent's env though the caller exported one; the seat's job-dir line is absolute (a relative --out)" \
    sh -c "python3 '$SB/chk.py' callerstore '$SB/a101' '$SB/oc-store' '$SB/a101.argv' '$SB/a101.env' '$SB/a122.argv' '$SB/a122.env' && tail -1 '$SB/a101/prompt.txt' | grep -qxF 'YOUR JOB DIR: $RD (your outbox: $RD/outbox.jsonl).'"
  # 101m (opencode 2.x takes no model from OPENCODE_CONFIG_CONTENT on ACP -- the agent ran the
  # driver's global default): the agent's session starts on ANOTHER model it lists, so the client sets the seat's one as
  # a session option, then the effort that model lists, then prompts. Falsification: drop the set -> model-mismatch.
  python3 "$SB/chk.py" derive "$SB/opencode-seat.jsonl" "$SB/oc-model.jsonl" model:opencode-go/glm-5.3
  ACP_REPLAY="$SB/oc-model.jsonl" ACP_REC="$SB/a101m.rec" stub_run start --effort low --cwd "$SB/gdir" --out "$SB/a101m" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/a101m" >/dev/null 2>&1; stub_run stop "$SB/a101m" >/dev/null 2>&1
  t 101m "an ACP agent whose session starts on ANOTHER model it lists (opencode 2.x runs its global default): the seat's model is SET as a session option, then the effort, before the first prompt; turn 1 answers ok" \
    python3 "$SB/chk.py" modelset "$SB/a101m.rec" "$SB/a101m"
  ACP_REC="$SB/a108a.rec" stub_run --effort max --cwd "$SB/tmp" --out "$SB/a108a" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1; R108A=$?
  ACP_REC="$SB/a108b.rec" stub_run --cwd "$SB/gdir" --out "$SB/a108b" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1; R108B=$?
  ACP_REC="$SB/a108c.rec" stub_run start --effort low --cwd "$SB/tmp" --out "$SB/a108c" grok:grok-9.9 "$SB/q.md" >/dev/null 2>&1; R108C=$?
  t 108 "typed refusals BEFORE any prompt (rc 2, refused, no turn, no session/prompt reached the agent): an effort grok-4.6 does not list (max), an opencode seat naming NO effort for a model that lists low|high|max (the engine never guesses), and grok:grok-9.9, a model the agent does not list (never substituted)" \
    sh -c "python3 '$SB/chk.py' refused '$R108A:$SB/a108a:effort-unsupported' '$R108B:$SB/a108b:effort-unsupported' '$R108C:$SB/a108c:model-mismatch' && jq -e '.reset|test(\"runs grok-4[.]6 and lists grok-4[.]7, grok-4[.]7-build-fast, grok-4[.]6, grok-4[.]5; the seat names grok-9[.]9\")' '$SB/a108c/envelope.json' >/dev/null"
  ACP_ARGV_LOG="$SB/a109a.argv" stub_run --out "$SB/a109a" opencode "$SB/q.md" >/dev/null 2>&1; R109A=$?
  ACP_ARGV_LOG="$SB/a109b.argv" stub_run --effort low --out "$SB/a109b" opencode:foo "$SB/q.md" >/dev/null 2>&1; R109B=$?
  t 109 "an opencode seat names provider/model: a model-less and a non-slash buy are refused before launch (rc 2, refused model-required, no agent argv)" \
    sh -c "[ $R109A -eq 2 ] && [ $R109B -eq 2 ] && [ ! -e '$SB/a109a.argv' ] && [ ! -e '$SB/a109b.argv' ] && jq -e -s 'all(.status==\"refused\" and .reason==\"model-required\")' '$SB/a109a/envelope.json' '$SB/a109b/envelope.json' >/dev/null"
  ACP_REPLAY="$SB/grok-hang.jsonl" ACP_MODE=stubborn ACP_REC="$SB/a110.rec" OC_SESSION_LOG="$SB/a110.del" stub_run start --effort low --cwd "$SB/tmp" --out "$SB/a110" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do grep -q '"kind": "turn_start"' "$SB/a110/events.jsonl" 2>/dev/null && [ -s "$SB/a110.rec.child" ] && break; sleep 0.1; done
  stub_run stop "$SB/a110" >/dev/null 2>&1; echo $? >"$SB/a110.rc"
  t 110 "STOP reaps an ACP seat mid-turn: TERM reaches the agent (it records it), the child in its process group dies, no wrapper remains, and the turn in flight is typed incomplete (rc 143) with its session" \
    sh -c "[ \"\$(cat '$SB/a110.rc')\" = 0 ] && [ -e '$SB/a110.rec.term' ] && python3 '$SB/chk.py' gone '$SB/a110' \$(cat '$SB/a110.rec.child') && jq -e '.status==\"incomplete\" and .rc==143 and .turn==1 and .session==\"01a0d38a-6416-74d3-82f8-80174c82f422\"' '$SB/a110/envelope.json' >/dev/null"
  ACP_REPLAY="$SB/grok-hang.jsonl" ACP_REC="$SB/a111.rec" stub_run --effort low --timeout 3 --cwd "$SB/tmp" --out "$SB/a111" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1
  t 111 "a TIMED-OUT ACP seat (its deadline mid-turn) is typed timeout, rc 142, and still records its session: the checkpoint contract" \
    sh -c "jq -e '.status==\"timeout\" and .rc==142 and .turn==1 and .session==\"01a0d38a-6416-74d3-82f8-80174c82f422\"' '$SB/a111/envelope.json' >/dev/null"
  # 111b (an opencode seat past its deadline was once typed cancelled): opencode 2.x answers its open
  # prompt "cancelled" as it is ended, then exits (the fake: on the deadline close's EOF as on a TERM) -- so a cancelled
  # turn is typed by the deadline too, as an incomplete one is. Falsification: type incomplete turns only -> cancelled, rc 1.
  python3 "$SB/chk.py" derive "$SB/opencode-seat.jsonl" "$SB/oc-hang.jsonl" hang
  ACP_REPLAY="$SB/oc-hang.jsonl" ACP_MODE=termcancel ACP_REC="$SB/a111b.rec" OC_SESSION_LOG="$SB/a111b.del" stub_run --effort low --timeout 3 --cwd "$SB/gdir" --out "$SB/a111b" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  t 111b "...and an agent that answers its open prompt CANCELLED as the deadline ends it (opencode 2.x) is typed timeout too, rc 142, with its session: the deadline types the turn, never the agent's word" \
    sh -c "jq -e '.status==\"timeout\" and .rc==142 and .turn==1 and .session==\"ses_f2c74432dffeoPDAL0Eq6RHjaI\"' '$SB/a111b/envelope.json' >/dev/null"
  # 172 (GUARD 1): THE POLITE DEADLINE, one rule for every ACP wire. opencode keeps a turn's execution
  # claim when TERMed and releases it on a polite close, and its managed background service resumes a kept claim at its
  # next boot -- so the wrapper ends the agent itself at the deadline (AcpSeat.close: stdin EOF first), and the reaper is a
  # backstop 10 s later. Falsification: put the reaper back at the deadline (no wrapper close) -> the first end is term.
  t 172 "THE POLITE DEADLINE, every ACP wire (grok a111, opencode a111b): at the launch deadline the agent sees its stdin close (EOF) before any TERM, and the turn in flight is still typed timeout, rc 142, with its session" \
    sh -c "[ \"\$(head -1 '$SB/a111.rec.ends')\" = eof ] && [ \"\$(head -1 '$SB/a111b.rec.ends')\" = eof ] && jq -e '.status==\"timeout\" and .rc==142 and .session==\"01a0d38a-6416-74d3-82f8-80174c82f422\"' '$SB/a111/envelope.json' >/dev/null && jq -e '.status==\"timeout\" and .rc==142 and .session==\"ses_f2c74432dffeoPDAL0Eq6RHjaI\"' '$SB/a111b/envelope.json' >/dev/null"
  # 173 (GUARD 2): AN ABANDONED TURN'S SESSION IS DELETED. An opencode agent ended mid-turn (here by a stop)
  # keeps the turn's execution claim, and the user's managed opencode service would resume it at its next boot; the wrapper
  # deletes the session (session delete <sid> --standalone, in the seat's cwd) before the turn's envelope lands. The stop
  # closes the agent politely: its first end is its stdin's EOF. Nothing else deletes: an idle stop (a101), a turn the
  # agent cancelled and lived on (a101's turn 3), a grok seat (a110), the deadline's close (a111b). A later send fails typed
  # (the agent answers the resume with an error). Falsification: drop the delete; delete on the deadline close, for grok,
  # or with the agent alive (a .del log appears); stop TERMing the agent's group at once (its first end is term).
  python3 "$SB/chk.py" derive "$HERE/fixtures/opencode-acp-resume-sample.jsonl" "$SB/oc-gone.jsonl" gone
  ACP_REPLAY="$SB/oc-hang.jsonl" ACP_REC="$SB/a173.rec" OC_SESSION_LOG="$SB/a173.del" stub_run start --effort low --cwd "$SB/gdir" --out "$SB/a173" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do grep -q '"kind": "turn_start"' "$SB/a173/events.jsonl" 2>/dev/null && grep -q session/prompt "$SB/a173.rec" 2>/dev/null && break; sleep 0.1; done
  stub_run stop "$SB/a173" >/dev/null 2>&1
  T173=$SECONDS; ACP_REPLAY="$SB/oc-gone.jsonl" stub_run send "$SB/a173" "$SB/q81.md" >"$SB/a173.sent" 2>/dev/null; R173=$?; T173=$((SECONDS - T173))
  # 177: a seat bought without --timeout has no deadline -- its handle records
  # timeout null and its agent runs under a reaper armed with 0 (no alarm). Falsification: default TIMEOUT=900 -> timeout
  # 900 and an armed reaper; drop limit()'s no-timeout branch -> the launch fails.
  ACP_REPLAY="$SB/oc-hang.jsonl" ACP_REC="$SB/a177.rec" stub_run start --effort low --cwd "$SB/gdir" --out "$SB/a177" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do grep -q '"kind": "turn_start"' "$SB/a177/events.jsonl" 2>/dev/null && grep -q session/prompt "$SB/a177.rec" 2>/dev/null && break; sleep 0.1; done
  W177="$(pgrep -f "seat.py spawn $SBP/a177 " | head -1)"; ps -A -ww -o ppid=,command= | awk -v p="${W177:-none}" '$1==p' >"$SB/a177.ps"
  stub_run stop "$SB/a177" >/dev/null 2>&1
  t 177 "NO LIMIT BY DEFAULT: a seat bought without --timeout records timeout null and runs its agent under a reaper armed with 0 (no alarm): no wall clock cuts its work" \
    sh -c "jq -e '.timeout==null' '$SB/a177/session' >/dev/null && grep -q 'wt_alarm.* seat [^ ]*/hands.sh 0 ' '$SB/a177.ps'"
  # 175: an agent TERMed mid-turn from outside (a stop closes politely: it sends no TERM), answering as opencode 2.0.22
  # does: its server died first, so the open prompt gets a JSON-RPC error (-32603 ServerUnavailable), not cancelled.
  # Falsification: delete only on incomplete/cancelled (the old guard) -> no .del log.
  ACP_REPLAY="$SB/oc-hang.jsonl" ACP_MODE=termerror ACP_REC="$SB/a175.rec" ACP_PIDFILE="$SB/a175.pid" OC_SESSION_LOG="$SB/a175.del" stub_run start --effort low --cwd "$SB/gdir" --out "$SB/a175" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do grep -q '"kind": "turn_start"' "$SB/a175/events.jsonl" 2>/dev/null && grep -q session/prompt "$SB/a175.rec" 2>/dev/null && break; sleep 0.1; done
  kill -TERM "$(head -1 "$SB/a175.pid")" 2>/dev/null; stub_run wait "$SB/a175" --for exit --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/a175" >/dev/null 2>&1
  t 175 "an abandoned opencode turn whose agent is TERMed mid-turn and answers with an ERROR (server unavailable, as 2.0.22 does live) still has its session deleted: ONCE, in the seat's cwd" \
    sh -c "[ \"\$(grep -c 'session delete' '$SB/a175.del')\" = 1 ] && grep -q 'ses_f2c74432dffeoPDAL0Eq6RHjaI' '$SB/a175.del' && grep -q '$GD' '$SB/a175.del'"
  t 173 "AN ABANDONED OPENCODE TURN'S SESSION IS DELETED: a stop mid-turn closes the agent politely (stdin EOF first) and runs session delete <sid> --standalone ONCE, in the seat's cwd, before the turn's envelope (incomplete, rc 143) lands; an idle stop, a turn the agent cancelled and lived on, a grok seat and the deadline's close delete nothing; a later send ends typed (error handshake, rc 1) within 30 s" \
    sh -c "[ \"\$(head -1 '$SB/a173.rec.ends')\" = eof ] && python3 '$SB/chk.py' forgot '$SB/a173' '$SB/a173.del' ses_f2c74432dffeoPDAL0Eq6RHjaI '$GD' '$SB/a173.sent' $R173 && [ $T173 -lt 30 ] && [ ! -e '$SB/a101.del' ] && [ ! -e '$SB/a110.del' ] && [ ! -e '$SB/a111b.del' ]"
  ACP_MODE=slow ACP_PIDFILE="$SB/a112.pids" ACP_REC="$SB/a112.rec" stub_run start --effort low --cwd "$SB/tmp" --out "$SB/a112" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1
  stub_run send "$SB/a112" "$SB/q81.md" >"$SB/a112.sent" 2>/dev/null; stub_run wait "$SB/a112" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/a112" >/dev/null 2>&1
  t 112 "a send WHILE an ACP turn runs queues (ACP v1 has no mid-turn input): acked sent, expect 2, delivery queue at once; its marker lands in events.jsonl before turn 1 ends; turn 2 opens only after turn 1's turn_end; both ok on ONE agent" \
    python3 "$SB/chk.py" queued "$SB/a112" "$SB/a112.sent"
  # CODEX SEATS: one `codex app-server` per launch (JSON-RPC on stdio, acp.Rpc): thread/start, or thread/resume on the
  # recorded thread at a relaunch; every line is a turn/start the app-server steers into the open turn or opens the next
  # with. test-stub.py answers as the app-server (CX_* seams); fixtures/codex-appserver-sample.jsonl is a real wire,
  # sanitized (fixtures/README.md).
  mkdir -p "$SB/cxw"; CXW="$(cd "$SB/cxw" && pwd -P)"
  CX_ARGV_LOG="$SB/c113.argv" CX_STDIN_LOG="$SB/c113.stdin" CX_CWD_LOG="$SB/c113.cwd" CX_STUB_CALLS="$SB/c113.calls" \
    stub_run start --effort high --cwd "$SB/cxw" --out "$SB/c113" codex:gpt-5.6-terra "$SB/q.md" >"$SB/c113.ack" 2>/dev/null
  stub_run wait "$SB/c113" --timeout 30 >/dev/null 2>&1; sleep 1.5
  ps -A -ww -o command= | grep -q "seat.py spawn $SBP/c113 " && echo alive >"$SB/c113.idle"
  stub_run send "$SB/c113" "$SB/q81.md" >"$SB/c113.ack2" 2>/dev/null
  stub_run wait "$SB/c113" --timeout 30 >/dev/null 2>&1
  stub_run stop "$SB/c113" >/dev/null 2>&1; echo $? >"$SB/c113.stoprc"
  t 113 "CODEX SEAT: start acks (spawned, expect 1, steer); ONE app-server (--no-daemon, memories and hooks off, live web search) serves both turns: initialize, initialized, thread/start (the canonical cwd, sandbox danger-full-access, approval never, the model, a persisted thread), then a turn/start per turn (its text, the effort); the wrapper stays, so a send is taken live (sent, expect 2); both turns answer on the one thread; usage = the turn's model calls summed, in excluding the cached input; stop closes the idle seat politely: its stdin's EOF, no interrupt (rc 0, no wrapper left)" \
    sh -c "python3 '$SB/chk.py' codexseat '$SB/c113' '$CXW' '$SB/q81.md' && [ \"\$(cat '$SB/c113.stoprc')\" = 0 ] && python3 '$SB/chk.py' gone '$SB/c113'"
  # 114/158: the recorded wire's turns, one per opening turn/start -- three on one app-server,
  # the fourth after a stop, on the relaunch's resumed thread
  ( export CX_REPLAY="$HERE/fixtures/codex-appserver-sample.jsonl" CX_STUB_CALLS="$SB/c114.calls"
    stub_run start --effort low --cwd "$SB/cxw" --out "$SB/c114" codex:gpt-6-luna "$SB/q.md" >/dev/null 2>&1
    stub_run wait "$SB/c114" --timeout 30 >/dev/null 2>&1
    for i in 2 3 4; do [ $i = 4 ] && stub_run stop "$SB/c114" >/dev/null 2>&1
      stub_run send "$SB/c114" "$SB/q81.md" >/dev/null 2>&1; stub_run wait "$SB/c114" --timeout 30 >/dev/null 2>&1; done
    stub_run stop "$SB/c114" >/dev/null 2>&1 )
  t 114 "codex events from a recorded app-server wire: the ten-kind vocabulary (each turn's calls opened in it; their commands finished only after it, so no result is in-turn); turn 1 (steered mid-turn) answers its final message only; an interrupted turn is cancelled; items and usage that arrive after their turn completed are strays, never the next turn's; a relaunch's turn rides the resumed thread" \
    sh -c "python3 '$SB/chk.py' vocab '$SB/c114' text,tool_call,usage,turn_end ok,cancelled,ok,ok STEERED && python3 '$SB/chk.py' codexreplay '$SB/c114'"
  t 158 "codex usage is PER TURN: each envelope and usage event carries the sum of its turn's model calls (\`last\`), never the thread's running total -- 4995/36/29568, 5441/39/17152, 312/6/22400, 3411/7/22528 (in/out/cache_read)" \
    python3 "$SB/chk.py" codexusage "$SB/c114"
  CX_STUB_MODE=files stub_run --cwd "$SB/cxw" --out "$SB/c115" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 115 "codex fileChange items become file events (one per change path, paired to its call); a failed command is an error tool_result; the turn still answers ok with its final message" \
    python3 "$SB/chk.py" codexfiles "$SB/c115"
  CXF="$(cat "$HERE/fixtures/codex-usage-limit.txt")"
  for m in quota noticedeath notice quoted quotedfail; do
    CX_STUB_MODE=$m CX_FAIL_TEXT="$CXF" stub_run --cwd "$SB/cxw" --out "$SB/c116-$m" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1; done
  CX_STUB_MODE=refuse CX_FAIL_TEXT='invalid params: effort' stub_run --cwd "$SB/cxw" --out "$SB/c116-refuse" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 116 "codex failure typing reads error FIELDS only: a turn failed with the usage-limit text is provider-quota-dead with its reset; the same text as an error notice before the app-server dies types it too; a reconnect notice before an answer stays ok; an answer QUOTING the text stays ok; a failed turn whose prose quotes it is a plain error; a turn/start the app-server refuses is that turn, a typed error naming the refusal" \
    sh -c "jq -e '.status==\"provider-quota-dead\" and (.reset|test(\"Aug\"))' '$SB/c116-quota/envelope.json' >/dev/null && jq -e '.status==\"provider-quota-dead\"' '$SB/c116-noticedeath/envelope.json' >/dev/null && jq -e '.status==\"ok\"' '$SB/c116-notice/envelope.json' >/dev/null && jq -e '.status==\"ok\"' '$SB/c116-quoted/envelope.json' >/dev/null && jq -e '.status==\"error\"' '$SB/c116-quotedfail/envelope.json' >/dev/null && jq -e '.status==\"error\" and .turn==1' '$SB/c116-refuse/envelope.json' >/dev/null && grep -q 'turn/start refused: -32602 invalid params: effort' '$SB/c116-refuse/events.jsonl'"
  CX_STUB_MODE=slow CX_STDIN_LOG="$SB/c117.stdin" CX_STUB_CALLS="$SB/c117.calls" stub_run start --cwd "$SB/cxw" --out "$SB/c117" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  stub_run send "$SB/c117" "$SB/q81.md" >"$SB/c117.sent" 2>/dev/null; stub_run wait "$SB/c117" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/c117" >/dev/null 2>&1
  t 117 "a send WHILE a codex turn runs is STEERED into it: acked sent, expect 1, delivery steer; it reaches the one app-server as a turn/start the open turn takes, and turn 1 answers both inputs -- no second turn, nothing cancelled" \
    python3 "$SB/chk.py" steered "$SB/c117" "$SB/q81.md"
  CX_STUB_MODE=stale CX_STDIN_LOG="$SB/c181.stdin" stub_run start --cwd "$SB/cxw" --out "$SB/c181" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  stub_run send "$SB/c181" "$SB/q81.md" >"$SB/c181.sent" 2>/dev/null; stub_run wait "$SB/c181" --timeout 30 >"$SB/c181.wait" 2>/dev/null; stub_run stop "$SB/c181" >/dev/null 2>&1
  t 181 "a send its turn OUTRAN (acked expect 1 while turn 1 ran, but turn 1 ended before the app-server took it) opens turn 2, and wait follows the input there: it returns turn 2's answer, never turn 1's; the command turn 1 left running completes during turn 2, and it and its usage are strays (turn 2 carries only its own tokens)" \
    python3 "$SB/chk.py" staled "$SB/c181" "$SB/q81.md" "$SB/c181.wait"
  CX_STUB_MODE=park CX_STUB_PIDFILE="$SB/c118.pid" CX_STUB_CALLS="$SB/c118.calls" CX_STDIN_LOG="$SB/c118.stdin" stub_run start --cwd "$SB/cxw" --out "$SB/c118" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/c118.pid" ] && break; sleep 0.1; done
  stub_run send "$SB/c118" "$SB/q81.md" >/dev/null 2>&1; stub_run stop "$SB/c118" >/dev/null 2>&1; echo $? >"$SB/c118.rc"
  t 118 "STOP closes a codex seat mid-turn as its deadline does -- a turn/interrupt naming the open turn, then its stdin's EOF --: the app-server and the command it runs in a session of its own die, no wrapper remains, and turn 1 (the send steered into it) is incomplete (rc 143) on its thread, carrying the usage its first call reported in the envelope's fields (in uncached), never codex's raw ones" \
    sh -c "[ \"\$(cat '$SB/c118.rc')\" = 0 ] && python3 '$SB/chk.py' gone '$SB/c118' \$(cat '$SB/c118.pid') && [ \"\$(cat '$SB/c118.calls')\" = 1 ] && jq -e '.status==\"incomplete\" and .rc==143 and .session==\"th_stub123\" and .usage=={\"in\":200,\"out\":100,\"cache_read\":800,\"reasoning\":40}' '$SB/c118/turn-1/envelope.json' >/dev/null && [ ! -e '$SB/c118/turn-2' ] && python3 '$SB/chk.py' interrupted '$SB/c118' '$SB/c118.pid' incomplete 143"
  # 178-180: codex 0.160 answers a TERM by exiting 0, so a wrapper that read
  # its halt off the CLI's rc ran on; a wrapper SIGKILLed before its close-out left markers past its turns
  CX_STUB_MODE=parkexit CX_STUB_PIDFILE="$SB/c178.pid" CX_STUB_CALLS="$SB/c178.calls" stub_run start --cwd "$SB/cxw" --out "$SB/c178" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/c178.pid" ] && break; sleep 0.1; done
  stub_run send "$SB/c178" "$SB/q81.md" >"$SB/c178.sent" 2>/dev/null; stub_run wait "$SB/c178" --timeout 1 >"$SB/c178.run" 2>/dev/null
  stub_run stop "$SB/c178" >/dev/null 2>&1; echo $? >"$SB/c178.rc"
  t 178 "STOP HALTS A SEAT WHOSE CLI EXITS 0 ON A TERM (codex 0.160): no wrapper or command remains, the CLI ran once, and turn 1 closes incomplete (rc 143), never ok" \
    sh -c "[ \"\$(cat '$SB/c178.rc')\" = 0 ] && python3 '$SB/chk.py' gone '$SB/c178' \$(cat '$SB/c178.pid') && [ \"\$(cat '$SB/c178.calls')\" = 1 ] && jq -e '.status==\"incomplete\" and .rc==143' '$SB/c178/turn-1/envelope.json' >/dev/null"
  t 179 "ONE JSON FORMAT for every verb line: the sent ack and wait's running line print compact, as the envelopes do (a driver's one parser reads every state)" \
    sh -c "jq -e '.status==\"sent\"' '$SB/c178.sent' >/dev/null && jq -e '.status==\"running\" and .turn==1' '$SB/c178.run' >/dev/null && ! grep -q '\": ' '$SB/c178.sent' '$SB/c178.run'"
  CX_STUB_MODE=park CX_STUB_PIDFILE="$SB/c180.pid" stub_run start --cwd "$SB/cxw" --out "$SB/c180" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/c180.pid" ] && break; sleep 0.1; done
  stub_run send "$SB/c180" "$SB/q81.md" >/dev/null 2>&1; stub_run send "$SB/c180" "$SB/q81.md" >/dev/null 2>&1
  W180="$(ps -A -ww -o pid=,command= | grep "seat.py spawn $SBP/c180 " | grep -v grep | awk '{print $1}')"
  [ -n "$W180" ] && kill -KILL -- "-$W180" 2>/dev/null; kill -KILL $(cat "$SB/c180.pid") 2>/dev/null; sleep 0.3
  stub_run send "$SB/c180" "$SB/q81.md" >"$SB/c180.a1" 2>/dev/null; stub_run wait "$SB/c180" --timeout 20 >/dev/null 2>&1
  stub_run send "$SB/c180" "$SB/q81.md" >"$SB/c180.a2" 2>/dev/null; stub_run wait "$SB/c180" --timeout 20 >/dev/null 2>&1; echo $? >"$SB/c180.wrc"
  stub_run stop "$SB/c180" >/dev/null 2>&1
  t 180 "A RELAUNCH AFTER A WRAPPER DIED WITHOUT ITS CLOSE-OUT (SIGKILL mid-turn, two sends steered into it) closes turn 1 incomplete first, then numbers past it: the relaunch acks spawned expect 2, the next send sent expect 3, and wait returns turn 3's answer" \
    sh -c "jq -e '.status==\"spawned\" and .expect==2' '$SB/c180.a1' >/dev/null && jq -e '.status==\"sent\" and .expect==3' '$SB/c180.a2' >/dev/null && [ \"\$(cat '$SB/c180.wrc')\" = 0 ] && jq -e '.status==\"incomplete\"' '$SB/c180/turn-1/envelope.json' >/dev/null && jq -e '.status==\"ok\" and .turn==3' '$SB/c180/envelope.json' >/dev/null && [ ! -e '$SB/c180/turn-4' ]"
  CX_STUB_MODE=park CX_STUB_PIDFILE="$SB/c123.pid" CX_STDIN_LOG="$SB/c123.stdin" stub_run --effort high --timeout 3 --cwd "$SB/cxw" --out "$SB/c123" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 182 "a codex seat's DEADLINE closes it politely: a turn/interrupt naming the open turn, then its stdin closed (the app-server reaps the command it ran); turn 1 is timeout (rc 142) on its thread and no process is left" \
    python3 "$SB/chk.py" interrupted "$SB/c123" "$SB/c123.pid" timeout 142
  CX_ARGV_LOG="$SB/c123.argv" CX_STDIN_LOG="$SB/c123.stdin" stub_run send "$SB/c123" "$SB/q81.md" >"$SB/c123.ack" 2>/dev/null
  stub_run wait "$SB/c123" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/c123" >/dev/null 2>&1
  CX_STUB_MODE=badresume stub_run send "$SB/c123" "$SB/q81.md" >"$SB/c123.bad" 2>/dev/null; echo $? >"$SB/c123.badrc"
  t 123 "a TIMED-OUT codex seat is a resumable checkpoint: a send relaunches it (spawned, expect 2) as thread/resume on the recorded thread with the same cwd, sandbox and model, and turn 2 answers ok on it with the same effort; a resume the app-server refuses is a typed launch failure (error handshake, its message, send rc 1), never a fresh thread" \
    sh -c "python3 '$SB/chk.py' cxresumed '$SB/c123' '$SB/c123.ack' '$CXW' \"\$(cat '$SB/c123.badrc')\" '$SB/c123.bad'"
  # 183-187: the cases of OpenAI's codex-plugin-cc fake app-server: codex's collaborator agents
  # report their turns on THEIR threads over the seat's connection; a malformed stdout line; a login the turn fails on
  for m in subagent latesub; do CX_STUB_MODE=$m stub_run --cwd "$SB/cxw" --out "$SB/c183-$m" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1; done
  t 183 "A COLLABORATOR'S THREAD IS NOT THE SEAT'S: codex spawns one (collabAgentToolCall) whose thread starts a turn, thinks, runs a command, answers and completes over the seat's connection before the main answer -- the seat has ONE turn, ok, answering the main final message; none of the collaborator's text, thought or call is the seat's and its turn/completed closes nothing, but its tokens are the turn's spend (summed over the fields of either report) and each of its items and turns is a progress line, so the seat never looks dead while it works" \
    python3 "$SB/chk.py" collab "$SB/c183-subagent"
  t 184 "...the main answer arriving BEFORE the collaborator finishes still answers the turn: the collaborator's later message is never the answer, and its call's completion after the answer never voids it (a collabAgentToolCall is not a tool of the answer rule)" \
    python3 "$SB/chk.py" collab "$SB/c183-latesub"
  CX_STUB_MODE=nomain stub_run start --cwd "$SB/cxw" --out "$SB/c185" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/c185" --timeout 3 >"$SB/c185.run" 2>/dev/null; stub_run stop "$SB/c185" >/dev/null 2>&1
  t 185 "...and when the main turn/completed never arrives the turn is NOT inferred over (no timer, no tracking of what the collaborators still owe): it runs until a stop, which types it incomplete (rc 143) with the usage it reported, and its answer stays in events.jsonl" \
    sh -c "jq -e '.status==\"running\" and .turn==1' '$SB/c185.run' >/dev/null && jq -e '.status==\"incomplete\" and .rc==143 and .usage=={\"in\":506,\"out\":53,\"cache_read\":4,\"reasoning\":1}' '$SB/c185/turn-1/envelope.json' >/dev/null && jq -e -s 'map(select(.kind==\"text\" and .turn==1 and .text==\"FINAL: the main answer\")) | length==1' '$SB/c185/events.jsonl' >/dev/null && [ ! -e '$SB/c185/turn-2' ]"
  CX_STUB_MODE=unauthorized stub_run --cwd "$SB/cxw" --out "$SB/c186" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 186 "a codex turn failed with codexErrorInfo unauthorized is auth-expired (a re-login, never a reroute), though its message names no login" \
    sh -c "jq -e '.status==\"auth-expired\" and .turn==1' '$SB/c186/envelope.json' >/dev/null"
  CX_STUB_MODE=badline PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" perl -e 'alarm 30; exec @ARGV' "$0" --cwd "$SB/cxw" --out "$SB/c187" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 187 "a MALFORMED app-server line mid-turn is kept raw in stream.jsonl and skipped: the turn still answers ok (bounded at 30 s: a reader that died on it would leave the turn hanging)" \
    sh -c "jq -e '.status==\"ok\"' '$SB/c187/envelope.json' >/dev/null && [ \"\$(cat '$SB/c187/result.txt')\" = 'stub ok' ] && jq -e -s 'map(select(.dir==\"in\" and (.msg|type)==\"string\")) | length==1' '$SB/c187/stream.jsonl' >/dev/null"
  t 146 "PRIVATE SEATS: every launch and relaunch argv opts out of the CLI's shared server -- grok agent --no-leader (no --leader/--leader-socket, no leader/serve/headless), codex --no-daemon app-server on stdio (no --remote, --remote-auth-token-env, --listen, daemon, proxy, remote-control), opencode a bare acp (its own child server; no attach flag exists)" \
    python3 "$SB/chk.py" private "$SB/a100.argv" "$SB/a107.argv" "$SB/c113.argv" "$SB/c123.argv" "$SB/a101.argv" "$SB/a122.argv"
  CL_ARGV_LOG="$SB/g49.argv" stub_run --out "$SB/g49" claude:sonnet "$SB/q.md" >/dev/null 2>&1 || true
  t 49 "claude seats launch with background tasks DISABLED (one-shot hardening, env pinned at the CLI) — and a clean run envelopes OK (jq's // treats a false is_error as ABSENT: reading it with a // default made every good claude seat an error)" \
    sh -c "grep -q 'bg=1' '$SB/g49/result.txt' && jq -e '.status==\"ok\"' '$SB/g49/envelope.json' >/dev/null"
  t 49b "claude buys STREAM both ways: --input-format + --output-format stream-json, --verbose (print mode requires it)" \
    sh -c "[ \"\$(awk 'p==\"--input-format\"{print;exit}{p=\$0}' '$SB/g49.argv')\" = 'stream-json' ] && [ \"\$(awk 'p==\"--output-format\"{print;exit}{p=\$0}' '$SB/g49.argv')\" = 'stream-json' ] && grep -qx -- '--verbose' '$SB/g49.argv' && jq -e '.session==\"sess-stub\"' '$SB/g49/envelope.json' >/dev/null"
  # 49e: LEAN SEATS — the ONE claude launch carries the measured lean flags and env; every relaunch sends the same.
  # Falsified per flag.
  t 49e "claude seats run LEAN: --strict-mcp-config + --exclude-dynamic-system-prompt-sections in argv, CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 in env" \
    sh -c "grep -qx -- --strict-mcp-config '$SB/g49.argv' && grep -qx -- --exclude-dynamic-system-prompt-sections '$SB/g49.argv' && grep -q 'mem=1' '$SB/g49/result.txt'"
  # 39w: the claude PROBE refreshes the quota board (its stream's rate_limit_event is the claude reading), so it pings the
  # smallest model in a neutral cwd, and its job dir lands in the jobs root, where the gauge reads.
  # Falsification: drop the haiku spec -> the CLI default (sonnet); drop the neutral cwd -> the caller's directory.
  TD="$(cd "$SB/tmp" && pwd -P)"
  ( cd "$SB/gdir" && CL_ARGV_LOG="$SB/cl39w.argv" CL_CWD_LOG="$SB/cl39w.cwd" PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_STATE_HOME="$SB/st39" XDG_CACHE_HOME="$SB/cache" "$SELF" --probe claude >"$SB/cl39w.out" 2>&1 ) || true
  t 39w "--probe claude pings HAIKU from a NEUTRAL cwd (TMPDIR), its stream in the jobs root's probe-*-claude (where the claude gauge reads)" \
    sh -c "awk 'p==\"--model\"{print;exit}{p=\$0}' '$SB/cl39w.argv' | grep -qx haiku && [ \"\$(cat '$SB/cl39w.cwd')\" = '$TD' ] && ls '$SB'/st39/hands/probe-*-claude/stream.jsonl >/dev/null && grep -q '\"status\":\"ok\",\"provider\":\"claude\"' '$SB/cl39w.out'"
  # 39q: the opencode PROBE pings the cheapest Go model at low, in a neutral cwd.
  # Falsification: drop its spec -> a model-less opencode buy is refused and nothing launches.
  ( cd "$SB/gdir" && ACP_ARGV_LOG="$SB/oc39q.argv" ACP_ENV_LOG="$SB/oc39q.env" PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_STATE_HOME="$SB/st39" XDG_CACHE_HOME="$SB/cache" "$SELF" --probe opencode >/dev/null 2>&1 ) || true
  t 39q "--probe opencode pings (a bare acp, the cheapest Go model at low: 2.x lists no working free one) from a NEUTRAL cwd (TMPDIR), never the caller's directory" \
    sh -c "[ \"\$(cat '$SB/oc39q.argv')\" = acp ] && jq -s -e 'any(.[]; .status==\"ok\" and .model==\"opencode-go/deepseek-v4.1-flash\" and .effort==\"low\")' '$SB'/st39/hands/probe-*-opencode/envelope.json >/dev/null && [ \"\$(sed -n 4p '$SB/oc39q.env')\" = '$TD' ]"
  CL_STUB_MODE=initonly stub_run --out "$SB/g49c" claude:sonnet "$SB/q.md" >/dev/null 2>&1 || true
  t 49c "a claude seat that died mid-turn STILL yields its session: the id is read from the stream's FIRST event, so an interrupted seat stays resumable (harvesting from the result event would find nothing here)" \
    sh -c "jq -e '.status != \"ok\" and .session==\"sess-stub\"' '$SB/g49c/envelope.json' >/dev/null && [ ! -s '$SB/g49c/result.txt' ]"

  # THE WIRE, NOT THE STUB: the claude parser over a REAL recorded stream (a fixture captured
  # live). A stub the author wrote to match their own parser proves only
  # self-consistency; this row (and the ACP and codex fixture rows) is where the wire shape is the input.
  CL_STUB_MODE=replay CL_STUB_REPLAY="$HERE/fixtures/claude-stream-sample.jsonl" \
    stub_run --out "$SB/wire1" claude:sonnet "$SB/q.md" >/dev/null 2>&1 || true
  t 49d "claude parser over a REAL recorded claude stream (fixtures/claude-stream-sample.jsonl): final message + session, straight off the wire" \
    sh -c "[ \"\$(cat '$SB/wire1/result.txt')\" = 'ok' ] && jq -e '.status==\"ok\" and .session==\"49c93750-efa1-4e50-9d05-6470e8eb5512\"' '$SB/wire1/envelope.json' >/dev/null"


  # RETRY + PERSISTENCE rows. Falsification: --ephemeral restored → 58.
  ACP_REPLAY="$SB/grok-402.jsonl" stub_run --effort low --out "$SB/pf55" grok:grok-4.6 "$SB/q.md" >/dev/null 2>&1
  t 55 "a usage-limit error answering a live ACP prompt (a JSON-RPC error carrying the provider's 402 text — a synthetic shape, the live one is unverified) classifies provider-quota-dead, with its session" \
    sh -c "jq -e '.status==\"provider-quota-dead\" and .session==\"01a0d38a-6416-74d3-82f8-80174c82f422\"' '$SB/pf55/envelope.json' >/dev/null"
  # 129: the live condition, a store with no login for the seat's provider: the agent answers the prompt with the error
  ACP_REPLAY="$SB/oc-nokey.jsonl" stub_run --effort low --out "$SB/pf129" opencode:opencode-go/deepseek-v4.1-flash "$SB/q.md" >/dev/null 2>&1
  t 129 "an opencode seat with NO usable login (its prompt answered by a JSON-RPC error carrying the live missing-login text, fixtures/opencode-missing-api-key.txt) is auth-expired, with its session: a re-login, never a reroute" \
    sh -c "jq -e '.status==\"auth-expired\" and .rc==1 and .session==\"ses_f2c74432dffeoPDAL0Eq6RHjaI\"' '$SB/pf129/envelope.json' >/dev/null"
  CX_STDIN_LOG="$SB/pf58.stdin" stub_run --out "$SB/pf58" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1 || true
  t 58 "codex seats PERSIST sessions: thread/start asks for a persisted thread (ephemeral false, the resume substrate)" \
    sh -c "grep '\"thread/start\"' '$SB/pf58.stdin' | jq -e '.params.ephemeral==false' >/dev/null"

  # the start spawn line self-teaches the output contract (drivers once read
  # absent results as instant-empty completion); codex, the last one-shot buy, is a seat now.
  PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" \
    "$0" start --cwd "$SB/tmp" --out "$SB/bg70" codex:gpt-5.6-terra "$SB/q.md" >"$SB/bg70.spawn" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -e "$SB/bg70/envelope.json" ] && break; sleep 0.5; done
  t 70 "a codex start is a SEAT like every other: its spawn line is an ACK (spawned, expect 1, delivery steer) that states the per-turn output contract, and turn 1 lands in turn-1/ ok" \
    sh -c "jq -e '.status==\"spawned\" and .expect==1 and .delivery==\"steer\" and (.note|test(\"turn-N\")) and (.note|test(\"never finished-empty\"))' '$SB/bg70.spawn' >/dev/null && jq -e '.status==\"ok\" and .turn==1' '$SB/bg70/envelope.json' >/dev/null && [ \"\$(cat '$SB/bg70/turn-1/result.txt')\" = 'stub ok' ]"

  # GROUP KILL + SESSION HARVEST rows (an orphan incident: a timed-out codex
  # seat survived its alarm for 2h+ because the npm shim, not the vendor binary, took
  # the SIGALRM — envelope said dead, seat kept building, driver rebought). 71: the
  # alarm must kill the seat's whole process GROUP; the stub models exactly the shim
  # indirection (work in a child the old exec-alarm never signalled; this one never
  # answers the handshake). 72: the envelope carries the native session id (182 proves
  # it on the TIMEOUT path). Falsification: group-kill reverted → 71 (grandchild
  # survives the alarm); harvest dropped → 72.
  mkdir -p "$SB/binshim"
  cat >"$SB/binshim/codex" <<'STUB'
#!/bin/sh
# npm-shim model: the real work happens in a CHILD the parent merely waits on
sh -c 'echo $$ > "$T71_PID"; exec sleep 30' &
cat >/dev/null
sleep 30
STUB
  chmod +x "$SB/binshim/codex"
  T71_PID="$SB/t71.pid" PATH="$SB/binshim:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" \
    "$0" --timeout 1 --out "$SB/t71" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  sleep 0.3   # let launchd reap the killed grandchild so kill -0 cannot see a zombie
  t 71 "timeout kills the seat PROCESS GROUP: a shim-spawned grandchild is dead when the envelope lands (the orphan class)" \
    sh -c "jq -e '.status==\"timeout\" and .rc==142' '$SB/t71/envelope.json' >/dev/null && [ -s '$SB/t71.pid' ] && ! kill -0 \$(cat '$SB/t71.pid') 2>/dev/null"
  t 72 "envelope carries the native session id on an OK codex buy (the thread thread/start answered)" \
    sh -c "jq -e '.session==\"th_stub123\"' '$SB/pf58/envelope.json' >/dev/null"

  # 76: the reaper runs on NATURAL exit too — a child the seat backgrounded and forgot
  # dies with the seat. Falsification: drop the post-waitpid reap() → the sleeper survives.
  : >"$SB/g76.pid"
  CL_STUB_MODE=straggler CL_STUB_PIDFILE="$SB/g76.pid" stub_run --out "$SB/g76" claude:sonnet "$SB/q.md" >/dev/null 2>&1 || true
  t 76 "a process the seat backgrounded and forgot dies WITH the seat: the wrapper reaps the seat pgroup on every exit, not only on alarm (a log stream once outlived its seat by hours)" \
    sh -c "[ -s '$SB/g76.pid' ] && jq -e '.status==\"ok\"' '$SB/g76/envelope.json' >/dev/null && for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 \$(cat '$SB/g76.pid') 2>/dev/null || exit 0; sleep 0.3; done; exit 1"
  kill -9 "$(cat "$SB/g76.pid" 2>/dev/null)" 2>/dev/null || true
  # 77: a CLI killed from outside (not by stop): the wrapper survives to reap the pgroup, parse the
  # stream, and write a typed envelope that carries the session.
  : >"$SB/g77.pid"
  ( CL_STUB_MODE=park CL_STUB_PIDFILE="$SB/g77.pid" stub_run --out "$SB/g77" claude:sonnet "$SB/q.md" >/dev/null 2>&1 ) &
  T77=$!
  for i in $(seq 1 60); do [ -s "$SB/g77.pid" ] && break; sleep 0.1; done
  kill -TERM "$(cat "$SB/g77.pid" 2>/dev/null)" 2>/dev/null
  wait $T77 2>/dev/null
  t 77 "A CLI KILLED FROM OUTSIDE (TERM to the seat process, not a stop) still yields a typed envelope WITH the session — the wrapper outlives the seat and finishes its parse (the reaping itself is row 76/76b)" \
    sh -c "jq -e '.status != \"ok\" and .rc==143 and .session==\"sess-stub\"' '$SB/g77/envelope.json' >/dev/null && ! kill -0 \$(cat '$SB/g77.pid') 2>/dev/null"
  # 74: the private pgroup must not orphan on ^C — INT forwards to the group (parity
  # with the old exec form, where the foreground group carried the CLI and its children;
  # the pgroup isolation would otherwise recreate the orphan class on a different
  # signal). The row signals the perl wrapper DIRECTLY, as a terminal ^C would.
  # Falsification: the forwarding handlers removed → rc still 130 but the grandchild
  # SURVIVES the INT — the second assert is the discriminating one.
  # 74 signals the wrapper with the INT disposition a FOREGROUND shell gives it (the perl
  # trampoline models the terminal: bash sets INT to SIG_IGN for its own `&` children, so
  # without it this row would test the background case instead). The wrapper is located as
  # the seat's PARENT, which is exact regardless of how many shells sit above it.
  # Falsification: the forwarding handlers removed -> rc still 130 but the grandchild
  # SURVIVES the INT — the second assert is the discriminating one.
  : > "$SB/t74.pid"
  ( perl -e '$SIG{INT}="DEFAULT"; exec @ARGV' bash -c "$(declare -f wt_alarm); wt_alarm 30 sh -c 'echo \$\$ > \"$SB/t74.pid\"; exec sleep 30'" >/dev/null 2>&1; echo $? > "$SB/t74.rc" ) &
  T74SUB=$!
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$SB/t74.pid" ] && break; sleep 0.1; done
  T74PERL="$(ps -o ppid= -p "$(cat "$SB/t74.pid")" 2>/dev/null | tr -d ' ')"
  [ -n "$T74PERL" ] && kill -INT "$T74PERL" 2>/dev/null
  wait $T74SUB 2>/dev/null
  sleep 0.3
  t 74 "INT forwards to the seat pgroup: ^C on a sync buy still kills a shim-spawned grandchild (foreground-group parity)" \
    sh -c "[ \"\$(cat '$SB/t74.rc' 2>/dev/null)\" = 130 ] && [ -s '$SB/t74.pid' ] && ! kill -0 \$(cat '$SB/t74.pid') 2>/dev/null"

  # 75 is 74's MIRROR and the nohup contract: when the caller has ALREADY ignored the
  # signal — `nohup` on a background buy — the wrapper must not
  # install a handler over that SIG_IGN. Those guards exist so a seat outlives a terminal
  # teardown, and the old exec form honoured them by inheritance; catching the signal
  # anyway would make every backgrounded buy killable by a stray HUP. The seat must still
  # be alive after the ignored INT and die only at its own alarm (rc 142).
  # Falsification: drop the SigAction check -> the seat dies on the INT and rc is 130.
  : > "$SB/t75.pid"; : > "$SB/t75.rc"
  ( perl -e '$SIG{INT}="IGNORE"; exec @ARGV' bash -c "$(declare -f wt_alarm); wt_alarm 4 sh -c 'echo \$\$ > \"$SB/t75.pid\"; exec sleep 30'" >/dev/null 2>&1; echo $? > "$SB/t75.rc" ) &
  T75SUB=$!
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$SB/t75.pid" ] && break; sleep 0.1; done
  T75PERL="$(ps -o ppid= -p "$(cat "$SB/t75.pid")" 2>/dev/null | tr -d ' ')"
  [ -n "$T75PERL" ] && kill -INT "$T75PERL" 2>/dev/null
  sleep 0.5
  T75ALIVE=no; kill -0 "$(cat "$SB/t75.pid")" 2>/dev/null && T75ALIVE=yes
  wait $T75SUB 2>/dev/null
  t 75 "an INT the caller ALREADY ignored (nohup) never reaches the seat: no handler is installed over SIG_IGN, so the seat dies only at its own alarm" \
    sh -c "[ '$T75ALIVE' = yes ] && [ \"\$(cat '$SB/t75.rc' 2>/dev/null)\" = 142 ]"

  # 78: THE SEAT BOX is engine-injected, not driver-remembered: the seat
  # contract heads every buy's prompt, and the file the CLI receives IS prompt.txt.
  { cat "$SB/box.txt"; echo; echo task; echo; seat_dir "$(cd "$SB/a100" && pwd -P)"; } >"$SB/a100.prompt"   # the job dir, canonical
  t 78 "SEAT BOX: a buy's prompt.txt = seat_box verbatim + a blank line + the task + a blank line + the seat's job-dir line, and THAT file is what the agent receives (the ACP seat's first session/prompt text, byte for byte)" \
    sh -c "[ \"\$(sed -n '1p' '$SB/a100/prompt.txt' | cut -c1-9)\" = 'SEAT BOX.' ] && cmp -s '$SB/a100.prompt' '$SB/a100/prompt.txt' && python3 '$SB/chk.py' firstprompt '$SB/a100'"
  t 145 "SEAT BOX FIRST, PER-SEAT LINE LAST: two seats given one task (grok and opencode, other dirs and cwds) have prompt.txt files that cmp equal through the task end, that shared prefix IS box + blank line + task (no per-seat byte before the task ends), and they differ after it, in the job-dir line" \
    sh -c "{ cat '$SB/box.txt'; echo; cat '$SB/q.md'; } >'$SB/t145' && cmp -s -n \$(wc -c <'$SB/t145') '$SB/a100/prompt.txt' '$SB/a101/prompt.txt' && head -c \$(wc -c <'$SB/t145') '$SB/a100/prompt.txt' | cmp -s - '$SB/t145' && ! cmp -s '$SB/a100/prompt.txt' '$SB/a101/prompt.txt'"

  # PERSISTENT SEAT rows (hands v2): a claude seat is one process across
  # turns (seat.py). The multi-line checks live in chk.py (written with the stubs above); the push rows
  # stand up a throwaway Unix socket that records what the wrapper posts.
  CL_STUB_MODE=count stub_run start --effort high --cwd "$SB/tmp" --out "$SB/p81" claude:sonnet "$SB/q.md" >"$SB/p81.ack" 2>/dev/null
  stub_run wait "$SB/p81" >/dev/null 2>&1
  stub_run send "$SB/p81" "$SB/q81.md" >/dev/null 2>&1
  stub_run wait "$SB/p81" >/dev/null 2>&1
  t 81 "PERSISTENT SEAT: start acks at once (spawned, expect 1, steer); turn 2 runs on the SAME process (one pid, its turn counter at 2); each turn lands whole in turn-N/; result.txt follows the latest ok turn; envelopes carry turn + delivery" \
    python3 "$SB/chk.py" twoturns "$SB/p81"
  stub_run stop "$SB/p81" >/dev/null 2>&1
  CL_STUB_MODE=count CL_ARGV_LOG="$SB/p82.argv" CL_CWD_LOG="$SB/p82.cwd" stub_run send "$SB/p81" "$SB/q81.md" >"$SB/p82.ack" 2>/dev/null
  stub_run wait "$SB/p81" >/dev/null 2>&1; stub_run stop "$SB/p81" >/dev/null 2>&1
  t 82 "send to a DEAD seat resumes the recorded session on the SAME model, effort and permissions in the SAME canonical cwd (claude --resume): a new process answers turn 3" \
    sh -c "awk 'p==\"--resume\"{print;exit}{p=\$0}' '$SB/p82.argv' | grep -qx sess-stub && awk 'p==\"--model\"{print;exit}{p=\$0}' '$SB/p82.argv' | grep -qx sonnet && awk 'p==\"--effort\"{print;exit}{p=\$0}' '$SB/p82.argv' | grep -qx high && grep -qx bypassPermissions '$SB/p82.argv' && [ \"\$(cat '$SB/p82.cwd')\" = \"\$(cd '$SB/tmp' && pwd -P)\" ] && grep -q '^turn 1 pid' '$SB/p81/turn-3/result.txt' && jq -e '.turn==3 and .status==\"ok\" and .session==\"sess-stub\"' '$SB/p81/envelope.json' >/dev/null && jq -e '.status==\"spawned\" and .expect==3' '$SB/p82.ack' >/dev/null"
  t 154 "CLAUDE SEATS REFUSE CROSS-SESSION MESSAGES: every claude launch, a start and a relaunch, carries exactly ONE --settings, {\"crossSessionInbound\":\"refuse\",\"disableAllHooks\":true} -- driver mail goes through hands' inbox, never the host socket" \
    python3 "$SB/chk.py" settings "$SB/g49.argv" "$SB/p82.argv"
  CL_STUB_MODE=count stub_run start --cwd "$SB/tmp" --out "$SB/p98" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/p98" >/dev/null 2>&1; stub_run stop "$SB/p98" >/dev/null 2>&1
  CL_STUB_MODE=newsid stub_run send "$SB/p98" "$SB/q81.md" >/dev/null 2>&1
  stub_run wait "$SB/p98" >/dev/null 2>&1; stub_run stop "$SB/p98" >/dev/null 2>&1
  t 98 "SESSION CONSTANCY: the handle keeps the FIRST session; a turn on another conversation (a resume that silently started fresh) is error, reason session-changed, and its envelope names the original session" \
    sh -c "jq -e '.turn==2 and .status==\"error\" and .reason==\"session-changed\" and .session==\"sess-stub\" and .rc==1' '$SB/p98/envelope.json' >/dev/null && jq -e '.session==\"sess-stub\"' '$SB/p98/session' >/dev/null"
  CL_STUB_MODE=replay CL_STUB_REPLAY="$HERE/fixtures/claude-stdin-sample.jsonl" stub_run start --cwd "$SB/tmp" --out "$SB/p84" claude:claude-haiku-4-5-20251001 "$SB/q.md" >/dev/null 2>&1
  for d in p84; do for i in 1 2; do stub_run wait "$SB/$d" >/dev/null 2>&1; stub_run send "$SB/$d" "$SB/q81.md" >/dev/null 2>&1; done
    stub_run wait "$SB/$d" >"$SB/$d.wait" 2>/dev/null; stub_run stop "$SB/$d" >/dev/null 2>&1; done
  t 84 "EVENTS, claude stdin wire (a recorded 3-turn haiku seat with tool calls): every line {t,kind,turn} from the ten kinds, tool_result pairs its tool_call by call id, usage precedes each turn_end, three ok turn_ends with session + final, result.txt = the last turn_end text" \
    python3 "$SB/chk.py" vocab "$SB/p84" turn_start,text,thought,tool_call,tool_result,usage,turn_end,progress ok,ok,ok ZEBRA-42
  t 155 "TRIMMED, PRICED STDOUT USAGE: the envelope line wait prints carries usage {in, cache_read, cache_write, out, cost_usd} only (at most 100 bytes; the rest of the line is envelope.json's), envelope.json keeps reasoning too, and claude's turn cost is the step of its process-cumulative total_cost_usd (recorded seat: 0.059393, 0.0156844, 0.0041071)" \
    python3 "$SB/chk.py" trimmed "$SB/p84.wait" "$SB/p84"
  for m in quoted quotedfail quota authfail; do CL_STUB_MODE=$m stub_run --cwd "$SB/tmp" --out "$SB/p88$m" claude:sonnet "$SB/q.md" >/dev/null 2>&1; done
  t 88 "failure typing reads provider fields + stderr only: an answer QUOTING a usage-limit line stays ok, and a failing turn whose prose quotes one is a plain error (the quoted-text class); an is_error result is provider-quota-dead with its reset; a stderr login failure is auth-expired" \
    sh -c "jq -e '.status==\"ok\"' '$SB/p88quoted/envelope.json' >/dev/null && jq -e '.status==\"error\" and .rc==1' '$SB/p88quotedfail/envelope.json' >/dev/null && jq -e '.status==\"provider-quota-dead\" and (.reset|test(\"resets 6:20am\"))' '$SB/p88quota/envelope.json' >/dev/null && jq -e '.status==\"auth-expired\"' '$SB/p88authfail/envelope.json' >/dev/null"
  CL_ARGV_LOG="$SB/p120.argv" stub_run send "$SB/p88authfail" "$SB/q81.md" >"$SB/p120.out" 2>"$SB/p120.err"; R120A=$?
  stub_run send "$SB/o5" "$SB/q81.md" >/dev/null 2>&1; R120B=$?
  stub_run send "$SB/p81" "$SB/nope.md" >/dev/null 2>&1; R120C=$?
  stub_run send "$SB/p81" >/dev/null 2>&1; R120D=$?
  t 120 "send refusals are typed before any spend: a seat that recorded no session (it never reached the model) is refused no-session (rc 2, the refused envelope printed, no CLI launch); a dir with no handle, a missing FILE and a missing argument exit 2" \
    sh -c "[ $R120A -eq 2 ] && [ ! -e '$SB/p120.argv' ] && jq -e '.status==\"refused\" and .reason==\"no-session\" and .rc==2' '$SB/p120.out' >/dev/null && jq -e '.reason==\"no-session\"' '$SB/p88authfail/envelope.json' >/dev/null && grep -q 'recorded no session' '$SB/p120.err' && [ $R120B -eq 2 ] && [ $R120C -eq 2 ] && [ $R120D -eq 2 ]"
  cat >"$SB/sockserv.py" <<'PY'
import os, socket, sys, threading, time
s = socket.socket(socket.AF_UNIX)   # the driver's inbox: takes the auth + user lines (logged), then answers argv[3]'s
s.bind(sys.argv[1])                 # bytes if any -- HOLD = the host's recorded way: no reply, the connection left open
s.listen(8)
def serve(c, data=b''):
    while data.count(b'\n') < 2:
        b = c.recv(65536)
        if not b:
            break
        data += b
    open(sys.argv[2], 'ab').write(data + b'--\n')
    r = open(sys.argv[3], 'rb').read() if os.path.exists(sys.argv[3]) else b''
    try:
        time.sleep(3) if r == b'HOLD\n' else c.sendall(r)
    except OSError:
        pass   # the poster already closed: it reads no reply
    c.close()
while True:
    threading.Thread(target=serve, args=(s.accept()[0],), daemon=True).start()
PY
  python3 "$SB/sockserv.py" "$SB/d.sock" "$SB/push.log" "$SB/d.reply" >/dev/null 2>&1 & local SOCKPID=$!
  for i in $(seq 1 50); do [ -S "$SB/d.sock" ] && break; sleep 0.1; done
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub stub_run --cwd "$SB/tmp" --out "$SB/p89" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 30); do grep -q turn_end "$SB/push.log" 2>/dev/null && break; sleep 0.1; done
  t 89 "PUSH: a turn_end posts ONE connection to the captured driver socket — the auth line (the token), then one user-shaped line: 'claude:sonnet t1 ok: <answer clip>', the job dir, 'id <dir>:1', the data marker, the answer; the captured token is private (0600)" \
    python3 "$SB/chk.py" push "$SB/push.log" "$SB/p89" ok 1 'claude:sonnet t1 ok: bg=1 mem=1 turn=1' 'bg=1 mem=1 turn=1'
  : >"$SB/p89b.pid"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub CL_STUB_MODE=park CL_STUB_PIDFILE="$SB/p89b.pid" \
    stub_run start --cwd "$SB/tmp" --out "$SB/p89b" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/p89b.pid" ] && break; sleep 0.1; done
  echo '{"text":"hi from the seat"}' >>"$SB/p89b/outbox.jsonl"
  for i in $(seq 1 40); do grep -q ' mail ' "$SB/push.log" 2>/dev/null && break; sleep 0.1; done
  t 89b "MAIL PUSH: a line the seat appends to its outbox.jsonl posts one push 'claude:sonnet t1 mail to driver: <text>' (the dir, the id, the text) while the turn is still running" \
    python3 "$SB/chk.py" push "$SB/push.log" "$SB/p89b" 'mail to driver' 1 'claude:sonnet t1 mail to driver: hi from the seat' 'hi from the seat'
  # 130: JSONL splits on LF only -- a raw U+2028 inside a JSON string (events are written ensure_ascii=False)
  # is no line break: str.splitlines() cut the turn_end in two (wait never saw it) and one mail into two pushes
  printf 'echo a\342\200\250b\n' >"$SB/q130.md"
  CL_STUB_MODE=echo stub_run start --cwd "$SB/tmp" --out "$SB/p130" claude:sonnet "$SB/q130.md" >/dev/null 2>&1
  stub_run wait "$SB/p130" --timeout 10 >"$SB/p130.wait" 2>&1; R130=$?; stub_run stop "$SB/p130" >/dev/null 2>&1
  printf '{"to":"driver","text":"one\342\200\250line"}\n' >>"$SB/p89b/outbox.jsonl"
  for i in $(seq 1 40); do grep -q 'mail to driver: one line' "$SB/push.log" 2>/dev/null && break; sleep 0.1; done; sleep 0.3
  t 130 "LF-only JSONL: a turn_end whose text holds a raw U+2028 is seen (wait returns its ok envelope, rc 0, within 10 s), and one outbox line holding U+2028 posts exactly ONE mail push (never one per fragment)" \
    sh -c "[ $R130 -eq 0 ] && python3 '$SB/chk.py' lsep '$SB/p130' '$SB/p130.wait' && python3 '$SB/chk.py' mails '$SB/push.log' '$SB/p89b' 2 \"\$(printf 'one\342\200\250line')\""
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/nope.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub stub_run --cwd "$SB/tmp" --out "$SB/p90" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  t 90 "push is a silent no-op without the socket env (no driver file captured, no push event) and survives a dead socket (the buy stays ok)" \
    sh -c "[ ! -e '$SB/g49/driver' ] && ! grep -q '\"push\"' '$SB/g49/events.jsonl' && jq -e '.status==\"ok\"' '$SB/g49/envelope.json' >/dev/null && [ -s '$SB/p90/driver' ] && jq -e '.status==\"ok\"' '$SB/p90/envelope.json' >/dev/null"
  # 132: push records its outcome as a progress event -- never swallowed silently
  printf '{"type":"result","delivered":false,"reason":"held for approval"}\n' >"$SB/d.reply"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub stub_run --cwd "$SB/tmp" --out "$SB/p132" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  printf '{"type":"error","message":"bad token"}\n' >"$SB/d.reply"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub stub_run --cwd "$SB/tmp" --out "$SB/p132b" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  echo HOLD >"$SB/d.reply"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub stub_run --cwd "$SB/tmp" --out "$SB/p132c" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  rm -f "$SB/d.reply"
  awk 'BEGIN { while (i++ < 5000) printf "x"; print "" }' >"$SB/q134.md"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub CL_STUB_MODE=echo stub_run --cwd "$SB/tmp" --out "$SB/p134" claude:sonnet "$SB/q134.md" >/dev/null 2>&1
  t 134 "PUSH TEXT: line 1 is ONE short line 'provider:model tN status: <clip>' (all the host previews), line 2 the job dir, line 3 the id <dir>:<turn> -- which the outcome event carries too, so a retried push is recognizable -- then the seat's text under a data marker, a 5,000+ char answer capped at 4,000 + the clip mark" \
    python3 "$SB/chk.py" capped "$SB/push.log" "$SB/p134"
  t 132 "PUSH OUTCOME, aligned to the host (it sends no reply and leaves the connection open): each push records ONE progress event -- posted when the host took the bytes (p89), and no reply is read (a stub answering 'held' or an error still reads posted: p132, p132b), failed with the error on a dead socket (p90); a silent open connection never stalls the push (p132c: its event within 1 s of the turn_end)" \
    sh -c "python3 '$SB/chk.py' pushed '$SB/p89' turn_end posted '' && python3 '$SB/chk.py' pushed '$SB/p132' turn_end posted '' && python3 '$SB/chk.py' pushed '$SB/p132b' turn_end posted '' && python3 '$SB/chk.py' pushed '$SB/p90' turn_end failed 'No such file' && python3 '$SB/chk.py' pushed '$SB/p132c' turn_end posted '' && python3 '$SB/chk.py' unwaited '$SB/p132c'"
  # THE ENV SHIM (rows 153/168/169): a shim ahead of each stub CLI logs
  # the env NAMES (never values) of EVERY process of that CLI, then runs the stub. PATH in the log proves names are
  # recorded, so an absent name means something.
  mkdir -p "$SB/binenv"
  cat >"$SB/envshim" <<'STUB'
#!/bin/sh
{ echo "== @CLI@ $1"; awk 'BEGIN { for (k in ENVIRON) print k }'; } >>"$SEAT_ENV_LOG"
exec "@BIN@" "$@"
STUB
  for c in claude codex grok opencode; do sed -e "s|@CLI@|$c|" -e "s|@BIN@|$SB/bin/$c|" "$SB/envshim" >"$SB/binenv/$c"; chmod +x "$SB/binenv/$c"; done
  shim_run() { PATH="$SB/binenv:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" "$0" "$@"; }
  for w in claude:sonnet codex:gpt-5.6-terra grok:grok-4.6 opencode:opencode-go/deepseek-v4.1-flash; do   # every wire, the socket exported to the buy
    CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub SEAT_ENV_LOG="$SB/env168-buy.log" \
      shim_run --effort low --cwd "$SB/tmp" --out "$SB/e153-${w%%:*}" "$w" "$SB/q.md" >/dev/null 2>&1; done
  for i in $(seq 1 30); do [ "$(grep -c 'e153-' "$SB/push.log" 2>/dev/null)" -ge 4 ] && break; sleep 0.1; done
  # 153: THE PUSH SURVIVES THE STRIP. Falsification: put main's top-of-file unset back in this file -> no capture, no push
  t 153 "THE DRIVER'S INBOX STAYS THE WRAPPER'S: with the socket and token exported to the buy, every wire's wrapper -- stdin (claude), rpc (codex), ACP (grok, opencode) -- captures both (DIR/driver's line 1 = the exported socket) and its turn_end push reaches the socket (the seat sees neither name: row 168)" \
    sh -c "for c in claude codex grok opencode; do [ \"\$(head -1 '$SB'/e153-\$c/driver)\" = '$SB/d.sock' ] || exit 1; done && python3 '$SB/chk.py' push '$SB/push.log' '$SB/e153-claude' ok 1 'claude:sonnet t1 ok: bg=1 mem=1 turn=1' 'bg=1 mem=1 turn=1' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/e153-codex' ok 1 'codex:gpt-5.6-terra t1 ok: stub ok' 'stub ok' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/e153-grok' ok 1 'grok:grok-4.6 t1 ok: PONG' 'PONG' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/e153-opencode' ok 1 'opencode:opencode-go/deepseek-v4.1-flash t1 ok: PONG' 'PONG'"
  # 156: PROBES NEVER PUSH. A probe is internal: with the stub socket exported, its job dir captures no socket and the socket
  # receives nothing from it. Falsification: drop run_probe's unset -> a driver file captured, a 'probe-' push logged.
  ( export CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub; mkdir -p "$SB/tmp156"
    PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp156" XDG_STATE_HOME="$SB/tmp156" XDG_CACHE_HOME="$SB/cache" "$0" --probe claude >"$SB/p156.out" 2>&1
    PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp156" XDG_STATE_HOME="$SB/tmp156" XDG_CACHE_HOME="$SB/cache" "$0" --probe codex >>"$SB/p156.out" 2>&1 ) || true
  sleep 0.5
  t 156 "PROBES NEVER PUSH: --probe claude and --probe codex with the driver socket + token exported answer ok, capture no socket in their job dirs, and post nothing to it" \
    sh -c "[ \"\$(grep -c '\"status\":\"ok\"' '$SB/p156.out')\" = 2 ] && ls -d '$SB'/tmp156/hands/probe-*-claude '$SB'/tmp156/hands/probe-*-codex >/dev/null && [ -z \"\$(find '$SB/tmp156' -name driver)\" ] && ! grep -q 'probe-' '$SB/push.log'"
  stub_run mail "$SB/p89b" hello there >/dev/null 2>&1
  t 91 "MAIL: hands.sh mail appends one {t, from: driver, text} line to the seat's inbox.jsonl" \
    sh -c "tail -1 '$SB/p89b/inbox.jsonl' | jq -e '.from==\"driver\" and .text==\"hello there\" and (.t|type==\"string\")' >/dev/null"
  ( stub_run wait "$SB/p89b" --for mail --timeout 10 >"$SB/w92.out" 2>&1; echo $? >"$SB/w92.rc" ) & local W92=$!
  # the waiter counts only lines after its start, which no file marks: mail until it answers (a slow start never misses)
  for i in $(seq 40); do kill -0 $W92 2>/dev/null || break; echo '{"to":"driver","text":"mail-92"}' >>"$SB/p89b/outbox.jsonl"; sleep 0.25; done; wait $W92
  ( stub_run wait "$SB/p89b" --for exit --timeout 30 >"$SB/w92e.out" 2>&1; echo $? >"$SB/w92e.rc" ) & local W92E=$!
  sleep 0.6; stub_run stop "$SB/p89b" >"$SB/p93.stop" 2>&1; echo $? >"$SB/p93.rc"; wait $W92E
  t 92 "WAIT returns on new mail (--for mail prints the outbox line, rc 0) and on process exit (--for exit prints the final envelope)" \
    sh -c "[ \"\$(cat '$SB/w92.rc')\" = 0 ] && grep -q mail-92 '$SB/w92.out' && tail -1 '$SB/w92e.out' | jq -e '.status==\"incomplete\" and .rc==143' >/dev/null"
  t 93 "STOP reaps a persistent seat mid-turn: the seat process is dead, no wrapper remains, and the turn in flight is typed incomplete (rc 143) with its session" \
    sh -c "[ \"\$(cat '$SB/p93.rc')\" = 0 ] && python3 '$SB/chk.py' gone '$SB/p89b' \$(cat '$SB/p89b.pid') && jq -e '.status==\"incomplete\" and .rc==143 and .session==\"sess-stub\" and .turn==1' '$SB/p89b/envelope.json' >/dev/null"
  # 168 (main's hands row 81): NO SEAT INHERITS THE DRIVER'S MESSAGING SOCKET/TOKEN. The caller exports both; the shim
  # logs every process of every provider CLI on a buy per provider (153's) and --probe all. Falsification: drop the
  # wrapper's strip (seat.py spawn) -> both halves.
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub SEAT_ENV_LOG="$SB/env168-probe.log" shim_run --probe all >/dev/null 2>&1
  t 168 "no seat inherits the driver's messaging socket/token: with the caller exporting both, neither name reaches any process of any provider's CLI on a buy (each wire) or --probe all" \
    sh -c "for c in claude codex grok opencode; do grep -q \"^== \$c \" '$SB/env168-buy.log' && grep -q \"^== \$c \" '$SB/env168-probe.log' || exit 1; done; for f in buy probe; do grep -qx PATH '$SB/env168-'\$f.log && ! grep -q '^CLAUDE_CODE_MESSAGING_' '$SB/env168-'\$f.log || exit 1; done"
  # 169 (main's reply row 33, on send): a seat bought with NO socket exported is gone; a send whose caller exports both
  # relaunches it: the relaunched CLI sees neither name, its wrapper captured the socket, and pushed its turn (2, never
  # 1). Falsification: drop the wrapper's strip -> the env half; put main's top-of-file unset back -> no capture, no push.
  for w in claude:sonnet codex:gpt-5.6-terra grok:grok-4.6 opencode:opencode-go/deepseek-v4.1-flash; do c="${w%%:*}"
    SEAT_ENV_LOG=/dev/null shim_run --effort low --cwd "$SB/tmp" --out "$SB/s169-$c" "$w" "$SB/q.md" >/dev/null 2>&1
    case "$c" in grok|opencode) export ACP_REPLAY="$HERE/fixtures/$c-acp-resume-sample.jsonl" ;; esac
    CLAUDE_CODE_MESSAGING_SOCKET="$SB/d.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub SEAT_ENV_LOG="$SB/env169.log" shim_run send "$SB/s169-$c" "$SB/q81.md" >/dev/null 2>&1
    stub_run wait "$SB/s169-$c" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/s169-$c" >/dev/null 2>&1; unset ACP_REPLAY; done
  for i in $(seq 1 30); do [ "$(grep -c 's169-' "$SB/push.log" 2>/dev/null)" -ge 4 ] && break; sleep 0.1; done
  t 169 "no relaunched seat inherits the driver's messaging socket/token, and its push survives: a send to each provider's GONE seat, its caller exporting both, relaunches a CLI that sees neither name; the relaunching wrapper captured the socket and its turn 2 push reaches it" \
    sh -c "for c in claude codex grok opencode; do grep -q \"^== \$c \" '$SB/env169.log' && [ \"\$(head -1 '$SB'/s169-\$c/driver)\" = '$SB/d.sock' ] || exit 1; done; grep -qx PATH '$SB/env169.log' && ! grep -q '^CLAUDE_CODE_MESSAGING_' '$SB/env169.log' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/s169-claude' ok 1 'claude:sonnet t2 ok: bg=1 mem=1 turn=1' 'bg=1 mem=1 turn=1' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/s169-codex' ok 1 'codex:gpt-5.6-terra t2 ok: stub ok' 'stub ok' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/s169-grok' ok 1 'grok:grok-4.6 t2 ok: PONG' 'PONG' && python3 '$SB/chk.py' push '$SB/push.log' '$SB/s169-opencode' ok 1 'opencode:opencode-go/deepseek-v4.1-flash t2 ok: PONG' 'PONG'"
  kill "$SOCKPID" 2>/dev/null
  # THE VIEW (135-136): `events DIR` renders; the raw stream is `tail -f DIR/events.jsonl`. Recorded live streams:
  for c in grok claude opencode; do mkdir -p "$SB/v-$c"; cp "$HERE/fixtures/$c-events-sample.jsonl" "$SB/v-$c/events.jsonl"
    "$0" events "$SB/v-$c" >"$SB/v-$c.out" 2>/dev/null; done
  t 135 "THE VIEW: the grok 228-line recorded stream renders to 20-25 readable lines -- text deltas merged into blocks, one line per tool call (input clipped), a line per delivered input and turn_start, '── turn N ok · time' per turn_end, a usage line per turn with its cache hit (0%, 99%, 98%), no raw JSON" \
    python3 "$SB/chk.py" grokview "$SB/v-grok.out"
  t 136 "THE VIEW is deterministic: each recorded proof (grok, claude, opencode) renders the same bytes twice and when fed one line at a time (a block closes on the next event, never on a pause, so --follow prints what the one-shot prints); an input opencode names late rides ONE ↳ line under its call" \
    sh -c "python3 '$SB/chk.py' steady '$HERE' '$0' '$SB/v-grok' '$SB/v-claude' '$SB/v-opencode' && python3 '$SB/chk.py' lateinput '$SB/v-opencode.out'"
  : >"$SB/p131.pid"
  CL_STUB_MODE=park CL_STUB_PIDFILE="$SB/p131.pid" stub_run start --cwd "$SB/tmp" --out "$SB/p131" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/p131.pid" ] && break; sleep 0.1; done
  : >"$SB/p137.calls"
  CL_STUB_MODE=slow CL_STUB_CALLS="$SB/p137.calls" CL_STDIN_LOG="$SB/p137.stdin" stub_run start --cwd "$SB/tmp" --out "$SB/p137" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run send "$SB/p137" "$SB/q81.md" >"$SB/p137.sent" 2>/dev/null
  for i in $(seq 1 80); do [ -e "$SB/p137/turn-2/envelope.json" ] && break; sleep 0.1; done
  stub_run stop "$SB/p137" >/dev/null 2>&1
  t 137 "A SEND TO A BUSY SEAT NEVER CANCELS ITS TURN -- stdin (claude, steer), acp (grok, queue); rpc (codex, steer) is row 117: turn 1 ends ok (rc 0) with its own answer, the busy send's message is answered after it as turn 2, on no extra launch (one CLI process), and no cancel reaches the CLI (only user lines on claude's stdin, then the stop's EOF; no session/cancel on the ACP wire)" \
    sh -c "python3 '$SB/chk.py' nocancel '$SB/p137' 1 '$SB/p137.stdin' && python3 '$SB/chk.py' nocancel '$SB/a112' 1 '$SB/a112.rec'"
  t 131 "events --follow ENDS WITH THE SEAT: following a live seat, it exits (rc 0) within 2 s of stop, after draining the file (the stopped turn's incomplete turn_end is its last news) -- no wrapper alive is the one signal, no new state" \
    python3 "$SB/chk.py" follows "$0" "$SB/p131"
  # THE TWO DELIVERY RACES (stub tests first, then the fixes). 138: a send killed mid-write leaves a
  # record with no end in in.fifo; framed LF+line+LF, its torn bytes end at the next record's leading LF and the wrapper,
  # the ONE reader of in.fifo for every wire, drops them. 139: two sends racing to a gone seat relaunch it ONCE -- the
  # wrapper holds job.log's lock for its life, and the losing launch delivers its message as a send to the winner.
  CL_STUB_MODE=echo CL_STDIN_LOG="$SB/p138.stdin" stub_run start --cwd "$SB/tmp" --out "$SB/p138" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/p138" --timeout 10 >/dev/null 2>&1
  python3 "$SB/chk.py" tear "$SB/p138/in.fifo"
  stub_run send "$SB/p138" "$SB/q81.md" >/dev/null 2>&1; stub_run wait "$SB/p138" --timeout 10 >/dev/null 2>&1; stub_run stop "$SB/p138" >/dev/null 2>&1
  t 138 "A SEND KILLED MID-WRITE NEVER FUSES WITH THE NEXT: the record it left unfinished in in.fifo is dropped (one typed progress event), and the next send reaches the claude CLI whole -- every line the CLI read is a user message, and turn 2 answers exactly the sent text" \
    python3 "$SB/chk.py" torn "$SB/p138" "$SB/p138.stdin" "$SB/q81.md"
  printf 'turn three\n' >"$SB/q139.md"
  CL_STUB_MODE=count stub_run start --cwd "$SB/tmp" --out "$SB/p139" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/p139" --timeout 10 >/dev/null 2>&1; stub_run stop "$SB/p139" >/dev/null 2>&1
  : >"$SB/p139.calls"
  ( CL_STUB_MODE=count CL_STUB_CALLS="$SB/p139.calls" stub_run send "$SB/p139" "$SB/q81.md" >"$SB/p139.s1" 2>/dev/null ) & S139A=$!
  ( CL_STUB_MODE=count CL_STUB_CALLS="$SB/p139.calls" stub_run send "$SB/p139" "$SB/q139.md" >"$SB/p139.s2" 2>/dev/null ) & S139B=$!
  wait $S139A $S139B; stub_run wait "$SB/p139" --timeout 10 >/dev/null 2>&1; stub_run stop "$SB/p139" >/dev/null 2>&1
  t 139 "TWO SENDS RACING TO A GONE SEAT RELAUNCH IT ONCE: one CLI launch answers both (turns 2 and 3 on one new pid), one send acks spawned and the other sent" \
    python3 "$SB/chk.py" relaunch1 "$SB/p139" "$SB/p139.calls" "$SB/p139.s1" "$SB/p139.s2"
  # DRIVER MAIL (142-144): hands.sh mail appends a driver line to the seat's inbox.jsonl, and the seat's wrapper delivers
  # it through THE delivery a send uses (steer/queue, never a cancel), once -- the marker names the inbox line.
  : >"$SB/m140b.calls"
  CL_STUB_MODE=echo CL_STUB_CALLS="$SB/m140b.calls" stub_run start --cwd "$SB/tmp" --out "$SB/m140b" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/m140b" --timeout 10 >/dev/null 2>&1
  MB="$(cd "$SB/m140b" && pwd -P)"
  until_file() { local i; for i in $(seq 1 100); do [ -e "$1" ] && return 0; sleep 0.1; done; return 1; }
  stub_run mail "$MB" "from the driver" >/dev/null 2>&1; R142=$?; until_file "$MB/turn-2/envelope.json"
  t 142 "DRIVER MAIL: hands.sh mail to a live seat (rc 0) is delivered by its wrapper as 'MAIL from driver', and answered as the next turn -- no seat has to read its inbox" \
    sh -c "[ $R142 -eq 0 ] && [ \"\$(cat '$MB/turn-2/result.txt')\" = \"\$(printf 'MAIL from driver:\nfrom the driver')\" ]"
  stub_run stop "$MB" >/dev/null 2>&1
  stub_run mail "$MB" "driver while out" >/dev/null 2>&1; R143=$?; cp "$SB/m140b.calls" "$SB/m143.calls0"
  CL_STUB_MODE=echo CL_STUB_CALLS="$SB/m140b.calls" stub_run send "$MB" "$SB/q81.md" >/dev/null 2>&1; until_file "$MB/turn-4/envelope.json"
  stub_run stop "$MB" >/dev/null 2>&1
  CL_STUB_MODE=echo CL_STUB_CALLS="$SB/m140b.calls" stub_run send "$MB" "$SB/q81.md" >/dev/null 2>&1; until_file "$MB/turn-5/envelope.json"; sleep 1.5
  stub_run stop "$MB" >/dev/null 2>&1
  t 143 "a GONE seat: driver mail to it is rc 1 and launches nothing; the line waits in its inbox, and the next launch delivers it ONCE after the send's own message (turns 3-4); a second restart re-delivers none (turn 5 is the send alone) -- delivered = a marker names the inbox line" \
    python3 "$SB/chk.py" regone "$MB" "$SB/m143.calls0" "$SB/m140b.calls" "$R143"
  : >"$SB/m144.calls"
  CX_STUB_MODE=slow CX_STUB_SLEEP=4 CX_STUB_CALLS="$SB/m144.calls" CX_STDIN_LOG="$SB/m144.stdin" stub_run start --cwd "$SB/cxw" --out "$SB/m144" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  MC="$(cd "$SB/m144" && pwd -P)"
  stub_run mail "$MC" "while you work" >/dev/null 2>&1; until_file "$MC/turn-1/envelope.json"
  stub_run stop "$MC" >/dev/null 2>&1
  t 144 "driver mail to a BUSY steered seat (codex, mid-turn) joins its turn: its marker lands before turn 1 ends, the labelled text reaches the one app-server as a turn/start the open turn takes, and turn 1 ends ok (rc 0) answering its prompt and the mail -- no second turn, nothing cancelled" \
    python3 "$SB/chk.py" busymail "$MC" "$SB/m144.calls" "$SB/m144.stdin"
  # 159, 170: NO PERSONAL HOOKS, NO CODEX MEMORIES. Every seat runs its CLI with the user's hooks off, each by its own
  # switch, and codex with the user's memories off (the twin of claude's auto-memory off), on a launch and a relaunch.
  # Falsification: drop any one switch -> its half here.
  CX_ARGV_LOG="$SB/h159a.argv" stub_run start --cwd "$SB/tmp" --out "$SB/h159" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/h159" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/h159" >/dev/null 2>&1
  CX_ARGV_LOG="$SB/h159b.argv" stub_run send "$SB/h159" "$SB/q81.md" >/dev/null 2>&1
  stub_run wait "$SB/h159" --timeout 30 >/dev/null 2>&1; stub_run stop "$SB/h159" >/dev/null 2>&1
  t 159 "NO SEAT RUNS THE USER'S HOOKS: codex disables hooks as a root option on an app-server launch and relaunch; claude's ONE --settings carries disableAllHooks on a launch (g49) and a relaunch (p82); grok's agent runs with its claude- and cursor-hook scanners off (a100)" \
    sh -c "python3 '$SB/chk.py' disabled hooks '$SB/h159a.argv' '$SB/h159b.argv' && for f in '$SB/g49.argv' '$SB/p82.argv'; do [ \"\$(grep -c -- --settings \"\$f\")\" = 1 ] && awk 'p==\"--settings\"{print;exit}{p=\$0}' \"\$f\" | jq -e '.disableAllHooks==true' >/dev/null || exit 1; done && sed -n 6p '$SB/a100.env' | grep -qx hooks=0,0"
  t 170 "NO SEAT USES THE USER'S CODEX MEMORIES: every codex launch disables memories as a root option, a launch and a relaunch" \
    python3 "$SB/chk.py" disabled memories "$SB/h159a.argv" "$SB/h159b.argv"
  # 164: the engine's opens of a seat's own mail files never follow a link (the suite plants the links): the outbox read
  # and the driver's mail. Falsification: read the outbox with a plain open (the linked file's line is pushed), let the
  # driver's mail follow the link (the victim is written) or raise (rc 1)
  python3 "$SB/sockserv.py" "$SB/n164.sock" "$SB/n164push.log" "$SB/n164.noreply" >/dev/null 2>&1 & NSOCK=$!
  for i in $(seq 1 50); do [ -S "$SB/n164.sock" ] && break; sleep 0.1; done
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/n164.sock" CLAUDE_CODE_MESSAGING_TOKEN=tok-stub CL_STUB_MODE=echo \
    stub_run start --cwd "$SB/tmp" --out "$SB/n164" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/n164" --timeout 10 >/dev/null 2>&1
  N164="$(cd "$SB/n164" && pwd -P)"; echo keep >"$SB/v164"; : >"$SB/v164.out"
  rm "$N164/outbox.jsonl"; ln -s "$SB/v164.out" "$N164/outbox.jsonl"
  printf '{"text":"LEAKED"}\n' >>"$SB/v164.out"; sleep 2.5
  rm "$N164/outbox.jsonl" "$N164/inbox.jsonl"; ln -s "$SB/v164" "$N164/inbox.jsonl"
  printf '{"text":"after the link"}\n' >"$N164/outbox.jsonl"
  for i in $(seq 1 50); do grep -q 'after the link' "$SB/n164push.log" 2>/dev/null && break; sleep 0.1; done
  stub_run mail "$N164" "through a link" >/dev/null 2>"$SB/n164.err"; R164=$?
  stub_run stop "$N164" >/dev/null 2>&1; kill "$NSOCK" 2>/dev/null
  t 164 "NO LINK IN A JOB DIR: a seat's outbox.jsonl swapped for a link is never read (its line is never pushed; the next real line is), and the driver's mail to a linked inbox is refused (rc 2) -- the victim untouched" \
    sh -c "[ $R164 = 2 ] && [ \"\$(cat '$SB/v164')\" = keep ] && ! grep -q LEAKED '$SB/n164push.log' && grep -q 'after the link' '$SB/n164push.log'"
  CX_STUB_MODE=nofinal stub_run --out "$SB/p94" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 94 "an answer FOLLOWED by a tool call is voided, never promoted: narration before a tool is no report (the turn fails closed: error, reason empty)" \
    sh -c "jq -e '.status==\"error\" and .reason==\"empty\"' '$SB/p94/envelope.json' >/dev/null && [ ! -e '$SB/p94/result.txt' ] && jq -e -s 'map(select(.kind==\"tool_call\" and .call==\"exec-5\")) | length==1' '$SB/p94/events.jsonl' >/dev/null"
  t 95 "INBOX BEFORE PROCESS: every buy creates DIR/inbox.jsonl and DIR/outbox.jsonl, empty, before its seat starts, so the SEAT BOX names existing files (an ACP grok seat too)" \
    sh -c "for d in '$SB/a100' '$SB/g49'; do [ -f \"\$d/inbox.jsonl\" ] && [ ! -s \"\$d/inbox.jsonl\" ] && [ -f \"\$d/outbox.jsonl\" ] && [ ! -s \"\$d/outbox.jsonl\" ] || exit 1; done"
  : >"$SB/p96.pid"
  CL_STUB_MODE=deaf CL_STUB_PIDFILE="$SB/p96.pid" stub_run start --cwd "$SB/tmp" --out "$SB/p96" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  for i in $(seq 1 50); do [ -s "$SB/p96.pid" ] && break; sleep 0.1; done
  stub_run stop "$SB/p96" >/dev/null 2>&1; echo $? >"$SB/p96.rc"
  t 96 "STOP in the close's order: a claude seat that ignores its stdin's EOF and the TERM is KILLed by its wrapper — no process remains, and the turn in flight is typed by the stop (incomplete, rc 143)" \
    sh -c "[ \"\$(cat '$SB/p96.rc')\" = 0 ] && python3 '$SB/chk.py' gone '$SB/p96' \$(cat '$SB/p96.pid') && jq -e '.status==\"incomplete\" and .rc==143 and .turn==1' '$SB/p96/envelope.json' >/dev/null"

  # classification corpus
  local fx st
  for fx in grok-402-balance grok-403-spending claude-session-limit codex-usage-limit; do
    st="$(classify_blob "$(cat "$HERE/fixtures/$fx.txt")")"
    t 12 "fixture $fx → provider-quota-dead" sh -c "printf '%s' '${st%%$'\t'*}' | grep -qx provider-quota-dead"
  done
  st="$(classify_blob "$(cat "$HERE/fixtures/claude-session-limit.txt")")"
  t 13 "claude fixture extracts reset"    sh -c "printf '%s' \"${st#*$'\t'}\" | grep -qi 'resets 6:20am'"
  st="$(classify_blob "$(cat "$HERE/fixtures/codex-usage-limit.txt")")"
  t 14 "codex fixture extracts reset"     sh -c "printf '%s' \"${st#*$'\t'}\" | grep -qi 'again at Aug'"
  st="$(classify_blob "$(cat "$HERE/fixtures/grok-auth-expired.txt")")"
  t 15 "auth fixture → auth-expired"      sh -c "printf '%s' '${st%%$'\t'*}' | grep -qx auth-expired"
  st="$(classify_blob "$(cat "$HERE/fixtures/claude-oauth-expired.txt")")"
  t 15b "claude oauth fixture → auth-expired" sh -c "printf '%s' '${st%%$'\t'*}' | grep -qx auth-expired"
  # no-credential fixture: a 2.x seat whose store held no Go credential read bare error
  st="$(classify_blob "$(cat "$HERE/fixtures/opencode-go-no-credential.txt")")"
  t 15c "opencode 2.x no-credential fixture → auth-expired" sh -c "printf '%s' '${st%%$'\t'*}' | grep -qx auth-expired"
  st="$(classify_blob "plain unrelated failure text")"
  t 16 "unrelated text → error"           sh -c "printf '%s' '${st%%$'\t'*}' | grep -qx error"

  # 204: quota is the read-only board, quota.sh behind hands.sh's verb. Falsification: drop the verb -> the words are
  # read as a buy and refused
  mkdir -p "$SB/st204"
  t 204 "QUOTA: hands.sh quota --json claude prints the board's JSON for that one pool (no claude reading in an empty jobs root: unknown, naming the refresh), rc 0" \
    sh -c "XDG_STATE_HOME='$SB/st204' '$0' quota --json claude | jq -e '.pools | length==1 and .[0].pool==\"claude\" and .[0].status==\"unknown\" and (.[0].why|test(\"--probe claude\"))' >/dev/null"
  # the not-installed probe shape (PATH stripped of all provider CLIs; system tools kept)
  t 20 "probe(all) four not-installed lines full-schema" sh -c "PATH=/usr/bin:/bin HOME='$SB' TMPDIR='$SB/tmp' XDG_CACHE_HOME='$SB/cache' '$0' --probe all | jq -e -s 'length==4 and all(.[]; .status==\"not-installed\" and has(\"dir\") and has(\"usage\"))'"
  # 20b: the probe's temp files are made fresh (mktemp): links planted at the names an older probe used ($TMPDIR/
  # hands-probe-lines-<pid> and hands-probe-<pid>.txt, the pid known because sh exec keeps it) are never written through.
  # Falsification: the fixed names -> the victim is truncated and rewritten
  mkdir -p "$SB/tmp20b"; printf keep >"$SB/victim20b"
  PATH=/usr/bin:/bin HOME="$SB" TMPDIR="$SB/tmp20b" XDG_CACHE_HOME="$SB/cache" sh -c 'ln -s "$1" "$2/hands-probe-lines-$$"; ln -s "$1" "$2/hands-probe-$$.txt"; exec "$3" --probe grok' _ "$SB/victim20b" "$SB/tmp20b" "$SELF" >/dev/null 2>&1
  t 20b "probe temp files are fresh: links planted at fixed names are never written through, and the probe leaves no temp file" \
    sh -c "[ \"\$(cat '$SB/victim20b')\" = keep ] && [ \"\$(ls '$SB/tmp20b' | grep -vc '^hands-probe-' )\" = 0 ] && [ \"\$(ls '$SB/tmp20b' | wc -l | tr -d ' ')\" = 2 ]"

  # 199: a wrapper that dies mid-turn (here KILLed with its CLI) leaves the sent turn without an end: wait says so,
  # incomplete wrapper-gone rc 1, never the previous turn's ok answer. Falsification: report the last envelope -> turn 1's
  # answer and ok
  # each turn takes 20 s, so the kill lands inside turn 2 even on a loaded machine
  CL_STUB_MODE=slow CL_STUB_SLEEP=20 stub_run start --cwd "$SB/tmp" --out "$SB/w199" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  stub_run wait "$SB/w199" --timeout 60 >/dev/null 2>&1; stub_run send "$SB/w199" "$SB/q81.md" >/dev/null 2>&1
  for i in $(seq 100); do jq -s -e 'any(.[]; .kind=="turn_start" and .turn==2)' "$SB/w199/events.jsonl" >/dev/null 2>&1 && break; sleep 0.1; done
  W199="$(ps -A -ww -o pid=,command= | grep "seat.py spawn $SBP/w199 " | grep -v grep | awk '{print $1}' | head -1)"
  [ -n "$W199" ] && kill -KILL -- "-$W199" 2>/dev/null; sleep 0.3
  stub_run wait "$SB/w199" --timeout 10 >"$SB/w199.wait" 2>/dev/null; echo $? >"$SB/w199.rc"
  t 199 "WAIT AFTER A WRAPPER DIED MID-TURN: incomplete, reason wrapper-gone, the sent turn (2), rc 1 -- never turn 1's ok answer" \
    sh -c "[ -n '$W199' ] && [ \"\$(cat '$SB/w199.rc')\" = 1 ] && jq -e '.status==\"incomplete\" and .reason==\"wrapper-gone\" and .turn==2' '$SB/w199.wait' >/dev/null"
  # 200: the driver file is written 0600 through the no-link open (opened()): a link planted there (a seat can, in
  # its own dir) takes nothing at the next capture, a relaunching send. Falsification: open driver without
  # O_NOFOLLOW -> the victim holds the token
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/nosock200" CLAUDE_CODE_MESSAGING_TOKEN=tok-200 stub_run --cwd "$SB/tmp" --out "$SB/p200" claude:sonnet "$SB/q.md" >/dev/null 2>&1
  printf keep >"$SB/v200"; rm -f "$SB/p200/driver"; ln -s "$SB/v200" "$SB/p200/driver"
  CLAUDE_CODE_MESSAGING_SOCKET="$SB/nosock200" CLAUDE_CODE_MESSAGING_TOKEN=tok-200 stub_run send "$SB/p200" "$SB/q81.md" >/dev/null 2>&1
  stub_run wait "$SB/p200" --timeout 20 >/dev/null 2>&1; stub_run stop "$SB/p200" >/dev/null 2>&1
  t 200 "CAPTURE NEVER WRITES THROUGH A LINK: a link planted at DIR/driver stays a link and the file it names is untouched" \
    sh -c "[ \"\$(cat '$SB/v200')\" = keep ] && [ -L '$SB/p200/driver' ]"
  rm -f "$SB/p200/driver"   # no planted link outlives its row (157 reads every driver file)
  # 201: the sweep never crosses into another filesystem: a stale job dir with a mount in it (lstat's st_dev differs,
  # forced in-process) is not stale. Falsification: drop the st_dev check -> stale() says True
  mkdir -p "$SB/t201"
  t 201 "THE SWEEP STOPS AT A MOUNT: a dir stale in every other way is kept when something under it is on another device" \
    python3 -c 'import os, sys, time
sys.path.insert(0, sys.argv[1]); import seat
p = sys.argv[2]; os.makedirs(os.path.join(p, "mnt")); old = time.time() - 31 * 86400
for q in (os.path.join(p, "mnt"), p): os.utime(q, (old, old))
cut = time.time() - seat.KEEP
assert seat.stale(p, cut)
real = os.lstat
def lstat(q, *a, **k):
    r = real(q, *a, **k)
    return os.stat_result((r[0], r[1], r[2] + 1) + tuple(r[3:])) if q.endswith("/mnt") else r
seat.os.lstat = lstat
assert not seat.stale(p, cut)' "$HERE" "$SB/t201/20250101-000000-codex-9"
  # 188: THE JOBS ROOT. A buy without --out makes its dir in $XDG_STATE_HOME/hands,
  # else HOME/.local/state/hands (unset, or relative: XDG ignores a relative value) -- never in $TMPDIR, which macOS
  # wipes at boot and prunes after 3 days -- named <date>-<time>-<provider>-<pid>, root and dir 0700 (XDG; prompts live
  # there); an --out dir is 0700 too, its parents keep the caller's umask; no buy anywhere in this suite landed in $TMPDIR/hands. The root is
  # seat.py's JOBS, its one copy (hands.sh takes sweep's output). Falsification: the old ${TMPDIR}/hands root -> all
  # land in $SB/tmp; drop the umask -> 0755; drop the --out umask -> the caller's; honor a relative value -> it lands in the cwd;
  # another fallback in seat.py -> the HOME buys land elsewhere
  mkdir -p "$SB/h188" "$SB/h188r"
  XDG_STATE_HOME="$SB/st188" stub_run --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  ( unset XDG_STATE_HOME; HOME="$SB/h188" stub_run --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1 )
  ( cd "$SB/tmp" && XDG_STATE_HOME=rel188 HOME="$SB/h188r" PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" XDG_CACHE_HOME="$SB/cache" \
    "$SELF" --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1 )
  stub_run --cwd "$SB/tmp" --out "$SB/o188/x" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  mkdir -p "$SB/o188e"; chmod 755 "$SB/o188e"; stub_run --cwd "$SB/tmp" --out "$SB/o188e" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  t 188 "THE JOBS ROOT: a buy without --out lands in \$XDG_STATE_HOME/hands (unset or relative: HOME/.local/state/hands), one dir named <date>-<time>-<provider>-<pid>, the root and the dir 0700, the seat answered ok; an --out dir is 0700 (an existing empty one too), its parent keeps the caller's umask; nothing in this suite landed in \$TMPDIR/hands or in a relative root" \
    python3 -c 'import os, re, sys, json
for r in sys.argv[1:4]:
    n = os.listdir(r)
    assert len(n) == 1 and re.fullmatch(r"\d{8}-\d{6}-codex-\d+", n[0]), (r, n)
    for q in (r, os.path.join(r, n[0])):
        assert os.stat(q).st_mode & 0o777 == 0o700, (q, oct(os.stat(q).st_mode))
    assert json.load(open(os.path.join(r, n[0], "envelope.json")))["status"] == "ok"
u = os.umask(0); os.umask(u)
q = os.path.join(sys.argv[4], "x")
assert os.stat(sys.argv[4]).st_mode & 0o777 == 0o777 & ~u, oct(os.stat(sys.argv[4]).st_mode)
assert os.stat(q).st_mode & 0o777 == 0o700, (q, oct(os.stat(q).st_mode))
assert not os.path.exists(sys.argv[5]) and not os.path.exists(sys.argv[6])
assert os.stat(sys.argv[7]).st_mode & 0o777 == 0o700, oct(os.stat(sys.argv[7]).st_mode)' "$SB/st188/hands" "$SB/h188/.local/state/hands" \
      "$SB/h188r/.local/state/hands" "$SB/o188" "$SB/tmp/hands" "$SB/tmp/rel188" "$SB/o188e"
  # 189: THE RETENTION. Every buy sweeps the jobs root first: a dir hands.sh named there, with
  # nothing in it written in 30 days, no repository in it and its job.log lock free, is deleted; a dir with one file
  # written 29 days ago deep in a turn dir, one whose lock a live process holds (a wrapper holds it for its whole life),
  # one --out-style name, one holding a .git and a link to an old dir elsewhere all stay, and the link's target gains
  # nothing. Falsification, each alone: a sweep that deletes nothing (the old dirs stay); the top dir's mtime alone (the
  # 29-day dir goes); no lock (the locked dir goes); no name check (keepme goes); no repository check (it goes); a naming
  # change NAMED misses (the real dirs stay); follow a link (it is hidden as .<name>, never to return); take a CLAUDE.md
  # for a repository (codex-6 stays)
  R189="$SB/st189/hands"; mkdir -p "$SB/t189"
  # a buy's dir and a probe's, as hands.sh names them today: the sweep's NAMED must take both
  # (a naming change that NAMED misses would leave every dir unswept)
  XDG_STATE_HOME="$SB/st189" stub_run --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  XDG_STATE_HOME="$SB/st189" stub_run --probe claude >/dev/null 2>&1
  REAL189="$(ls "$R189" | tr '\n' ' ')"
  for n in 20250101-000000-codex-1 probe-1-claude 20250101-000000-codex-2 20250101-000000-codex-3 keepme 20250101-000000-codex-4 20250101-000000-codex-6; do
    mkdir -p "$R189/$n/turn-1"; echo '{"provider":"codex","cwd":"/"}' >"$R189/$n/session"; echo x >"$R189/$n/turn-1/result.txt"; : >"$R189/$n/job.log"; done
  mkdir -p "$R189/20250101-000000-codex-4/.git"; echo ref >"$R189/20250101-000000-codex-4/.git/HEAD"; echo x >"$R189/20250101-000000-codex-6/CLAUDE.md"
  echo t >"$SB/t189/session"; ln -s "$SB/t189" "$R189/20250101-000000-codex-5"
  python3 -c 'import os, sys, time
old, young = time.time() - 31 * 86400, time.time() - 29 * 86400
for root in sys.argv[1:]:
    for top, dirs, files in os.walk(root, topdown=False):
        for n in dirs + files:
            os.utime(os.path.join(top, n), (old, old), follow_symlinks=False)
    os.utime(root, (old, old))
os.utime(os.path.join(sys.argv[1], "20250101-000000-codex-2", "turn-1", "result.txt"), (young, young))' "$R189" "$SB/t189"
  python3 -c 'import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)
fcntl.flock(fd, fcntl.LOCK_EX); open(sys.argv[2], "w").close(); time.sleep(60)' "$R189/20250101-000000-codex-3/job.log" "$SB/h189.ready" & H189=$!
  for i in $(seq 1 50); do [ -e "$SB/h189.ready" ] && break; sleep 0.1; done
  XDG_STATE_HOME="$SB/st189" stub_run --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  kill "$H189" 2>/dev/null; wait "$H189" 2>/dev/null
  t 189 "THE RETENTION: a buy sweeps the jobs root first -- an engine-named dir (a real buy's and a real probe's, as hands.sh names them; hand-named ones; one holding a CLAUDE.md) with nothing written in 30 days goes; one with a file written 29 days ago deep in it, one whose job.log lock is held, an --out-style name, one holding a .git and a link to an old dir elsewhere stay (the target gains nothing); the buy's own dir is made" \
    python3 -c 'import os, re, sys
r, t = sys.argv[1], sys.argv[2]
n = set(os.listdir(r))
assert not {"20250101-000000-codex-1", "probe-1-claude", "20250101-000000-codex-6"} & n, n
assert {"20250101-000000-codex-2", "20250101-000000-codex-3", "keepme", "20250101-000000-codex-4", "20250101-000000-codex-5"} <= n, n
assert os.path.islink(os.path.join(r, "20250101-000000-codex-5")) and sorted(os.listdir(t)) == ["session"], os.listdir(t)
real = sys.argv[3].split()
assert len(real) == 2 and any(x.startswith("probe-") for x in real) and not set(real) & n, (real, n)
assert len([x for x in n if re.fullmatch(r"\d{8}-\d{6}-codex-\d+", x) and not x.startswith("2025")]) == 1, n' "$R189" "$SB/t189" "$REAL189"
  # 191: A RESUME IS USE. A send that finds no live wrapper touches the seat's dir (seat.py send, rc 3, creating
  # nothing) before the relaunch, so a buy's sweep in between keeps it. Here every file of a stopped seat is 31 days
  # old, a sweep runs right after that touch, and the relaunch answers turn 2. Falsification: drop send's utime -> the
  # sweep deletes the seat and the relaunch fails
  cat >"$SB/age.py" <<'PY'
import os, sys, time
old = time.time() - float(sys.argv[1]) * 86400   # every entry under each path, and the path, aged argv[1] days
for root in sys.argv[2:]:
    for top, dirs, files in os.walk(root, topdown=False):
        for n in dirs + files:
            os.utime(os.path.join(top, n), (old, old), follow_symlinks=False)
    os.utime(root, (old, old))
PY
  XDG_STATE_HOME="$SB/st191" stub_run --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>&1
  D191="$(ls -d "$SB"/st191/hands/*-codex-* 2>/dev/null | head -1)"; python3 "$SB/age.py" 31 "$D191"
  XDG_STATE_HOME="$SB/st191" python3 "$HERE/seat.py" send "$D191" "$SB/q81.md" >/dev/null 2>&1; R191=$?
  XDG_STATE_HOME="$SB/st191" python3 "$HERE/seat.py" sweep >/dev/null 2>&1
  XDG_STATE_HOME="$SB/st191" stub_run send "$D191" "$SB/q81.md" >/dev/null 2>&1
  stub_run wait "$D191" --timeout 30 >/dev/null 2>&1; stub_run stop "$D191" >/dev/null 2>&1
  t 191 "A RESUME IS USE: a send to a stopped seat whose files are all 31 days old touches it (rc 3: no live seat), a sweep right after keeps it, and the relaunch answers turn 2 ok" \
    sh -c "[ $R191 -eq 3 ] && [ -n '$D191' ] && [ -d '$D191' ] && jq -e '.status==\"ok\"' '$D191/turn-2/envelope.json' >/dev/null"
  # 192: THE SWEEP UNDER THE LOCK, in-process on seat.py's own sweep(), each case
  # forced at its worst moment: (a) a write that lands after the first judgment, before the lock, keeps the dir (judged
  # again under the lock); (b) a relaunch opening job.log by the dir's path while the delete runs finds no dir (renamed out
  # of every address first); (c) a delete that stops part way (a read-only subdir) is left as .<name>, and the next sweep
  # that finds it stale finishes it; (d) a dir with no job.log (a buy refused before its launch) goes, with no job.log made
  # in it. And the sweep prints the root. Falsification: no second judgment -> (a) goes; no rename -> (b) opens job.log;
  # no .<name> handling -> (c) stays forever; make job.log to lock it -> (d) is judged fresh and stays
  cat >"$SB/sw192.py" <<'PY'
import errno, fcntl, io, os, shutil, sys, time
sys.path.insert(0, sys.argv[1])
import seat
R, old, got = seat.JOBS, time.time() - 31 * 86400, {}
def mk(n, log=True, sub=None):
    p = os.path.join(R, n)
    os.makedirs(os.path.join(p, 'turn-1'))
    open(os.path.join(p, 'session'), 'w').write('{"provider":"codex","cwd":"/"}')
    open(os.path.join(p, 'turn-1', 'result.txt'), 'w').write('x')
    if log:
        open(os.path.join(p, 'job.log'), 'w').close()
    if sub:
        os.makedirs(os.path.join(p, sub))
        open(os.path.join(p, sub, 'f'), 'w').close()
    return p
a, b, c, d = (mk('20250101-000000-codex-1'), mk('20250101-000000-codex-2'), mk('20250101-000000-codex-3', sub='ro'),
              mk('20250101-000000-codex-4', log=False))
for top, ds, fs in os.walk(R, topdown=False):
    for n in ds + fs:
        os.utime(os.path.join(top, n), (old, old))
os.chmod(os.path.join(c, 'ro'), 0o500)
for p in (a, b, c, d):
    os.utime(p, (old, old))
real_flock, real_rmtree, ino_a = fcntl.flock, shutil.rmtree, os.stat(os.path.join(a, 'job.log')).st_ino
def flock(fd, op):   # (a): a write lands in a between its first judgment and its lock
    if os.fstat(fd).st_ino == ino_a:
        open(os.path.join(a, 'turn-1', 'late.txt'), 'w').close()
    return real_flock(fd, op)
def rmtree(path, *x, **k):   # (b): a relaunch opens b's job.log by b's own path while b is being deleted
    if path.endswith('codex-2'):
        try:
            os.close(os.open(os.path.join(b, 'job.log'), os.O_WRONLY | os.O_CREAT))
            got['b'] = 'opened'
        except OSError as e:
            got['b'] = e.errno
    return real_rmtree(path, *x, **k)
seat.fcntl.flock, seat.shutil.rmtree, out = flock, rmtree, io.StringIO()
sys.stdout, real_out = out, sys.stdout
seat.sweep()
sys.stdout = real_out
cdot = os.path.join(R, '.20250101-000000-codex-3')
hidden = os.path.isdir(cdot) and not os.path.lexists(c)
for q in (os.path.join(c, 'ro'), os.path.join(cdot, 'ro')):
    if os.path.isdir(q):
        os.chmod(q, 0o700)   # the delete's blocker gone (and the suite's own rm -rf can clear it)
if os.path.isdir(cdot):   # 31 days on, the next sweep finishes it in place
    for top, ds, fs in os.walk(cdot, topdown=False):
        for n in ds + fs:
            os.utime(os.path.join(top, n), (old, old))
    os.utime(cdot, (old, old))
sys.stdout = io.StringIO()
seat.sweep()
sys.stdout = real_out
checks = {'a kept': os.path.isdir(a), 'b no dir': got.get('b') == errno.ENOENT and not os.path.lexists(b),
          'c finished later': hidden and not os.path.lexists(cdot),
          'd gone': not os.path.lexists(d), 'prints root': out.getvalue() == R + '\n'}
print(checks)
sys.exit(0 if all(checks.values()) else 1)
PY
  XDG_STATE_HOME="$SB/st192" python3 "$SB/sw192.py" "$HERE" >"$SB/sw192.out" 2>&1
  t 192 "THE SWEEP UNDER THE LOCK: a write between the first judgment and the lock keeps the dir; a relaunch opening job.log while the dir goes finds no dir; a part-way delete is left as .<name> and the next stale sweep finishes it; a dir with no job.log goes, none made; the sweep prints the root" \
    sh -c "grep -q \"'a kept': True, 'b no dir': True, 'c finished later': True, 'd gone': True, 'prints root': True\" '$SB/sw192.out'"
  # 193: NO ROOT, NO BUY: a buy or a probe whose seat.py sweep prints no jobs root
  # (a broken seat.py) is refused -- rc 2 naming why, rc 1 for a probe -- and no dir is made anywhere (never /<name>).
  # Falsification: build the dir from the empty root -> the buy dies elsewhere, without the reason
  mkdir -p "$SB/b193"; cp "$SELF" "$SB/b193/hands.sh"; printf 'import sys\nsys.exit(1)\n' >"$SB/b193/seat.py"
  XDG_STATE_HOME="$SB/st193" PATH="$SB/bin:$PATH" "$SB/b193/hands.sh" --cwd "$SB/tmp" codex:gpt-5.6-terra "$SB/q.md" >/dev/null 2>"$SB/b193.err"; R193=$?
  XDG_STATE_HOME="$SB/st193" PATH="$SB/bin:$PATH" "$SB/b193/hands.sh" --probe claude >/dev/null 2>"$SB/b193p.err"; P193=$?
  t 193 "NO ROOT, NO BUY: with a seat.py whose sweep prints no jobs root, a buy is refused rc 2 naming why, a probe rc 1, and no dir is made" \
    sh -c "[ $R193 = 2 ] && [ $P193 = 1 ] && grep -q 'sweep printed no jobs root' '$SB/b193.err' && grep -q 'sweep printed no jobs root' '$SB/b193p.err' && [ ! -e '$SB/st193' ]"
  # 157: HERMETIC SUITE. The suite unsets the driver-inbox names at its top, so a seat
  # captures a socket only where its row exported a STUB one (under $SB). Falsification: drop the top unset and run the
  # suite with a fake socket exported -> job dirs capture the outer path.
  t 157 "HERMETIC SUITE: every driver socket any seat captured in this run is a stub under the sandbox -- none from the env the suite was started in" \
    sh -c "for f in \$(find '$SB' -name driver -type f); do case \"\$(head -1 \"\$f\")\" in '$SB'/*) ;; *) exit 1;; esac; done"
  wait "$A202" 2>/dev/null
  t 202 "A SLOW LAUNCH ACKS: an ACP launch whose handshake takes 70 s (past a minute, each call within its bound) acks spawned while its wrapper runs, never 'never delivered'" \
    jq -e '.status=="spawned"' "$SB/a202.ack"
  wait "$A205" 2>/dev/null
  t 205 "A STOP DURING THE LAUNCH: an ACP seat stopped in its handshake -- one ending 3 s in, one still answering at 10 s (stop TERMs its agent) -- reads incomplete, rc 143 (turn 1 for the first, its MSG never sent to the agent), the stop ends rc 0 with no wrapper left, and no wrapper thread dies with a traceback" \
    sh -c "for v in 3 70; do [ \"\$(cat '$SB'/a205-\$v.rc)\" = 0 ] && jq -e '.status==\"incomplete\" and .rc==143' '$SB'/a205-\$v/envelope.json >/dev/null && ! grep -q Traceback '$SB'/a205-\$v/job.log || exit 1; done; jq -e '.turn==1' '$SB/a205-3/envelope.json' >/dev/null && ! grep -q session/prompt '$SB/a205-3.rec' && python3 '$SB/chk.py' gone '$SB/a205-3' && python3 '$SB/chk.py' gone '$SB/a205-70'"
  for d in "$SB"/*/; do [ -f "$d/session" ] && "$0" stop "$d" >/dev/null 2>&1; done   # no seat outlives the suite
  rm -rf "$SB"
  echo "selftest: $PASS passed, $FAIL failed$([ "$SKIP" -gt 0 ] && echo ", $SKIP skipped")"
  [ "$FAIL" -eq 0 ]
}

# ---------- dispatch ----------
# SOURCE SEAM: seat.py sources this file for wt_alarm, the ONE reaper every seat process runs under; quota.sh
# for opencode_auth, the Go key's one path. A sourcing caller sets HANDS_SOURCED=1 and
# `set --` first; nothing below runs.
if [ -n "${HANDS_SOURCED:-}" ]; then return 0 2>/dev/null || exit 0; fi
case "$VERB" in   # the seat verbs (start is the buy below)
  send) [ "${#VERB_ARGS[@]}" -eq 2 ] || { echo "hands: usage: hands.sh send DIR FILE" >&2; exit 2; }
        seat_send "${VERB_ARGS[0]}" "${VERB_ARGS[1]}"; exit $? ;;
  mail|wait|events|stop) exec python3 "$SEATPY" "$VERB" ${VERB_ARGS[@]+"${VERB_ARGS[@]}"} ;;
esac
if [ "$SELFTEST" = 1 ]; then case "$0" in */*) ;; *) exec bash "$SELF" --selftest ;; esac   # the suite reruns "$0": give it a path
  run_selftest; exit $?
elif [ -n "$PROBE" ]; then run_probe "$PROBE"; exit $?
elif [ $# -ge 2 ]; then
  run_single "$1" "$2"; exit $?
else
  sed -n '/^# hands — /,/^set -u/{/^set -u/!p;}' "$0"; exit 2
fi
