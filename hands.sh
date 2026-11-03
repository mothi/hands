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
CWD="$PWD" TIMEOUT="" OUT="" EFFORT="" PROBE="" VERB=""
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

# classify a failure blob -> "status<TAB>reset". Pure function so tests/selftest.sh can feed the
# fixture corpus (tests/fixtures/) through the exact production regexes — ONE copy, seat.py's
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

# ---------- dispatch ----------
# SOURCE SEAM: seat.py sources this file for wt_alarm, the ONE reaper every seat process runs under; quota.sh
# for opencode_auth, the Go key's one path; tests/selftest.sh for the functions its rows call. A sourcing caller sets HANDS_SOURCED=1 and
# `set --` first; nothing below runs.
if [ -n "${HANDS_SOURCED:-}" ]; then return 0 2>/dev/null || exit 0; fi
case "$VERB" in   # the seat verbs (start is the buy below)
  send) [ "${#VERB_ARGS[@]}" -eq 2 ] || { echo "hands: usage: hands.sh send DIR FILE" >&2; exit 2; }
        seat_send "${VERB_ARGS[0]}" "${VERB_ARGS[1]}"; exit $? ;;
  mail|wait|events|stop) exec python3 "$SEATPY" "$VERB" ${VERB_ARGS[@]+"${VERB_ARGS[@]}"} ;;
esac
if [ -n "$PROBE" ]; then run_probe "$PROBE"; exit $?
elif [ $# -ge 2 ]; then
  run_single "$1" "$2"; exit $?
else
  sed -n '/^# hands — /,/^set -u/{/^set -u/!p;}' "$0"; exit 2
fi
