#!/usr/bin/env python3
"""Hermetic tests for quota.py: stub curl and codex CLIs on PATH answer from fixtures/, in a
sandbox HOME and jobs root. No network, no credential, no model call.
Run: python3 tests/test_quota.py"""
import json, os, subprocess, sys, tempfile, time, unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
QUOTA = os.path.join(os.path.dirname(TESTS), 'quota.py')
FX = os.path.join(TESTS, 'fixtures')
POOLS = ('claude', 'codex', 'grok', 'opencode')

# A run inside a Claude Code session inherits the driver's inbox socket; nothing here may reach it.
# opencode's key file resolves XDG_DATA_HOME first and the codex stub CODEX_HOME before HOME, as
# the real CLIs do: unset, so no test reads a real login (test_47 sets XDG_DATA_HOME itself).
for _name in ('CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'XDG_DATA_HOME',
              'CODEX_HOME'):
    os.environ.pop(_name, None)

# curl: the URL picks a fixture; QUOTA_STUB_<POOL>=<path>|FAIL overrides it. Headers come as -H
# values, or on stdin when a value is @- (as quota.py passes the bearer).
CURL = r'''#!/bin/sh
url=""; for a in "$@"; do case "$a" in http*) url="$a";; esac; done
[ -n "${QUOTA_CALL_LOG:-}" ] && echo "curl $url" >>"$QUOTA_CALL_LOG"
[ -n "${QUOTA_ARGV_LOG:-}" ] && echo "curl $*" >>"$QUOTA_ARGV_LOG"
hdrs=$(for a in "$@"; do echo "$a"; done; for a in "$@"; do [ "$a" = @- ] && cat; done)
[ -n "${QUOTA_AUTH_LOG:-}" ] && printf '%s\n' "$hdrs" | grep '^Authorization:' >>"$QUOTA_AUTH_LOG"
pick() { [ "$1" = FAIL ] && exit 6; cat "$1"; exit 0; }
case "$url" in
  *grok.com*)    pick "${QUOTA_STUB_GROK:-@FX@/quota-grok-happy.json}" ;;
  *opencode.ai*) pick "${QUOTA_STUB_OPENCODE:-@FX@/quota-opencode-happy.json}" ;;
  *) exit 6 ;;
esac
'''

# codex: the app-server as codex-cli 0.160 speaks quota.py's exchange. It answers initialize,
# then account/rateLimits/read with the fixture's {"result": ...} or {"error": ...}. A read whose
# stdin is at EOF by its answer is dropped, as the real one drops it. No auth.json answers the
# recorded no-login error. QUOTA_STUB_CODEX=<path>|FAIL|HANG|LINGER.
CODEX = r'''#!/usr/bin/env python3
import json, os, select, sys, time
if os.environ.get("QUOTA_CALL_LOG"):
    open(os.environ["QUOTA_CALL_LOG"], "a").write(" ".join(["codex"] + sys.argv[1:]) + "\n")
f = os.environ.get("QUOTA_STUB_CODEX") or "@FX@/quota-codex-happy.json"
if f == "FAIL": sys.exit(6)
if f == "HANG": time.sleep(3600)
linger = f == "LINGER"   # answers, then ignores stdin EOF
if linger: f = "@FX@/quota-codex-happy.json"
# what the live server sends between the initialize answer and the read answer, and a non-JSON line
LIVE = "".join(json.dumps(n) + "\n" for n in (
    {"method": "remoteControl/status/changed", "params": {"installationId": "FAKEFIXTUREID0000",
                                                          "status": "disabled"}},
    {"method": "account/updated", "params": {"authMode": "chatgpt"}})) + "not json\n"
home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
if not os.path.exists(os.path.join(home, "auth.json")):
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
            answer = {"id": m["id"], "result": {"userAgent": "stub"}}
            sys.stdout.write(json.dumps(answer) + "\n" + LIVE)
        else:   # the backend round trip: a stdin at EOF by its end has shut the server down
            time.sleep(0.5)
            if select.select([0], [], [], 0)[0]:
                c = os.read(0, 65536)
                if not c: sys.exit(0)
                buf += c
            sys.stdout.write(json.dumps(dict(json.load(open(f)), id=m["id"])) + "\n")
        sys.stdout.flush()
        if linger and m["method"] != "initialize": time.sleep(3600)
'''

# Tripwires: the claude reading reads no credential and calls nothing. These only log a call.
TRIPWIRE = '#!/bin/sh\n[ -n "${QUOTA_CALL_LOG:-}" ] && echo "%s $*" >>"$QUOTA_CALL_LOG"\nexit 1\n'


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def rate_event(five_hour, seven_day, five_hour_resets=None):
    """A claude rate_limit_event; utilizations are 0-1, resets an hour and three days ahead."""
    now = int(time.time())
    return json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed', 'unifiedWindows': {
            'five_hour': {'utilization': five_hour, 'resetsAt': five_hour_resets or now + 3600},
            'seven_day': {'utilization': seven_day, 'resetsAt': now + 3 * 86400}}}})


def stream(path, age, *lines):
    """A job stream.jsonl holding lines, last written age seconds ago."""
    write(path, ''.join(line + '\n' for line in lines))
    when = time.time() - age
    os.utime(path, (when, when))


def replay(fixture, state):
    """A recorded claude stream as a job stream under state. Only the clock moves: every resetsAt
    shifts by one constant, so the earliest lands an hour ahead and no window has reset."""
    with open(fixture, 'rb') as f:
        lines = f.read().splitlines(True)
    events = [json.loads(line) for line in lines]
    first = min(w['resetsAt'] for e in events if e.get('type') == 'rate_limit_event'
                for w in e['rate_limit_info']['unifiedWindows'].values())
    shift = max(0, int(time.time()) + 3600 - first)
    path = os.path.join(state, 'hands', 'replay-claude', 'stream.jsonl')
    os.makedirs(os.path.dirname(path))
    with open(path, 'wb') as out:
        for line, event in zip(lines, events):
            if event.get('type') == 'rate_limit_event':
                for w in event['rate_limit_info']['unifiedWindows'].values():
                    w['resetsAt'] += shift
                line = (json.dumps(event) + '\n').encode()
            out.write(line)


class Quota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='quota-test.')
        sb = cls.sb = cls.tmp.name
        for name, text in (('curl', CURL), ('codex', CODEX)):
            write(os.path.join(sb, 'bin', name), text.replace('@FX@', FX), 0o755)
        for name in ('security', 'claude'):
            write(os.path.join(sb, 'bin', name), TRIPWIRE % name, 0o755)
        write(os.path.join(sb, 'home', '.codex', 'auth.json'),
              '{"tokens":{"access_token":"tok-test","account_id":"acct-test"}}')
        write(os.path.join(sb, 'home', '.grok', 'auth.json'),
              '{"https://auth.x.ai::test-client":{"key":"tok-test"}}')
        write(os.path.join(sb, 'home', '.local', 'share', 'opencode', 'auth.json'),
              '{"opencode-go":{"key":"sk-test"}}')
        # the default jobs root: a fresh claude stream with two readings, the last one wins
        stream(os.path.join(sb, 'state', 'hands', 'j1-claude', 'stream.jsonl'), 0,
               '{"type":"system","subtype":"init","session_id":"s1"}', rate_event(0.20, 0.30),
               '{"type":"assistant"}', rate_event(0.26, 0.43), '{"type":"result","result":"ok"}')
        os.makedirs(os.path.join(sb, 'tmp'))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.sb, *parts)

    def run_quota(self, *args, **env):
        base = {'HOME': self.path('home'), 'PATH': self.path('bin') + ':' + os.environ['PATH'],
                'TMPDIR': self.path('tmp'), 'XDG_STATE_HOME': self.path('state')}
        done = subprocess.run([sys.executable, QUOTA] + list(args), capture_output=True,
                              text=True, timeout=90, env=dict(os.environ, **dict(base, **env)))
        self.assertEqual(done.returncode, 0, done.stderr)   # quota always exits 0
        return done.stdout

    def pools(self, *args, **env):
        """The --json document's pools, by name."""
        doc = json.loads(self.run_quota('--json', *args, **env))
        return {p['pool']: p for p in doc['pools']}

    def windows(self, reading):
        return [(w['name'], w['used_pct']) for w in reading['windows']]

    def test_01_all_four_pools_read(self):
        pools = self.pools()
        self.assertEqual(list(pools), list(POOLS))
        self.assertTrue(all(p['status'] == 'ok' for p in pools.values()), pools)

    def test_02_claude_reads_the_last_event_of_its_stream(self):
        claude = self.pools('claude')['claude']
        self.assertEqual(self.windows(claude), [('week', 43.0), ('5h', 26.0)])   # x100, week first
        self.assertTrue(all(w['resets_at'] for w in claude['windows']))
        self.assertIsInstance(claude['age_s'], int)

    def test_03_codex_window_is_named_by_its_length(self):
        codex = self.pools('codex')['codex']
        self.assertEqual(self.windows(codex), [('week', 40)])   # 10080 minutes, secondary null
        self.assertTrue(codex['windows'][0]['resets_at'])

    def test_04_codex_identity_never_reaches_output(self):
        for args in (('--json',), ()):
            out = self.run_quota(*args)
            self.assertNotRegex(out, 'FAKEFIXTUREID|fixture-must-never-appear')

    def test_05_grok(self):
        grok = self.pools('grok')['grok']
        self.assertEqual(self.windows(grok), [('week', 71.0)])
        self.assertTrue(grok['windows'][0]['resets_at'])

    def test_06_opencode_lists_its_three_windows_longest_first(self):
        opencode = self.pools('opencode')['opencode']
        self.assertEqual(self.windows(opencode), [('month', 22), ('week', 8), ('rolling', 0)])

    def test_07_a_reply_that_is_not_json_is_unknown(self):   # opencode's 404 page
        opencode = self.pools('opencode', QUOTA_STUB_OPENCODE=FX + '/quota-opencode-404.html')
        self.assertEqual(opencode['opencode']['status'], 'unknown')
        self.assertEqual(opencode['opencode']['why'], 'the reply is not JSON')
        self.assertEqual(opencode['opencode']['windows'], [])

    def test_08_an_error_reply_carries_the_provider_message(self):
        # the parse is the same for every pool: an Anthropic 429 envelope, served as grok's reply
        grok = self.pools('grok', QUOTA_STUB_GROK=FX + '/quota-claude-429.json')['grok']
        self.assertEqual(grok['status'], 'unknown')
        self.assertEqual(grok['why'], 'Number of requests has exceeded your rate limit. '
                                      'Please try again later.')

    def test_09_an_absent_percentage_is_no_window_never_zero(self):
        codex = self.pools('codex', QUOTA_STUB_CODEX=FX + '/quota-codex-absent-pct.json')['codex']
        self.assertEqual((codex['status'], codex['why']),
                         ('unknown', 'the reply has no usage windows'))

    def test_43_codex_windows_are_told_apart_by_length_not_name(self):
        # primary has been the weekly window, then the 5-hour one: only the length is stable
        out = self.run_quota('--json', 'codex',
                             QUOTA_STUB_CODEX=FX + '/quota-codex-two-window.json')
        codex = json.loads(out)['pools'][0]
        self.assertEqual(self.windows(codex), [('week', 16), ('5h', 92)])
        self.assertNotRegex(out, 'FAKEFIXTUREID|fixture-must-never-appear')

    def test_44_the_board_line_lists_windows_longest_first(self):
        out = self.run_quota('opencode')
        self.assertRegex(out, r'^opencode  month 22%.*, week 8%.*, rolling 0%')

    def test_45_the_board_line_for_two_codex_windows(self):   # both reset in the past: no "resets"
        out = self.run_quota('codex', QUOTA_STUB_CODEX=FX + '/quota-codex-two-window.json')
        self.assertEqual(out, 'codex     week 16%, 5h 92%\n')

    def test_13_nothing_to_read_is_all_unknown(self):
        empty = self.path('empty')
        pools = self.pools(HOME=empty, XDG_STATE_HOME=empty)
        self.assertTrue(all(p['status'] == 'unknown' for p in pools.values()), pools)
        self.assertTrue(pools['claude']['why'].startswith('no claude reading'))
        self.assertEqual(pools['codex']['why'],
                         'codex account authentication required to read rate limits')
        self.assertTrue(pools['grok']['why'].startswith('no login in '))
        self.assertTrue(pools['opencode']['why'].startswith('no login in '))

    def test_15_one_board_line_per_pool(self):
        lines = self.run_quota().splitlines()
        self.assertEqual([line.split()[0] for line in lines], list(POOLS))

    def test_16_provider_arguments_pick_the_pools_in_order(self):
        self.assertEqual(list(self.pools('codex', 'grok')), ['codex', 'grok'])

    def test_17_an_unknown_argument_is_ignored(self):
        self.assertEqual(len(self.run_quota('--bogus-flag').splitlines()), 4)   # and exit 0

    def test_18_a_new_grok_period_reads_zero(self):
        # the gateway leaves out a zero value: the period's end shows the reply is a real one
        grok = self.pools('grok', QUOTA_STUB_GROK=FX + '/quota-grok-fresh-period.json')['grok']
        self.assertEqual((grok['status'], self.windows(grok)), ('ok', [('week', 0.0)]))

    def test_19_a_grok_reply_without_its_period_stays_unknown(self):
        write(self.path('grok-alien.json'), '{"config":{"somethingElse":1}}')
        grok = self.pools('grok', QUOTA_STUB_GROK=self.path('grok-alien.json'))['grok']
        self.assertEqual((grok['status'], grok['why']),
                         ('unknown', 'the reply has no usage windows'))

    def test_20_an_odd_reply_makes_only_its_own_pool_unknown(self):
        write(self.path('grok-cfg-list.json'), '{"config":["x"]}')
        pools = self.pools(QUOTA_STUB_GROK=self.path('grok-cfg-list.json'))
        self.assertEqual(pools['grok']['why'], 'the reply has an unexpected shape')
        self.assertEqual([pools[p]['status'] for p in ('claude', 'codex', 'opencode')], ['ok'] * 3)

    def test_21_a_claude_utilization_that_is_text_is_unknown(self):
        state = self.path('t21')
        stream(os.path.join(state, 'hands', 'j', 'stream.jsonl'), 0,
               '{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":'
               '{"five_hour":{"utilization":"26","resetsAt":9999999999}}}}')
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual((claude['status'], claude['why']),
                         ('unknown', 'the reply has an unexpected shape'))

    def test_22_a_codex_length_that_is_not_a_number_keeps_the_window_name(self):
        write(self.path('codex-mins-text.json'), '{"result":{"rateLimits":{"primary":'
              '{"usedPercent":40,"windowDurationMins":"300"}}}}')
        codex = self.pools('codex', QUOTA_STUB_CODEX=self.path('codex-mins-text.json'))['codex']
        self.assertEqual(self.windows(codex), [('primary', 40)])

    def test_29_provider_text_is_masked(self):
        write(self.path('leak.json'), '{"type":"error","error":{"type":"auth_error","message":'
              '"Bad token sk-ant-oat01-LEAKLEAKLEAKLEAK and id 0123456789012345678901234567890123'
              '456789ABCD were rejected. Re-authenticate."}}')
        why = self.pools('grok', QUOTA_STUB_GROK=self.path('leak.json'))['grok']['why']
        self.assertNotRegex(why, 'sk-ant|LEAK|0123456789')
        self.assertIn('Re-authenticate', why)
        self.assertLessEqual(len(why), 200)

    def test_34_the_newest_reading_wins(self):
        # the most recently written stream, and in it the last event
        state = self.path('t34')
        stream(os.path.join(state, 'hands', 'old', 'stream.jsonl'), 600, rate_event(0.90, 0.90))
        stream(os.path.join(state, 'hands', 'new', 'stream.jsonl'), 30, rate_event(0.11, 0.12),
               '{"type":"assistant"}', rate_event(0.15, 0.16))
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual(self.windows(claude), [('week', 16.0), ('5h', 15.0)])

    def test_35_a_quoted_event_is_text_not_a_reading(self):
        # a codex seat discussing an event, written later than the real claude stream
        state = self.path('t35')
        stream(os.path.join(state, 'hands', 'claude', 'stream.jsonl'), 300, rate_event(0.21, 0.22))
        quoted = json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': rate_event(0.99, 0.99)}})
        stream(os.path.join(state, 'hands', 'codex', 'stream.jsonl'), 5, quoted)
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual(self.windows(claude), [('week', 22.0), ('5h', 21.0)])

    def test_36_an_old_reading_is_shown_with_its_age_in_hours(self):
        state = self.path('t36')
        stream(os.path.join(state, 'hands', 'claude', 'stream.jsonl'), 7200, rate_event(0.30, 0.40))
        out = self.run_quota('claude', XDG_STATE_HOME=state)
        self.assertRegex(out, r'^claude    week 40% \(resets in 2d\), '
                              r'5h 30% \(resets in \d+[mh]\); '
                              r'read 2h ago; refresh: hands\.sh --probe claude\n$')

    def test_37_a_window_reset_since_the_reading_is_dropped(self):
        state = self.path('t37')
        stream(os.path.join(state, 'hands', 'claude', 'stream.jsonl'), 60,
               rate_event(0.95, 0.40, int(time.time()) - 10))
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual((claude['status'], self.windows(claude), claude['reset_since']),
                         ('ok', [('week', 40.0)], ['5h']))
        out = self.run_quota('claude', XDG_STATE_HOME=state)
        self.assertIn('s ago, before the 5h reset; refresh: hands.sh --probe claude', out)

    def test_38_an_event_without_unified_windows_is_not_a_reading(self):
        # the older event shape, in a stream whose text mentions the field: the next stream counts
        state = self.path('t38')
        stream(os.path.join(state, 'hands', 'b', 'stream.jsonl'), 300, rate_event(0.31, 0.32))
        stream(os.path.join(state, 'hands', 'a', 'stream.jsonl'), 10,
               '{"type":"assistant","message":{"content":[{"type":"text",'
               '"text":"the gauge reads unifiedWindows"}]}}',
               '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
               '"resetsAt":1788303000,"rateLimitType":"five_hour"}}')
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual(self.windows(claude), [('week', 32.0), ('5h', 31.0)])

    def test_39_a_fresh_reading_says_its_age_in_seconds(self):
        out = self.run_quota('claude')
        self.assertRegex(out, r'^claude    week 43% \(resets in 2d\), '
                              r'5h 26% \(resets in \d+[mh]\); '
                              r'read \d+s ago; refresh: hands\.sh --probe claude\n$')

    def test_40_the_claude_reading_calls_nothing(self):   # no keychain, no curl, no claude CLI
        log = self.path('calls40.log')
        write(log, '')
        self.assertEqual(self.pools('claude', QUOTA_CALL_LOG=log)['claude']['status'], 'ok')
        self.assertEqual(read(log), '')

    def test_41_a_real_recorded_claude_stream(self):
        state = self.path('t41')
        replay(FX + '/claude-stream-ratelimit.jsonl', state)
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual(self.windows(claude), [('week', 37.0), ('5h', 12.0)])

    def test_47_the_opencode_key_comes_from_xdg_data_home_first(self):
        data, log = self.path('xdg47'), self.path('auth47.log')
        write(os.path.join(data, 'opencode', 'auth.json'), '{"opencode-go":{"key":"sk-xdg47"}}')
        self.run_quota('opencode', QUOTA_AUTH_LOG=log, XDG_DATA_HOME=data)
        self.assertEqual(read(log), 'Authorization: Bearer sk-xdg47\n')

    def test_48_a_model_limit_is_listed_apart_never_a_window(self):
        # the recorded fable event carries seven_day_overage_included 97 beside week 58
        state = self.path('t48')
        replay(FX + '/claude-stream-fable-scoped.jsonl', state)
        claude = self.pools('claude', XDG_STATE_HOME=state)['claude']
        self.assertEqual([w['name'] for w in claude['windows']], ['week', '5h'])
        limit = claude['model_limits']
        self.assertEqual([(m['name'], m['used_pct']) for m in limit],
                         [('seven_day_overage_included', 97.0)])
        self.assertTrue(limit[0]['resets_at'])
        out = self.run_quota('claude', XDG_STATE_HOME=state)
        self.assertRegex(out, r'^claude    week 58%.*, '
                              r'seven_day_overage_included 97% \(one model\); read ')

    def test_50_codex_calls_no_endpoint_and_reads_no_credential(self):
        home, log = self.path('home50'), self.path('calls50.log')
        login = os.path.join(home, '.codex', 'auth.json')
        write(login, read(self.path('home', '.codex', 'auth.json')), 0)   # unreadable
        codex = self.pools('codex', HOME=home, QUOTA_CALL_LOG=log)['codex']
        self.assertEqual(self.windows(codex), [('week', 40)])
        self.assertEqual(read(log), 'codex --no-daemon app-server\n')

    def test_51_one_deadline_bounds_the_codex_cli(self):
        # exits unanswered: unknown with its rc
        codex = self.pools('codex', QUOTA_STUB_CODEX='FAIL')['codex']
        self.assertEqual(codex['why'], 'codex app-server gave no answer (rc 6)')
        # never answers, or answers and never exits: the 20 s deadline kills its group. The two
        # run side by side, so the test waits for the deadline once
        env = dict(os.environ, HOME=self.path('home'),
                   PATH=self.path('bin') + ':' + os.environ['PATH'])
        start = time.time()
        runs = {}
        for mode in ('HANG', 'LINGER'):
            runs[mode] = subprocess.Popen([sys.executable, QUOTA, '--json', 'codex'],
                                          stdout=subprocess.PIPE, text=True,
                                          env=dict(env, QUOTA_STUB_CODEX=mode))
        out = {mode: json.loads(run.communicate(timeout=60)[0])['pools'][0]
               for mode, run in runs.items()}
        self.assertTrue(19 <= time.time() - start <= 30)
        self.assertEqual(out['HANG']['why'],
                         'codex app-server gave no answer (killed at the 20 s deadline)')
        self.assertEqual(self.windows(out['LINGER']), [('week', 40)])
        left = subprocess.run(['pgrep', '-f', self.path('bin', 'codex')],
                              capture_output=True, text=True)
        self.assertEqual(left.stdout, '')   # no codex stub is left running

    def test_52_the_bearer_never_rides_argv(self):
        argv_log, auth_log = self.path('argv52'), self.path('auth52')
        self.run_quota('grok', 'opencode', QUOTA_ARGV_LOG=argv_log, QUOTA_AUTH_LOG=auth_log)
        self.assertEqual(sorted(read(auth_log).splitlines()),
                         ['Authorization: Bearer sk-test', 'Authorization: Bearer tok-test'])
        argv = read(argv_log)
        self.assertTrue(argv)
        self.assertNotIn('Bearer', argv)

    def test_49_the_suite_env_carries_no_driver_inbox(self):
        names = {'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'CODEX_HOME'}
        self.assertFalse(names & set(os.environ))


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=2).result
    bad = len(result.failures) + len(result.errors)
    print('test_quota: %d passed, %d failed' % (result.testsRun - bad, bad))
    sys.exit(1 if bad else 0)
