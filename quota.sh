#!/usr/bin/env bash
# quota — the quota board for claude|codex|grok|opencode: how much of each provider's pool is used, and when it
# resets. READ-ONLY: it never spends quota and never starts a seat, and hands never reads it before a buy.
# Endpoints are UNDOCUMENTED, reverse-engineered, liable to move.
# FAILURE CONTRACT: every failure degrades to status:"unknown"
# carrying its cause in `why`, and this script ALWAYS exits 0. No state: every run probes.
#
#   hands.sh quota [--json] [provider ...]   one line per provider (all four by default), or normalized JSON
#   quota.sh --selftest                      hermetic: PATH stubs + fixtures, zero network, zero model calls
#
# Redaction by construction: raw bodies are never printed, never persisted, never sent to
# stderr; the normalizer emits only pool/status/windows/binding/verdict. codex identity
# fields (accountId, reset-credit ids) die inside the normalizer — selftest 04 asserts.
# grok/opencode tokens are read transiently from each CLI's own file; the CLIs own
# refresh: a 401 here is "CLI hasn't refreshed", never an error to act on.
# CODEX READS NO CREDENTIAL AND CALLS NO ENDPOINT: its CLI's own
# app-server answers `account/rateLimits/read` from the login it refreshes itself (codex_body).
# CLAUDE READS NO CREDENTIAL AND CALLS NO ENDPOINT: its reading
# is the vendor's own `rate_limit_event`, the newest one in a claude job stream in the jobs
# root, $XDG_STATE_HOME/hands (an absolute one) else ~/.local/state/hands (claude_body reads seat.py's JOBS). The
# board shows its age; a window that has reset since the reading is dropped (its number is no longer true), and
# `hands.sh --probe claude` writes a fresh reading.
# The verdict is ADVISORY: one hard stop per pool for every window (100 non-claude, 90 claude); `short` labels
# duration only (codex 5h, opencode rolling).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
POOLS="" MODE=observe SELFTEST=0

while [ $# -gt 0 ]; do
  case "$1" in
    --json) MODE=json ;;
    --selftest) SELFTEST=1 ;;
    -h|--help) sed -n '/^# quota — /,/^set -u/p' "$0" | grep '^#'; exit 0 ;;
    claude|codex|grok|opencode) POOLS="$POOLS $1" ;;
    *) echo "quota: unknown arg $1 (ignored; see --help)" >&2 ;;  # contract: never nonzero
  esac
  shift
done
[ -n "$POOLS" ] || POOLS="claude codex grok opencode"

jqp() { python3 -c 'import sys,json;d=json.load(sys.stdin);'"$1" 2>/dev/null; }

# ---------- probes: echo the raw body, or {"error":"<why>"} — nothing else, no stderr
# claude: the vendor's OWN stream. Every claude seat's stream-json carries
# `rate_limit_event` lines whose rate_limit_info.unifiedWindows.{five_hour,seven_day} hold
# utilization (0-1) and resetsAt (epoch s). The reading is the NEWEST such event across the job
# streams in the jobs root — buys, replies and probes all land there — plus its age (the
# stream's mtime) and the windows that have reset since it was written. No reading at all is unknown;
# one claude call (hands.sh --probe claude) writes one.
# An event without unifiedWindows (pre-2.1.2xx CLIs) is not a reading. Each line is parsed as JSON
# and only its TOP-LEVEL object is read: a seat whose message quotes an event is text, never a
# reading, and a stream with no reading (a codex job written after the last claude one) is passed over.
claude_body() {
  python3 - "$HERE" <<'PY' 2>/dev/null || printf '{"error":"claude streams unreadable"}'
import glob, json, os, sys, time
sys.path.insert(0, sys.argv[1])
from seat import JOBS as root   # the jobs root's one python copy
now = time.time()
streams = []
for p in glob.glob(os.path.join(root, "*", "stream.jsonl")):
    try:
        streams.append((os.stat(p).st_mtime, p))
    except OSError:
        continue
for m, p in sorted(streams, reverse=True):
    try:
        data = open(p, "rb").read()
    except OSError:
        continue
    if b"unifiedWindows" not in data:   # fast path: most job streams hold no reading
        continue
    for line in reversed(data.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        info = e.get("rate_limit_info") if isinstance(e, dict) else None
        if not (isinstance(info, dict) and isinstance(info.get("unifiedWindows"), dict)):
            continue
        reset = sorted(k for k, w in info["unifiedWindows"].items() if isinstance(w, dict)
                       and isinstance(w.get("resetsAt"), (int, float)) and w["resetsAt"] <= now)
        print(json.dumps({"rate_limit_info": info, "age_s": int(now - m), "reset_since": reset}))
        sys.exit(0)
print(json.dumps({"error": "no claude reading in the jobs root; refresh: hands.sh --probe claude"}))
PY
}
# codex: the CLI's OWN app-server. One `account/rateLimits/read` over its stdio JSON-RPC, answered
# from the login the CLI refreshes itself, so codex has no stale-token class. The body is the read's result, or
# {"error": its JSON-RPC error} (no login: -32600 "codex account authentication required to read rate limits").
# stdin stays open until the answer: at stdin EOF the app-server drops a read still in flight (codex-cli
# 0.160). The CLI runs in its own process group, and ONE 20 s deadline, its exit included, kills that group:
# a CLI that hangs, or answers and never exits, never hangs the board.
codex_body() {
  python3 - <<'PY' 2>/dev/null || printf '{"error":"codex app-server unreadable"}'
import json, os, subprocess, threading
try:
    p = subprocess.Popen(["codex", "--no-daemon", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, start_new_session=True)
except OSError:
    print(json.dumps({"error": "no codex CLI on PATH"}))
    raise SystemExit(0)
deadline = threading.Timer(20, os.killpg, (p.pid, 9))
deadline.daemon = True
deadline.start()
try:
    for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hands-quota", "version": "1"}}},
              {"method": "initialized"}, {"id": 2, "method": "account/rateLimits/read"}):
        p.stdin.write((json.dumps(m) + "\n").encode())
    p.stdin.flush()
except OSError:
    pass
a = {}
for line in p.stdout:
    try:
        a = json.loads(line)
    except ValueError:
        continue
    if isinstance(a, dict) and a.get("id") == 2:
        break
    a = {}
try:
    p.stdin.close()
except OSError:
    pass
p.wait()
deadline.cancel()
if "result" in a:
    print(json.dumps(a["result"]))
else:
    # note 6: name the deadline only when it fired
    print(json.dumps({"error": a.get("error") or "codex app-server gave no answer (%s)"
                      % ("killed at the 20 s deadline" if p.returncode == -9 else "rc %s" % p.returncode)}))
PY
}
grok_body() {
  local tok
  tok=$(jqp 'print(list(d.values())[0]["key"])' <"$HOME/.grok/auth.json" 2>/dev/null)
  [ -n "$tok" ] || { printf '{"error":"no-token"}'; return; }
  # the bearer rides curl's stdin (-H @-), never its argv: ps shows every process's argv to every local user
  printf 'Authorization: Bearer %s\n' "$tok" | curl -sS --max-time 20 "https://cli-chat-proxy.grok.com/v1/billing?format=credits" \
    -H @- -H "x-grok-client-mode: build" 2>/dev/null \
    || printf '{"error":"http-failed"}'
}
opencode_body() {
  local key
  # the key file is hands.sh's opencode_auth, the ONE copy of its path (XDG_DATA_HOME first, as the
  # CLI resolves it) — sourced in a subshell, so none of hands.sh's globals or functions reach ours
  key=$(jqp 'print(d["opencode-go"]["key"])' <"$(HANDS_SOURCED=1; set --; . "$HERE/hands.sh"; opencode_auth)" 2>/dev/null)
  [ -n "$key" ] || { printf '{"error":"no-token"}'; return; }
  printf 'Authorization: Bearer %s\n' "$key" | curl -sS --max-time 20 https://opencode.ai/zen/go/v1/usage -H @- 2>/dev/null \
    || printf '{"error":"http-failed"}'
}
# ---------- normalize + verdict, one pass
run_gauge() {
  local cl="" cx="" gk="" oc="" p
  for p in $POOLS; do
    case "$p" in
      claude)   cl=$(claude_body) ;;
      codex)    cx=$(codex_body) ;;
      grok)     gk=$(grok_body) ;;
      opencode) oc=$(opencode_body) ;;
    esac
  done
  CL="$cl" CX="$cx" GK="$gk" OC="$oc" POOLS_ENV="$POOLS" MODE_ENV="$MODE" NOW_ENV="$(date +%s)" python3 <<'PY'
import json, os, re, sys, datetime

# (warn, hard) per pool, ADVISORY: the board marks a pool hot (would-warn) or at the provider's own hard stop
# (would-gate); nothing refuses a buy. Claude's hard stop is 90: the 10 above it is the user's interactive reserve.
# ONE threshold per pool for every window: a short window (codex 5h) only resets sooner.
THR = {"codex": (90, 100), "grok": (90, 100), "claude": (80, 90), "opencode": (90, 100)}
NOW = int(os.environ["NOW_ENV"])

def iso(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()

def win(name, used, resets, short):
    # absent used_pct => no window at all — never 0 (the absent-field rule)
    if used is None: return None
    return {"name": name, "used_pct": used, "resets_at": resets, "short": short}

def norm(pool, raw):
    if not raw: return {"pool": pool, "status": "unknown", "why": "not-probed", "windows": []}
    try: d = json.loads(raw)
    except Exception: return {"pool": pool, "status": "unknown", "why": "unparseable-body", "windows": []}
    if not isinstance(d, dict): return {"pool": pool, "status": "unknown", "why": "unparseable-body", "windows": []}
    if d.get("error"):
        # A DICT error used to flatten to the literal "provider-error-body",
        # discarding the only sentence that said what was wrong — six claude probes reported
        # that stub while the body plainly read "OAuth access token has expired". Surface the
        # provider text; a non-string message/type still falls back to the stub so `why`
        # stays a string and no body sub-structure leaks (header redaction invariant).
        er = d["error"]
        if isinstance(er, str):
            why = er
        elif isinstance(er, dict):
            why = next((v for v in (er.get("message"), er.get("type"))
                        if isinstance(v, str) and v), "provider-error-body")
        else:
            why = "provider-error-body"
        # Provider error text is UNTRUSTED — the
        # request carries a bearer token and a provider message could echo credential
        # material back. Mask sk-… prefixes and any 40+ char unbroken run (tokens are
        # long; meaningful slugs are not), cap at 200 chars, BEFORE the text reaches the
        # board line and --json. Applies to string errors too — that
        # exposure predates the DICT-error fix above.
        why = re.sub(r"sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{40,}", "…", why)[:200]
        return {"pool": pool, "status": "unknown", "why": why, "windows": []}
    ws = []
    scoped = []
    gone = sorted(d.get("reset_since") or [])   # claude: windows that reset after the reading; their numbers are not true
    if pool == "claude":
        # the newest rate_limit_event of a claude job stream (claude_body): utilization is 0-1.
        # Only the ACCOUNT windows gate: five_hour and seven_day, the two every claude stream carries.
        # Any other key is a SCOPED limit (seven_day_overage_included rides fable streams only: 9 of 9
        # recorded, 0 of 64 others) — it throttles one model, so it is DISPLAY-ONLY:
        # braces on the board, never a window, so verdict() and routing never read it.
        for k, w in ((d.get("rate_limit_info") or {}).get("unifiedWindows") or {}).items():
            if isinstance(w, dict) and k not in gone:
                u, r = w.get("utilization"), w.get("resetsAt")
                u = None if u is None else round(u * 100, 1)
                r = iso(r) if isinstance(r, (int, float)) else None
                if k in ("five_hour", "seven_day"): ws.append(win(k, u, r, k == "five_hour"))
                elif u is not None: scoped.append({"name": k[:40], "used_pct": u, "resets_at": r})
    elif pool == "codex":
        # the app-server's account bucket (codex_body): a window is short by windowDurationMins, never by its name
        # (primary has been weekly, then 5h, then weekly again); resetsAt is epoch seconds
        rl = d.get("rateLimits") or {}
        for k in ("primary", "secondary"):
            w = rl.get(k)
            if isinstance(w, dict):
                r = w.get("resetsAt")
                ws.append(win(k, w.get("usedPercent"), iso(r) if isinstance(r, (int, float)) else None,
                              (w.get("windowDurationMins") or 0) < 1440))
    elif pool == "grok":
        c = d.get("config") or {}
        # The proto3 JSON gateway omits ZERO-VALUED scalars: a fresh billing period has NO
        # creditUsagePercent key at all (a live drift, retained as
        # fixtures/quota-grok-fresh-period.json — the gauge read no-windows-in-body for 38
        # straight probes after the weekly rollover, i.e. blind exactly when the pool was
        # emptiest, and routing skipped grok as pool-unknown). Absent is
        # zero ONLY while the body still carries the period marker; an alien body still
        # yields no window and stays unknown — the absent-field rule's "never fabricate a 0" holds there.
        u = c.get("creditUsagePercent")
        if u is None and c.get("billingPeriodEnd"): u = 0.0
        ws.append(win("weekly", u, c.get("billingPeriodEnd"), False))
    elif pool == "opencode":
        u = d.get("usage") or {}
        for k in ("rolling", "weekly", "monthly"):
            w = u.get(k)
            if isinstance(w, dict): ws.append(win(k, w.get("percent"), w.get("resetsAt"), k == "rolling"))
    ws = [w for w in ws if w]
    if not ws:
        why = ("out of date: %s reset since the reading; refresh: hands.sh --probe claude" % ",".join(gone)
               if gone else "no-windows-in-body")
        return {"pool": pool, "status": "unknown", "why": why, "windows": []}
    e = {"pool": pool, "status": "ok", "why": None, "windows": ws}
    if gone: e["reset_since"] = gone
    if scoped: e["scoped"] = scoped
    if isinstance(d.get("age_s"), int): e["age_s"] = d["age_s"]   # a stream reading, not a live probe
    return e

def verdict(pool, entry):
    if entry["status"] != "ok": return "unknown", None, None
    warn, hard = THR.get(pool, (70, 90))
    binding, brk = None, -1.0
    routing, rrk = None, -1.0
    gate = warned = False
    for w in entry["windows"]:
        thr = hard
        r = (w["used_pct"] or 0) / thr
        # binding carries short + resets_at: a reader tells resets-soon from spent-for-the-window by them --
        # the window NAME alone is provider jargon (codex "primary" is short; nothing in the name says so)
        wd = {"name": w["name"], "used_pct": w["used_pct"], "threshold": thr,
              "short": w.get("short"), "resets_at": w.get("resets_at")}
        if r > brk: brk, binding = r, wd
        # routing: the LONG-window binding —
        # the number to compare across pools when picking the emptiest. Short windows
        # are pacing (they reset within hours and still set the verdict through `binding`);
        # when one fronted the board, a week-empty pool read as half-full and bulk work
        # routed to a genuinely fuller pool. The verdict stays on `binding`.
        if not w["short"] and r > rrk: rrk, routing = r, wd
        if w["used_pct"] >= thr: gate = True
        if not w["short"] and w["used_pct"] >= warn: warned = True
    return ("would-gate" if gate else "would-warn" if warned else "proceed"), binding, routing

raw = {"claude": os.environ["CL"], "codex": os.environ["CX"],
       "grok": os.environ["GK"], "opencode": os.environ["OC"]}
pools = os.environ["POOLS_ENV"].split()

out = []
for pool in pools:
    # Failure contract, enforced: a malformed body from ONE
    # provider must degrade THAT pool to unknown — it must never kill the gauge. Before
    # this, `config` as a list or string (AttributeError) or `creditUsagePercent` as a
    # string (TypeError) died with rc 0, EMPTY stdout: all four
    # pools blind at once, every consumer fail-open, looking exactly like success.
    try:
        e = norm(pool, raw.get(pool, ""))
        v, binding, routing = verdict(pool, e)
    except Exception:
        e = {"pool": pool, "status": "unknown", "why": "malformed-body", "windows": []}
        v, binding, routing = "unknown", None, None
    e["verdict"] = v
    e["binding"] = binding
    e["routing"] = routing
    out.append(e)

doc = {"probed_at": iso(NOW), "mode": "observe-only", "pools": out}

mode = os.environ["MODE_ENV"]
if mode == "json":
    print(json.dumps(doc, indent=1))
else:
    for e in out:
        if e["status"] == "ok" and e["binding"]:
            # headline = routing (the long window, the number pools compare by); the short
            # window rides in brackets ONLY when it is the hotter binding, so throttle
            # heat stays visible without fronting the board. Absent routing (a pool with
            # only short windows) falls back to binding — never fabricated, never a crash.
            b = e["binding"]
            rt = e.get("routing") or b
            line = (f"quota(observe): {e['pool']} used={rt['used_pct']}% "
                    f"({rt['name']}, thr {rt['threshold']})")
            if b["name"] != rt["name"]:
                line += f" [{b['name']} {b['used_pct']}% thr {b['threshold']}]"
            # braces = a scoped limit, display-only (brackets stay short-window heat)
            for s in e.get("scoped") or []:
                line += f" {{{s['name']} {s['used_pct']}%}}"
            line += f" verdict={e['verdict']}"
            a = e.get("age_s")   # a reading taken from a stream says how old it is, and how to refresh it
            if isinstance(a, int):
                age = f"{a}s" if a < 120 else f"{a // 60}m" if a < 7200 else f"{a // 3600}h"
                gone = e.get("reset_since")
                line += (f" (read {age} ago" + (f", before the {'+'.join(gone)} reset" if gone else "")
                         + "; refresh: hands.sh --probe claude)")
        elif e["status"] == "ok":
            # the same failure contract: a window whose ratio never exceeds the initial
            # -1.0 sentinel (percent <= -threshold) leaves binding None on an ok pool;
            # b['used_pct'] then killed the WHOLE print loop mid-output. Degrade the line,
            # never the loop — the containment comment says the gauge must never die.
            line = (f"quota(observe): {e['pool']} used=? "
                    f"(ok, no binding window) verdict={e['verdict']}")
        else:
            line = f"quota(observe): {e['pool']} UNKNOWN (why: {e['why']}) verdict=unknown"
        print(line)
PY
  return 0
}

# ---------- selftest: hermetic — PATH stubs for curl and the codex app-server (+ credential tripwires), HOME, TMPDIR and XDG_STATE_HOME sandbox
run_selftest() {
  local SB PASS=0 FAIL=0 FX="$HERE/tests/fixtures"
  SB="$(mktemp -d "${TMPDIR:-/tmp}/quota-selftest.XXXXXX")"
  # HERMETIC: a suite run inside a Claude Code session inherits the driver's inbox socket — unset suite-wide, so
  # nothing this suite starts can capture it (heals once posted into the live session)
  unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
  mkdir -p "$SB/bin" "$SB/home/.codex" "$SB/home/.grok" \
           "$SB/home/.local/share/opencode" "$SB/tmp" "$SB/state/hands/j1-claude"
  # the claude reading lives in job streams in the jobs root, $XDG_STATE_HOME/hands: EVERY row runs on the
  # sandbox's (a row that sets its own names it), so no row can read (or be fooled by) the real job dirs
  export TMPDIR="$SB/tmp" XDG_STATE_HOME="$SB/state"
  # the opencode key file resolves XDG_DATA_HOME first, then HOME (which every row pins): unset, no row
  # can reach a caller's store; row 47 sets it explicitly. The codex stub resolves CODEX_HOME before HOME, as the
  # real CLI does: unset too (row 49)
  unset XDG_DATA_HOME CODEX_HOME
  # j1: a fresh claude job stream — two readings, the LAST one wins (26/43, the old fixture values)
  python3 - "$SB/state/hands/j1-claude/stream.jsonl" <<'PY'
import json, sys, time
n = int(time.time())
ev = lambda f, s: {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": f, "resetsAt": n + 3600}, "seven_day": {"utilization": s, "resetsAt": n + 3 * 86400}}}}
with open(sys.argv[1], "w") as o:
    for e in ({"type": "system", "subtype": "init", "session_id": "s1"}, ev(0.20, 0.30), {"type": "assistant"},
              ev(0.26, 0.43), {"type": "result", "is_error": False, "result": "ok"}):
        o.write(json.dumps(e) + "\n")
PY
  # a codex login, to the codex stub below: no gauge row reads it (row 50 makes it unreadable)
  printf '{"tokens":{"access_token":"tok-test","account_id":"acct-test"}}' >"$SB/home/.codex/auth.json"
  printf '{"https://auth.x.ai::test-client":{"key":"tok-test"}}' >"$SB/home/.grok/auth.json"
  printf '{"opencode-go":{"key":"sk-test"}}' >"$SB/home/.local/share/opencode/auth.json"

  sed -e "s|@FX@|$FX|g" >"$SB/bin/curl" <<'STUB'
#!/bin/sh
# hermetic stub: URL -> fixture; QUOTA_STUB_<POOL>=<path>|FAIL overrides; never network
url=""; for a in "$@"; do case "$a" in http*) url="$a";; esac; done
[ -n "${QUOTA_CALL_LOG:-}" ] && echo "curl $url" >>"$QUOTA_CALL_LOG"
[ -n "${QUOTA_ARGV_LOG:-}" ] && echo "curl $*" >>"$QUOTA_ARGV_LOG"
# headers come as -H values, or from stdin when a value is @- (as the gauges pass the bearer)
hdrs=$(for a in "$@"; do echo "$a"; done; for a in "$@"; do [ "$a" = @- ] && cat; done)
[ -n "${QUOTA_AUTH_LOG:-}" ] && printf '%s\n' "$hdrs" | grep '^Authorization:' >>"$QUOTA_AUTH_LOG"
pick() { [ "$1" = FAIL ] && exit 6; cat "$1"; exit 0; }
case "$url" in
  *grok.com*)          pick "${QUOTA_STUB_GROK:-@FX@/quota-grok-happy.json}" ;;
  *opencode.ai*)       pick "${QUOTA_STUB_OPENCODE:-@FX@/quota-opencode-happy.json}" ;;
  *) exit 6 ;;
esac
STUB
  # the codex app-server as codex-cli 0.160 speaks codex_body's exchange: it answers
  # initialize, then account/rateLimits/read with the fixture's response member ({"result": …} or {"error": …});
  # a read whose stdin is at EOF by its answer is dropped unanswered, as the real one drops it; no auth.json
  # (CODEX_HOME, else HOME/.codex) answers the recorded no-login error. QUOTA_STUB_CODEX=<path>|FAIL|HANG; never network
  sed -e "s|@FX@|$FX|g" >"$SB/bin/codex" <<'STUB'
#!/usr/bin/env python3
import json, os, select, sys, time
if os.environ.get("QUOTA_CALL_LOG"):
    open(os.environ["QUOTA_CALL_LOG"], "a").write(" ".join(["codex"] + sys.argv[1:]) + "\n")
f = os.environ.get("QUOTA_STUB_CODEX") or "@FX@/quota-codex-happy.json"
if f == "FAIL": sys.exit(6)
if f == "HANG": time.sleep(3600)
linger = f == "LINGER"   # answers, then ignores stdin EOF (row 51)
if linger: f = "@FX@/quota-codex-happy.json"
# what the live server (0.160) sends between the initialize answer and the read answer, plus one non-JSON line
LIVE = "".join(json.dumps(n) + "\n" for n in (
    {"method": "remoteControl/status/changed", "params": {"environmentId": None, "installationId": "FAKEFIXTUREID0000",
                                                          "serverName": "quota-stub", "status": "disabled"}},
    {"method": "account/updated", "params": {"authMode": "chatgpt", "planType": "plus"}})) + "not json\n"
if not os.path.exists(os.path.join(os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex"), "auth.json")):
    f = "@FX@/quota-codex-no-login.json"
buf, eof = b"", False
while not eof:
    c = os.read(0, 65536)
    eof, buf = not c, buf + c
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        m = json.loads(line)
        if "id" not in m: continue
        if m["method"] == "initialize":
            sys.stdout.write(json.dumps({"id": m["id"], "result": {"userAgent": "quota-stub"}}) + "\n" + LIVE)
        else:   # the backend round trip (0.4-2.5 s live): a stdin at EOF by its end has shut the server down
            time.sleep(0.5)
            if select.select([0], [], [], 0)[0]:
                c = os.read(0, 65536)
                if not c: sys.exit(0)
                buf += c
            sys.stdout.write(json.dumps(dict(json.load(open(f)), id=m["id"])) + "\n")
        sys.stdout.flush()
        if linger and m["method"] != "initialize": time.sleep(3600)
STUB
  # TRIPWIRES: the claude gauge reads NO credential and calls NO endpoint. These stubs
  # only record that they were called; row 40 asserts a claude reading calls none of them.
  for c in security claude; do
    printf '#!/bin/sh\n[ -n "${QUOTA_CALL_LOG:-}" ] && echo "%s $*" >>"$QUOTA_CALL_LOG"\nexit 1\n' "$c" >"$SB/bin/$c"
  done
  chmod +x "$SB/bin/curl" "$SB/bin/codex" "$SB/bin/security" "$SB/bin/claude"

  q() { HOME="$SB/home" PATH="$SB/bin:$PATH" "$0" "$@"; }
  t() { local n="$1" d="$2"; shift 2
        if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); echo "ok  $n $d"
        else FAIL=$((FAIL+1)); echo "FAIL $n $d"; fi; }

  q --json >"$SB/o1.json" 2>/dev/null
  t 01 "happy path: all four pools ok" \
    sh -c "python3 -c 'import json,sys;d=json.load(open(\"$SB/o1.json\"));assert all(p[\"status\"]==\"ok\" for p in d[\"pools\"]) and len(d[\"pools\"])==4'"
  t 02 "claude (newest rate_limit_event of a job stream): both windows, utilization x100, resets present, five_hour short, age carried" \
    sh -c "python3 -c 'import json;p=[x for x in json.load(open(\"$SB/o1.json\"))[\"pools\"] if x[\"pool\"]==\"claude\"][0];w={x[\"name\"]:x for x in p[\"windows\"]};assert w[\"five_hour\"][\"used_pct\"]==26.0 and w[\"seven_day\"][\"used_pct\"]==43.0 and w[\"five_hour\"][\"short\"] and not w[\"seven_day\"][\"short\"] and w[\"five_hour\"][\"resets_at\"] and isinstance(p[\"age_s\"],int)'"
  t 03 "codex: primary 40%, long window, secondary absent, resets computed" \
    sh -c "python3 -c 'import json;p=[x for x in json.load(open(\"$SB/o1.json\"))[\"pools\"] if x[\"pool\"]==\"codex\"][0];w=p[\"windows\"];assert len(w)==1 and w[0][\"name\"]==\"primary\" and w[0][\"used_pct\"]==40 and not w[0][\"short\"] and w[0][\"resets_at\"]'"
  t 04 "REDACTION: codex identity fields never reach output" \
    sh -c "! grep -qE 'FAKEFIXTUREID|fixture-must-never-appear' '$SB/o1.json'"
  t 05 "grok: unified weekly 71% with period end" \
    sh -c "python3 -c 'import json;p=[x for x in json.load(open(\"$SB/o1.json\"))[\"pools\"] if x[\"pool\"]==\"grok\"][0];assert p[\"windows\"][0][\"used_pct\"]==71.0 and p[\"windows\"][0][\"resets_at\"]'"
  t 06 "opencode: three Go-plan windows parse; rolling is short and values are discriminating" \
    sh -c "python3 -c 'import json;p=[x for x in json.load(open(\"$SB/o1.json\"))[\"pools\"] if x[\"pool\"]==\"opencode\"][0];w={x[\"name\"]:x for x in p[\"windows\"]};assert w[\"rolling\"][\"used_pct\"]==0 and w[\"rolling\"][\"short\"] and w[\"weekly\"][\"used_pct\"]==8 and w[\"monthly\"][\"used_pct\"]==22'"
  t 07 "opencode SPA 404 shell is unknown/unparseable, never a fabricated zero" \
    sh -c "QUOTA_STUB_OPENCODE='$FX/quota-opencode-404.html' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' opencode --json | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"unparseable-body\" and not p[\"windows\"]'"
  # THE DICT-ERROR FIX MOVED THIS ROW: it used to assert the flattened "provider-error-body"
  # stub — i.e. it pinned the defect. Same body, same unknown, but `why` now carries the
  # provider sentence, so this row is the second independent witness for that fix.
  # the error-envelope parse is pool-agnostic; the body (an anthropic 429 envelope) rides the grok stub, since
  # codex calls no endpoint either (its CLI answers JSON-RPC errors, row 13)
  t 08 "an error-envelope body (429) → unknown carrying the provider message (not the provider-error-body stub)" \
    sh -c "QUOTA_STUB_GROK='$FX/quota-claude-429.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json | python3 -c 'import json,sys;p=[x for x in json.load(sys.stdin)[\"pools\"] if x[\"pool\"]==\"grok\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"Number of requests has exceeded your rate limit. Please try again later.\", p'"
  t 09 "absent usedPercent → window skipped entirely, pool unknown, NEVER fabricated as 0 (the absent-field rule)" \
    sh -c "QUOTA_STUB_CODEX='$FX/quota-codex-absent-pct.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json >'$SB/o9.json' && python3 -c 'import json;p=[x for x in json.load(open(\"$SB/o9.json\"))[\"pools\"] if x[\"pool\"]==\"codex\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"no-windows-in-body\" and p[\"windows\"]==[] and p[\"binding\"] is None'"
  # THE 100% RULE (every non-claude quota is spent to 100%), pinned to observable
  # output. Row 10 = the WARN arm (91 warns, routes on); row 30 = the old 97 gate is GONE
  # (98 only warns); row 30b = the hard stop is 100 — the one number this rule moved. Its
  # fixture is the 98 body re-valued to 100.0 in the sandbox, so the arm cannot drift.
  t 10 "verdicts under the 100 ruling: grok 91 → would-WARN (thr 100); codex 40 → proceed (thr 100)" \
    sh -c "QUOTA_STUB_GROK='$FX/quota-grok-hot.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o10.txt' && grep -q 'grok used=91.0% (weekly, thr 100) verdict=would-warn' '$SB/o10.txt' && grep -q 'codex used=40% (primary, thr 100) verdict=proceed' '$SB/o10.txt' && ! grep -q 'verdict=would-gate' '$SB/o10.txt'"
  t 30 "the 97 gate is GONE: grok 98 → would-WARN on thr 100 (spent, not refused)" \
    sh -c "QUOTA_STUB_GROK='$FX/quota-grok-gated.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o30.txt' && grep -q 'grok used=98.0% (weekly, thr 100) verdict=would-warn' '$SB/o30.txt'"
  sed 's/98\.0/100.0/g' "$FX/quota-grok-gated.json" >"$SB/grok-100.json"
  t 30b "the hard stop is 100: grok 100 → would-GATE on thr 100 (the arm the rule moved)" \
    sh -c "QUOTA_STUB_GROK='$SB/grok-100.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o30b.txt' && grep -q 'grok used=100.0% (weekly, thr 100) verdict=would-gate' '$SB/o30b.txt'"
  # Codex two-window era (a live drift; the fixture mirrors that probe): the
  # provider REPURPOSED primary from weekly to 5h and populated secondary (weekly) — names
  # are not stable, so classification MUST come from windowDurationMins, never the name.
  # Under the single hard stop (100) the 5h at 92 is hot, not gated: verdict proceed (short
  # windows never set warn), binding = primary (92/100 outranks 16/100) at thr 100. Row 43b
  # re-values the 5h to 100 -> would-gate with short=true (resets soon).
  # t 03 stays the witness for the single-window era body (both shapes must keep parsing).
  t 43 "codex 5h+weekly: primary SHORT by duration not name, 5h at 92 → proceed under thr 100 (weekly 16), binding=primary thr 100, canaries redacted" \
    sh -c "QUOTA_STUB_CODEX='$FX/quota-codex-two-window.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' codex --json >'$SB/o43.json' && python3 -c 'import json;t=open(\"$SB/o43.json\").read();p=[x for x in json.loads(t)[\"pools\"] if x[\"pool\"]==\"codex\"][0];w={x[\"name\"]:x for x in p[\"windows\"]};assert p[\"status\"]==\"ok\" and len(w)==2 and w[\"primary\"][\"used_pct\"]==92 and w[\"primary\"][\"short\"] and w[\"primary\"][\"resets_at\"] and w[\"secondary\"][\"used_pct\"]==16 and not w[\"secondary\"][\"short\"] and w[\"secondary\"][\"resets_at\"] and p[\"verdict\"]==\"proceed\" and p[\"binding\"][\"name\"]==\"primary\" and p[\"binding\"][\"threshold\"]==100 and \"FAKEFIXTUREID\" not in t and \"fixture-must-never-appear\" not in t, p'"
  sed 's/"usedPercent": 92/"usedPercent": 100/' "$FX/quota-codex-two-window.json" >"$SB/codex-5h-full.json"
  t 43b "a FULL short window gates at the same 100: codex 5h at 100 → would-gate, binding primary short=true thr 100" \
    sh -c "QUOTA_STUB_CODEX='$SB/codex-5h-full.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' codex --json | python3 -c 'import json,sys;p=[x for x in json.load(sys.stdin)[\"pools\"] if x[\"pool\"]==\"codex\"][0];b=p[\"binding\"];assert p[\"verdict\"]==\"would-gate\" and b[\"name\"]==\"primary\" and b[\"short\"] is True and b[\"threshold\"]==100 and b[\"used_pct\"]==100, p'"
  # Routing/binding split: the board headline
  # is the LONG-window used% — the number to compare across
  # pools when picking the emptiest — while `binding` (all windows) still owns the verdict. Live
  # failure that bought this: a pool week-empty at 8% fronted its five_hour 47 and building
  # work routed to a fuller pool. Row 44 = headline long + short bracketed + the JSON
  # contract; row 45 = the headline/bracket split is UNMOVED by the single threshold;
  # row 46 = an only-short-windows pool falls back to binding, no bracket, no crash.
  sed 's/"percent": 0,/"percent": 30,/' "$FX/quota-opencode-happy.json" >"$SB/oc-rolling-hot.json"
  t 44 "board headline is the LONG window: opencode rolling 30 binds → used=22% (monthly, the hotter long window) with rolling bracketed; JSON routing=monthly, binding=rolling" \
    sh -c "QUOTA_STUB_OPENCODE='$SB/oc-rolling-hot.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' opencode >'$SB/o44.txt' 2>&1 && grep -qF 'opencode used=22% (monthly, thr 100) [rolling 30% thr 100] verdict=proceed' '$SB/o44.txt' && QUOTA_STUB_OPENCODE='$SB/oc-rolling-hot.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' opencode --json | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"routing\"][\"name\"]==\"monthly\" and p[\"routing\"][\"used_pct\"]==22 and p[\"binding\"][\"name\"]==\"rolling\", p'"
  t 45 "…and the split is UNMOVED: headline = the weekly 16 with the 5h 92 bracketed; verdict proceed (short heat paces, it never gates below 100)" \
    sh -c "QUOTA_STUB_CODEX='$FX/quota-codex-two-window.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' codex >'$SB/o45.txt' 2>&1 && grep -qF 'codex used=16% (secondary, thr 100) [primary 92% thr 100] verdict=proceed' '$SB/o45.txt'"
  printf '{"result":{"rateLimits":{"primary":{"usedPercent":5,"windowDurationMins":300,"resetsAt":1794002569}}}}' >"$SB/codex-short-only.json"
  t 46 "a pool with ONLY short windows falls back to the binding headline — no bracket, no crash" \
    sh -c "QUOTA_STUB_CODEX='$SB/codex-short-only.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' codex >'$SB/o46.txt' 2>&1 && grep -qF 'codex used=5% (primary, thr 100) verdict=proceed' '$SB/o46.txt' && ! grep -qF ' [' '$SB/o46.txt'"
  t 13 "total failure (no auth anywhere, no claude stream) → all unknown, exit 0; claude has no reading, codex its CLI's own JSON-RPC no-login error, the rest no-token" \
    sh -c "HOME='$SB/empty' XDG_STATE_HOME='$SB/empty' PATH='$SB/bin:$PATH' '$0' --json >'$SB/o13.json'; [ \$? -eq 0 ] && python3 -c 'import json;d={p[\"pool\"]:p for p in json.load(open(\"$SB/o13.json\"))[\"pools\"]};assert all(p[\"status\"]==\"unknown\" for p in d.values()) and d[\"claude\"][\"why\"].startswith(\"no claude reading\") and d[\"codex\"][\"why\"]==\"codex account authentication required to read rate limits\" and all(d[k][\"why\"]==\"no-token\" for k in (\"grok\",\"opencode\"))'"
  t 15 "observe grammar: one verdict= line per pool" \
    sh -c "HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o15.txt' && [ \$(grep -c 'verdict=' '$SB/o15.txt') -eq 4 ]"
  t 16 "provider arguments subset the board: only those pools probed, in order" \
    sh -c "HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' codex grok --json | python3 -c 'import json,sys;d=json.load(sys.stdin);assert [p[\"pool\"] for p in d[\"pools\"]]==[\"codex\",\"grok\"]'"
  t 17 "unknown pool arg tolerated, exit 0 (contract)" \
    sh -c "HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --bogus-flag; [ \$? -eq 0 ]"
  t 18 "grok fresh period: proto3 omits the zero scalar → used=0 ok, NOT unknown (a live drift)" \
    sh -c "QUOTA_STUB_GROK='$FX/quota-grok-fresh-period.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json | python3 -c 'import json,sys;p=[x for x in json.load(sys.stdin)[\"pools\"] if x[\"pool\"]==\"grok\"][0];assert p[\"status\"]==\"ok\" and p[\"windows\"][0][\"used_pct\"]==0.0 and p[\"windows\"][0][\"resets_at\"]'"
  printf '{"config":{"somethingElse":1}}' >"$SB/grok-alien.json"
  t 19 "grok body WITHOUT the period marker stays unknown — absent is still never fabricated (the absent-field rule)" \
    sh -c "QUOTA_STUB_GROK='$SB/grok-alien.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json | python3 -c 'import json,sys;p=[x for x in json.load(sys.stdin)[\"pools\"] if x[\"pool\"]==\"grok\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"no-windows-in-body\"'"
  # failure contract: a body that CRASHES the parser degrades THAT pool to
  # unknown/malformed-body while every other pool stays live and the document still prints.
  # The containment wraps the per-pool CALL SITE, so it covers all four pools and both
  # crash sites (norm and verdict) at once; the rows deliberately SPAN pools and sites
  # (three same-pool bodies would test one branch thrice).
  # `why` stays the fixed literal "malformed-body": exception text can echo body-derived
  # values, and raw bodies never reach output (header invariant).
  printf '{"config":["x"]}' >"$SB/grok-cfg-list.json"
  mkdir -p "$SB/tmp21/hands/j"; printf '{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":"26","resetsAt":9999999999}}}}\n' >"$SB/tmp21/hands/j/stream.jsonl"
  printf '{"result":{"rateLimits":{"primary":{"usedPercent":40,"windowDurationMins":"300"}}}}' >"$SB/codex-secs-str.json"
  printf '{"config":{"creditUsagePercent":-1000,"billingPeriodEnd":"2026-08-26T00:00:00+00:00"}}' >"$SB/grok-neg-pct.json"
  t 25 "percent <= -threshold (binding None on an ok pool): all FOUR observe lines still print, rc 0" \
    sh -c "QUOTA_STUB_GROK='$SB/grok-neg-pct.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o25.txt' 2>&1 && [ \$(grep -c 'quota(observe):' '$SB/o25.txt') -eq 4 ] && grep -q 'grok used=? (ok, no binding window)' '$SB/o25.txt'"
  t 20 "grok config-as-list (AttributeError in norm) → ONLY grok malformed-body; others live; the document prints" \
    sh -c "QUOTA_STUB_GROK='$SB/grok-cfg-list.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json >'$SB/s20.json' 2>/dev/null && python3 -c 'import json;d=json.load(open(\"$SB/s20.json\"));p={x[\"pool\"]:x for x in d[\"pools\"]};g=p[\"grok\"];assert g[\"status\"]==\"unknown\" and g[\"why\"]==\"malformed-body\" and p[\"claude\"][\"status\"]==\"ok\" and p[\"codex\"][\"status\"]==\"ok\"'"
  t 21 "claude utilization-as-string (TypeError in norm) degrades to malformed-body, exit 0" \
    sh -c "XDG_STATE_HOME='$SB/tmp21' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json | python3 -c 'import json,sys;p=[x for x in json.load(sys.stdin)[\"pools\"] if x[\"pool\"]==\"claude\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"malformed-body\"'"
  t 22 "codex windowDurationMins-as-string (TypeError in norm, the fourth crash site) degrades on the observe line" \
    sh -c "QUOTA_STUB_CODEX='$SB/codex-secs-str.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' >'$SB/o22.txt' 2>&1; grep -q 'codex UNKNOWN (why: malformed-body)' '$SB/o22.txt'"
  # Provider text is untrusted — a message echoing credential material
  # must reach the board masked (pool-agnostic; the body rides the grok stub). Synthetic body
  # (NOT a captured stream, so it lives in the sandbox, not fixtures/): an sk-ant token, a 44-char run.
  printf '{"type":"error","error":{"type":"auth_error","message":"Bad token sk-ant-oat01-LEAKLEAKLEAKLEAK and id 0123456789012345678901234567890123456789ABCD were rejected. Re-authenticate."}}' >"$SB/leak-body.json"
  t 29 "provider error message with token material → why is MASKED (no sk-, no 40+ run) and capped, prose kept" \
    sh -c "QUOTA_STUB_GROK='$SB/leak-body.json' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json grok | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];w=p[\"why\"];assert p[\"status\"]==\"unknown\" and \"sk-ant\" not in w and \"LEAK\" not in w and \"0123456789\" not in w and \"Re-authenticate\" in w and len(w)<=200, p'"

  # THE CLAUDE READING: the newest top-level rate_limit_event across claude job
  # streams, at any age; a window that reset after it is dropped. Each row is falsified by reverting one rule of claude_body.
  mk() { # $1 dir  $2 age-seconds (mtime)  $3.. json lines
    local d="$1" a="$2"; shift 2; mkdir -p "$d"; printf '%s\n' "$@" >"$d/stream.jsonl"
    python3 -c 'import os,sys,time;t=time.time()-int(sys.argv[2]);os.utime(sys.argv[1],(t,t))' "$d/stream.jsonl" "$a"
  }
  F5=$(( $(date +%s) + 3600 )); F7=$(( $(date +%s) + 259200 ))
  R() { printf '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":{"five_hour":{"utilization":%s,"resetsAt":%s},"seven_day":{"utilization":%s,"resetsAt":%s}}}}' "$1" "${3:-$F5}" "$2" "$F7"; }
  T=$SB/t34; mk "$T/hands/old" 600 "$(R 0.90 0.90)"; mk "$T/hands/new" 30 "$(R 0.11 0.12)" '{"type":"assistant"}' "$(R 0.15 0.16)"
  t 34 "the NEWEST reading wins: the most recently written stream (a 10-minute-older one reads 90), and in it the LAST event (15, not 11)" \
    sh -c "XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];w={x[\"name\"]:x[\"used_pct\"] for x in p[\"windows\"]};assert w==dict(five_hour=15.0,seven_day=16.0), p'"
  T=$SB/t35; mk "$T/hands/claude" 300 "$(R 0.21 0.22)"
  mk "$T/hands/codex" 5 "$(printf '{"type":"item.completed","item":{"type":"agent_message","text":"%s"}}' "$(R 0.99 0.99 | sed 's/"/\\"/g')")"
  t 35 "a stream that only QUOTES a rate_limit_event (a codex seat discussing one) is text, never a reading: the older real claude stream wins (21, not 99)" \
    sh -c "XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];w={x[\"name\"]:x[\"used_pct\"] for x in p[\"windows\"]};assert w[\"five_hour\"]==21.0, p'"
  T=$SB/t36; mk "$T/hands/claude" 7200 "$(R 0.30 0.40)"
  t 36 "an OLD reading is still shown, with its age in hours and how to refresh it -- never hidden" \
    sh -c "XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' claude >'$SB/o36.txt' && grep -qxF 'quota(observe): claude used=40.0% (seven_day, thr 90) verdict=proceed (read 2h ago; refresh: hands.sh --probe claude)' '$SB/o36.txt'"
  T=$SB/t37; mk "$T/hands/claude" 60 "$(R 0.95 0.40 $(( $(date +%s) - 10 )))"
  t 37 "a reading that PREDATES a window reset drops that window (a 95% five_hour that has since reset is no longer true) and keeps the rest, the line marked out of date" \
    sh -c "XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"status\"]==\"ok\" and [w[\"name\"] for w in p[\"windows\"]]==[\"seven_day\"] and p[\"reset_since\"]==[\"five_hour\"], p' && XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' claude | grep -qF 's ago, before the five_hour reset; refresh: hands.sh --probe claude)'"
  T=$SB/t38; mk "$T/hands/b" 300 "$(R 0.31 0.32)"; mk "$T/hands/a" 10 '{"type":"assistant","message":{"content":[{"type":"text","text":"the gauge reads unifiedWindows"}]}}' '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1788303000,"rateLimitType":"five_hour"}}'
  t 38 "an event WITHOUT unifiedWindows (the pre-2.1.2xx shape, fixtures/claude-stream-sample.jsonl) is not a reading, even in a stream whose text mentions the field: the next stream that has one is read" \
    sh -c "XDG_STATE_HOME='$T' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];w={x[\"name\"]:x[\"used_pct\"] for x in p[\"windows\"]};assert w[\"five_hour\"]==31.0, p'"
  t 39 "the board line carries the reading's AGE and its refresh after the verdict (a stream reading is not a live probe)" \
    sh -c "q() { HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' \"\$@\"; }; q claude | grep -qE '^quota\\(observe\\): claude used=43.0% \\(seven_day, thr 90\\) verdict=proceed \\(read [0-9]+s ago; refresh: hands.sh --probe claude\\)\$'"
  : >"$SB/calls.log"
  QUOTA_CALL_LOG="$SB/calls.log" HOME="$SB/home" PATH="$SB/bin:$PATH" "$0" --json claude >"$SB/o40.json" 2>/dev/null
  t 40 "the claude reading reads NO credential and calls NO endpoint: no keychain (security), no curl, no claude --version — and it is still ok" \
    sh -c "[ ! -s '$SB/calls.log' ] && python3 -c 'import json;p=json.load(open(\"$SB/o40.json\"))[\"pools\"][0];assert p[\"status\"]==\"ok\", p'"
  # THE WIRE, NOT THE STUB: replay() copies a RECORDED claude stream into a sandbox job dir. Only the
  # clock moves: every unifiedWindows resetsAt shifts by one constant, so the earliest lands an hour
  # ahead and the reading predates no reset; every other byte of the stream is the recorded one.
  replay() { # $1 fixture  $2 sandbox XDG_STATE_HOME — the stream lands at $2/hands/replay-claude/stream.jsonl
    mkdir -p "$2/hands/replay-claude"
    python3 - "$1" "$2/hands/replay-claude/stream.jsonl" <<'PY'
import json, sys, time
lines = open(sys.argv[1], "rb").read().splitlines(True)
evs = [json.loads(l) for l in lines]
first = min(w["resetsAt"] for e in evs if e.get("type") == "rate_limit_event"
            for w in e["rate_limit_info"]["unifiedWindows"].values())
shift = max(0, int(time.time()) + 3600 - first)
with open(sys.argv[2], "wb") as o:
    for l, e in zip(lines, evs):
        if e.get("type") == "rate_limit_event":
            for w in e["rate_limit_info"]["unifiedWindows"].values(): w["resetsAt"] += shift
            l = (json.dumps(e) + "\n").encode()
        o.write(l)
PY
  }
  # fixtures/claude-stream-ratelimit.jsonl: a live claude 2.1.300 probe stream (hook events
  # before init, thinking, the rate_limit_event, a post-turn summary)
  replay "$FX/claude-stream-ratelimit.jsonl" "$SB/t41"
  t 41 "the claude reading off a REAL recorded stream (fixtures/claude-stream-ratelimit.jsonl): five_hour 12.0 short, seven_day 37.0, verdict proceed" \
    sh -c "XDG_STATE_HOME='$SB/t41' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];w={x[\"name\"]:(x[\"used_pct\"],x[\"short\"]) for x in p[\"windows\"]};assert p[\"status\"]==\"ok\" and w==dict(five_hour=(12.0,True),seven_day=(37.0,False)) and p[\"verdict\"]==\"proceed\", p'"
  # 47: the opencode key file has ONE resolver, hands.sh's opencode_auth: XDG_DATA_HOME first,
  # as opencode resolves it — opencode's own auth.json, the store every opencode seat runs on. Falsification: read
  # $HOME/.local/share again (the gauge's old private copy of the path) → the bearer is the HOME key.
  mkdir -p "$SB/xdg47/opencode"; printf '{"opencode-go":{"key":"sk-xdg47"}}' >"$SB/xdg47/opencode/auth.json"; : >"$SB/auth47.log"
  t 47 "the opencode gauge reads the key file through hands.sh's resolver: XDG_DATA_HOME wins over HOME (the bearer is the XDG store's key)" \
    sh -c "QUOTA_AUTH_LOG='$SB/auth47.log' XDG_DATA_HOME='$SB/xdg47' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' opencode >/dev/null 2>&1 && [ \"\$(cat '$SB/auth47.log')\" = 'Authorization: Bearer sk-xdg47' ]"
  # 48: THE SCOPED LIMIT NEVER GATES (pinned on the stream wire). The recorded
  # fable event (fixtures/claude-stream-fable-scoped.jsonl) carries seven_day_overage_included
  # at 97 beside the account windows five_hour 15 and seven_day 58. Falsification: gate on every
  # unifiedWindows key again → would-gate at 97, no braces.
  replay "$FX/claude-stream-fable-scoped.jsonl" "$SB/t48"
  t 48 "a scoped limit is DISPLAY-ONLY: the recorded fable wire (seven_day_overage_included 97, seven_day 58) reads verdict proceed, headline seven_day, the scoped 97 in braces and in JSON scoped, never a window" \
    sh -c "XDG_STATE_HOME='$SB/t48' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' claude >'$SB/o48.txt' 2>&1 && grep -qE '^quota\\(observe\\): claude used=58.0% \\(seven_day, thr 90\\) \\{seven_day_overage_included 97.0%\\} verdict=proceed \\(read ' '$SB/o48.txt' && XDG_STATE_HOME='$SB/t48' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json claude | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"verdict\"]==\"proceed\" and sorted(x[\"name\"] for x in p[\"windows\"])==[\"five_hour\",\"seven_day\"] and p[\"scoped\"]==[dict(name=\"seven_day_overage_included\",used_pct=97.0,resets_at=p[\"scoped\"][0][\"resets_at\"])] and p[\"scoped\"][0][\"resets_at\"], p'"
  # 50: THE CODEX READING: its CLI's own app-server answers, so the gauge calls no endpoint and reads no
  # credential. Falsification: restore the auth.json read + curl to chatgpt.com -> curl in the log, the unreadable login no-token.
  mkdir -p "$SB/home50/.codex"; cp "$SB/home/.codex/auth.json" "$SB/home50/.codex/"; chmod 000 "$SB/home50/.codex/auth.json"; : >"$SB/calls50.log"
  t 50 "the codex reading calls NO endpoint and reads NO credential: one codex --no-daemon app-server answers, no curl, and an unreadable auth.json still reads ok" \
    sh -c "QUOTA_CALL_LOG='$SB/calls50.log' HOME='$SB/home50' PATH='$SB/bin:$PATH' '$0' --json codex | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"status\"]==\"ok\" and p[\"windows\"][0][\"used_pct\"]==40, p' && [ \"\$(cat '$SB/calls50.log')\" = 'codex --no-daemon app-server' ]"
  # 51: the failure contract on the codex wire: a CLI that exits unanswered is unknown carrying its rc; one that never
  # answers, or answers and never exits (LINGER, run beside HANG), is ended by the one 20 s deadline (its group killed);
  # the gauge exits 0 and no CLI process is left. Falsification: drop the deadline's start, or start_new_session (killpg
  # then finds no group) -> HANG runs into the 40 s alarm; cancel the deadline before the exit wait -> LINGER does.
  t 51 "a codex app-server that exits unanswered (rc 6), never answers, or answers and never exits is bounded by the one 20 s deadline (its group killed): unknown or ok, exit 0, no process left" \
    sh -c "QUOTA_STUB_CODEX=FAIL HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' --json codex | python3 -c 'import json,sys;p=json.load(sys.stdin)[\"pools\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"codex app-server gave no answer (rc 6)\", p' || exit 1; s=\$(date +%s); QUOTA_STUB_CODEX=LINGER HOME='$SB/home' PATH='$SB/bin:$PATH' perl -e 'alarm shift; exec @ARGV' 40 '$0' --json codex >'$SB/o51l.json' & l=\$!; QUOTA_STUB_CODEX=HANG HOME='$SB/home' PATH='$SB/bin:$PATH' perl -e 'alarm shift; exec @ARGV' 40 '$0' --json codex >'$SB/o51.json' && wait \$l && e=\$((\$(date +%s)-s)) && [ \$e -ge 19 ] && [ \$e -le 30 ] && python3 -c 'import json;p=json.load(open(\"$SB/o51.json\"))[\"pools\"][0];assert p[\"status\"]==\"unknown\" and p[\"why\"]==\"codex app-server gave no answer (killed at the 20 s deadline)\", p' && python3 -c 'import json;p=json.load(open(\"$SB/o51l.json\"))[\"pools\"][0];assert p[\"status\"]==\"ok\" and p[\"windows\"][0][\"used_pct\"]==40, p' && ! pgrep -f '$SB/bin/[c]odex'"
  # 52: the grok and opencode bearers reach curl on its stdin (-H @-), never in its argv, which ps shows every local
  # user. Falsification: pass either as -H "Authorization: Bearer ..." -> it is in the argv log
  t 52 "THE BEARER NEVER RIDES ARGV: the grok and opencode gauges hand their token to curl on stdin -- both reach the header, neither is in curl's argv" \
    sh -c "QUOTA_ARGV_LOG='$SB/argv52' QUOTA_AUTH_LOG='$SB/auth52' HOME='$SB/home' PATH='$SB/bin:$PATH' '$0' grok opencode >/dev/null 2>&1 && grep -qx 'Authorization: Bearer tok-test' '$SB/auth52' && grep -qx 'Authorization: Bearer sk-test' '$SB/auth52' && [ -s '$SB/argv52' ] && ! grep -q 'Bearer' '$SB/argv52'"
  # 49: falsification: drop a suite-wide unset and run the suite with that name exported -> red.
  t 49 "HERMETIC SUITE: the suite's env carries neither driver-inbox name nor CODEX_HOME, whatever env the suite was started in" \
    sh -c "[ -z \"\${CLAUDE_CODE_MESSAGING_SOCKET+x}\${CLAUDE_CODE_MESSAGING_TOKEN+x}\${CODEX_HOME+x}\" ]"

  rm -rf "$SB"
  echo "selftest: $PASS passed, $FAIL failed"
  [ "$FAIL" -eq 0 ]
}

if [ "$SELFTEST" = 1 ]; then case "$0" in */*) ;; *) exec bash "$HERE/$(basename "$0")" --selftest ;; esac   # the suite reruns "$0": give it a path
  run_selftest; exit $?; fi
run_gauge
exit 0
