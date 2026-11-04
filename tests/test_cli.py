#!/usr/bin/env python3
"""Tests for hands.py: the command line, the job dir and prompt it makes, the probe, the verbs
(send, mail, wait, events), the events view and the 30-day sweep of the jobs root.

Stub CLIs stand in for the providers (tests/stubs). Run: python3 tests/test_cli.py"""
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
import unittest

import harness
from harness import envelope, events, read_json, read_text, result, wait_until

import hands
import seat

SB = None
SYSTEM_PATH = '/usr/bin:/bin'


def setUpModule():
    global SB
    SB = harness.Sandbox('cli')


def tearDownModule():
    SB.close()


def on_system_path(name):
    return bool(shutil.which(name, path=SYSTEM_PATH))


class CommandLine(unittest.TestCase):
    """What hands.sh refuses before any seat runs: each refusal is rc 2, and no CLI is launched."""

    def assertNoLaunch(self, job):
        self.assertEqual(SB.stub_log(job).launches(), [])

    def test_help_prints_the_usage(self):
        run = SB.hands('--help')
        self.assertEqual(run.rc, 0)
        self.assertIn('--effort <tier>', run.out)
        self.assertRegex(run.out, re.compile('^envelope:', re.M))

    def test_an_unknown_flag_and_no_arguments_are_usage_errors(self):
        self.assertEqual(SB.hands('--bogus', 'x', SB.task).rc, 2)
        self.assertEqual(SB.hands().rc, 2)

    def test_an_unknown_provider_is_refused_with_an_error_envelope(self):
        job, run = SB.buy('unknown', 'badprov')
        self.assertEqual(run.rc, 2)
        self.assertEqual((envelope(job)['status'], envelope(job)['provider']), ('error', 'badprov'))

    @unittest.skipIf(on_system_path('gemini'), 'a gemini CLI is on the system path')
    def test_gemini_is_refused_as_any_unknown_provider(self):
        job = SB.path('gemini')
        run = SB.hands('--out', job, 'gemini:gemini-3.8-flash-high', SB.task,
                       PATH=harness.STUBS + ':' + SYSTEM_PATH)
        self.assertEqual(run.rc, 2)
        self.assertEqual({k: envelope(job)[k] for k in ('status', 'provider', 'model', 'rc')},
                         {'status': 'error', 'provider': 'gemini',
                          'model': 'gemini-3.8-flash-high', 'rc': 2})
        self.assertIn("unknown provider 'gemini' (claude|codex|grok|opencode)", run.err)

    def test_a_missing_task_file_is_refused_with_a_full_envelope(self):
        job, run = SB.buy('missing-task', 'claude:sonnet', task=SB.path('nope.md'))
        self.assertEqual(run.rc, 2)
        self.assertNoLaunch(job)
        keys = {'status', 'provider', 'model', 'effort', 'rc', 'seconds', 'reset', 'dir', 'usage'}
        self.assertLessEqual(keys, set(envelope(job)))
        self.assertIsNone(envelope(job)['effort'])
        job, _ = SB.buy('missing-task-effort', 'claude:sonnet', '--effort', 'xhigh',
                        task=SB.path('nope.md'))
        self.assertEqual(envelope(job)['effort'], 'xhigh')   # the effort a relaunch would run at

    def test_a_provider_name_must_be_plain_lowercase(self):
        run = SB.hands('x/../../esc:m', SB.task)
        self.assertEqual(run.rc, 2)
        self.assertIn("bad provider 'x/../../esc'", run.err)
        # the dir it would have named, <root>/<date>-x/../../esc-<pid>, is outside the root
        self.assertFalse([n for n in os.listdir(SB.path('state')) if n.startswith('esc')])

    def test_an_out_dir_that_cannot_be_made_is_refused(self):
        here = SB.path('caller')
        os.makedirs(here)
        run = SB.hands('--out', '/dev/null/x', 'claude', SB.task, cwd=here)
        self.assertEqual(run.rc, 2)
        self.assertIn('cannot create the job dir /dev/null/x', run.err)
        self.assertEqual(os.listdir(here), [])   # nothing landed in the caller's cwd

    def test_a_value_flag_with_no_value_is_a_usage_error(self):
        run = SB.hands('--cwd')
        self.assertEqual(run.rc, 2)
        self.assertIn('hands: --cwd needs a value', run.err)

    def test_a_flag_after_the_provider_is_named(self):
        run = SB.hands('--out', SB.path('late-flag'), 'claude', '--effort')
        self.assertIn('must PRECEDE the provider positional', run.out + run.err)

    def test_an_out_dir_in_use_is_refused(self):
        SB.buy('in-use', 'claude:sonnet', task=SB.path('nope.md'))
        self.assertEqual(SB.buy('in-use', 'claude:sonnet')[1].rc, 2)

    def test_a_non_empty_out_dir_is_refused_and_left_alone(self):
        job = SB.path('not-empty')
        os.makedirs(job)
        SB.write('not-empty/notes.txt', 'keep')
        _, run = SB.buy('not-empty', 'claude:sonnet')
        self.assertEqual(run.rc, 2)
        self.assertIn('is not empty', run.err)
        self.assertEqual(os.listdir(job), ['notes.txt'])
        self.assertEqual(read_text(os.path.join(job, 'notes.txt')), 'keep')

    def test_an_opencode_seat_names_provider_slash_model(self):
        for name, spec in (('oc-no-model', 'opencode'), ('oc-no-slash', 'opencode:foo')):
            job, run = SB.buy(name, spec, '--effort', 'low')
            self.assertEqual(run.rc, 2)
            self.assertEqual((envelope(job)['status'], envelope(job)['reason']),
                             ('refused', 'model-required'))
            self.assertNoLaunch(job)

    def test_send_refusals_come_before_any_launch(self):
        job, _ = SB.buy('no-session', 'claude:sonnet', STUB_MODE='authfail')
        log = SB.stub_log(job)
        launched = len(log.launches())
        run = SB.send(job)
        self.assertEqual(run.rc, 2)
        self.assertEqual(len(log.launches()), launched)
        self.assertEqual((run.json['status'], run.json['reason'], run.json['rc']),
                         ('refused', 'no-session', 2))
        self.assertEqual(envelope(job)['reason'], 'no-session')
        self.assertIn('recorded no session', run.err)
        handle_less, _ = SB.buy('no-handle', 'claude:sonnet', task=SB.path('nope.md'))
        self.assertEqual(SB.send(handle_less).rc, 2)
        seat_dir, _ = SB.buy('send-args', 'claude:sonnet')
        self.assertEqual(SB.send(seat_dir, SB.path('nope.md')).rc, 2)
        self.assertEqual(SB.hands('send', seat_dir).rc, 2)


class SeatPrompt(unittest.TestCase):
    """prompt.txt: the seat box, a blank line, the task, a blank line, the job-dir line."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(SB.path('elsewhere'))
        cls.grok, _ = SB.start('prompt-grok', 'grok:grok-4.6', '--effort', 'low', '--cwd', SB.work)
        cls.opencode, _ = SB.start('prompt-opencode', 'opencode:opencode-go/deepseek-v4.1-flash',
                                   '--effort', 'low', '--cwd', SB.path('elsewhere'))
        cls.claude, _ = SB.buy('prompt-claude', 'claude:sonnet')
        for job in (cls.grok, cls.opencode):
            SB.wait(job)
            SB.stop(job)

    def test_the_prompt_is_the_box_the_task_and_the_job_dir_line(self):
        expected = hands.SEAT_BOX + '\ntask\n\n' + hands.seat_line(self.grok)
        prompt = read_text(os.path.join(self.grok, 'prompt.txt'))
        self.assertTrue(prompt.startswith('SEAT BOX.'))
        self.assertEqual(prompt, expected)
        agent = SB.stub_log(self.grok).seats()[0]
        first = next(params for method, params in agent.messages() if method == 'session/prompt')
        self.assertEqual(first['prompt'], [{'type': 'text', 'text': prompt}])   # byte for byte

    def test_two_seats_given_one_task_differ_only_after_it(self):
        shared = hands.SEAT_BOX + '\n' + read_text(SB.task)
        prompts = [read_text(os.path.join(job, 'prompt.txt')) for job in (self.grok, self.opencode)]
        self.assertTrue(all(p.startswith(shared) for p in prompts))
        self.assertNotEqual(prompts[0], prompts[1])

    def test_the_inbox_and_outbox_exist_empty_before_the_seat_starts(self):
        for job in (self.grok, self.claude):
            for name in ('inbox.jsonl', 'outbox.jsonl'):
                path = os.path.join(job, name)
                self.assertTrue(os.path.isfile(path) and os.path.getsize(path) == 0, path)


class Probe(unittest.TestCase):
    def probe_dirs(self, state, provider):
        root = os.path.join(state, 'hands')
        return [os.path.join(root, n) for n in os.listdir(root) if n.endswith('-' + provider)]

    def test_the_claude_probe_pings_haiku_from_a_neutral_cwd(self):
        state = SB.path('probe-state')
        log = SB.path('probe-claude.stub.jsonl')
        run = SB.hands('--probe', 'claude', cwd=SB.work, XDG_STATE_HOME=state, STUB_LOG=log)
        cli = harness.StubLog(log).seats()[0]
        self.assertEqual(cli.argv[cli.argv.index('--model') + 1], 'haiku')
        self.assertEqual(cli.cwd, SB.path('tmp'))
        dirs = self.probe_dirs(state, 'claude')
        self.assertTrue(dirs and os.path.exists(os.path.join(dirs[0], 'stream.jsonl')))
        self.assertIn('"status":"ok","provider":"claude"', run.out)

    def test_the_opencode_probe_pings_the_cheapest_go_model_at_low(self):
        state = SB.path('probe-state-oc')
        log = SB.path('probe-opencode.stub.jsonl')
        SB.hands('--probe', 'opencode', cwd=SB.work, XDG_STATE_HOME=state, STUB_LOG=log)
        cli = harness.StubLog(log).seats()[0]
        self.assertEqual((cli.argv, cli.cwd), (['acp'], SB.path('tmp')))
        found = [envelope(d) for d in self.probe_dirs(state, 'opencode')]
        self.assertIn(('ok', 'opencode-go/deepseek-v4.1-flash', 'low'),
                      [(e['status'], e['model'], e['effort']) for e in found])

    @unittest.skipUnless(on_system_path('python3'), 'no python3 on the system path')
    def test_providers_not_installed_each_print_a_full_line(self):
        if any(on_system_path(c) for c in harness.SPECS):
            self.skipTest('a provider CLI is on the system path')
        run = SB.hands('--probe', 'all', PATH=SYSTEM_PATH, HOME=SB.root)
        lines = [json.loads(line) for line in run.out.split('\n') if line.strip()]
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertEqual((line['status'], line['dir']), ('not-installed', None))
            self.assertIn('usage', line)

    @unittest.skipUnless(on_system_path('python3'), 'no python3 on the system path')
    def test_the_probe_never_writes_through_a_planted_link(self):
        if on_system_path('grok'):
            self.skipTest('grok is on the system path')
        tmp = SB.path('probe-tmp')
        os.makedirs(tmp)
        victim = SB.write('victim', 'keep')
        # sh's exec keeps its pid, so the links sit at the names an old probe would have used
        plant = ('ln -s "$1" "$2/hands-probe-lines-$$"; ln -s "$1" "$2/hands-probe-$$.txt"; '
                 'exec "$3" --probe grok')
        SB.run(['sh', '-c', plant, '_', victim, tmp, harness.HANDS], PATH=SYSTEM_PATH,
               HOME=SB.root, TMPDIR=tmp)
        self.assertEqual(read_text(victim), 'keep')
        left = os.listdir(tmp)   # the two links, and no temp file of the probe's own
        self.assertEqual(len(left), 2)
        self.assertTrue(all(name.startswith('hands-probe-') for name in left), left)

    def test_probes_never_push_to_the_driver(self):
        state = SB.path('probe-push')
        socket = harness.DriverSocket(SB.path('probe.sock'))
        try:
            run = SB.hands('--probe', 'claude', XDG_STATE_HOME=state,
                           CLAUDE_CODE_MESSAGING_SOCKET=socket.path,
                           CLAUDE_CODE_MESSAGING_TOKEN='tok-stub')
            run2 = SB.hands('--probe', 'codex', XDG_STATE_HOME=state,
                            CLAUDE_CODE_MESSAGING_SOCKET=socket.path,
                            CLAUDE_CODE_MESSAGING_TOKEN='tok-stub')
            time.sleep(0.5)
            self.assertEqual((run.out + run2.out).count('"status":"ok"'), 2)
            self.assertTrue(self.probe_dirs(state, 'claude') and self.probe_dirs(state, 'codex'))
            drivers = [top for top, _, files in os.walk(state) if 'driver' in files]
            self.assertEqual(drivers, [])
            self.assertEqual(socket.connections, [])
        finally:
            socket.close()


class Verbs(unittest.TestCase):
    """mail, wait and the JSON lines the verbs print."""

    def test_mail_appends_one_driver_line_to_the_inbox(self):
        job, _ = SB.start('mail', 'claude:sonnet', STUB_MODE='park')
        SB.hands('mail', job, 'hello', 'there')
        line = harness.read_jsonl(os.path.join(job, 'inbox.jsonl'))[-1]
        self.assertEqual((line['from'], line['text']), ('driver', 'hello there'))
        self.assertIsInstance(line['t'], str)
        SB.stop(job)

    def test_wait_returns_on_new_mail_and_on_exit(self):
        job, _ = SB.start('wait-for', 'claude:sonnet', STUB_MODE='park')
        waiter = SB.popen('wait', job, '--for', 'mail', '--timeout', '10')
        for _ in range(40):   # the waiter counts only lines after it starts: mail until it answers
            if waiter.poll() is not None:
                break
            with open(os.path.join(job, 'outbox.jsonl'), 'a') as f:
                f.write('{"to":"driver","text":"mail-92"}\n')
            time.sleep(0.25)
        out, _ = waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 0)
        self.assertIn('mail-92', out)
        exit_waiter = SB.popen('wait', job, '--for', 'exit', '--timeout', '30')
        time.sleep(0.6)
        SB.stop(job)
        out, _ = exit_waiter.communicate(timeout=30)
        final = harness.last_json(out)
        self.assertEqual((final['status'], final['rc']), ('incomplete', 143))

    def test_every_verb_line_is_compact_json(self):
        job, _ = SB.start('compact', 'codex:gpt-5.6-terra', STUB_MODE='park')
        wait_until(lambda: SB.stub_log(job).seats()[0].parked)
        sent = SB.send(job)
        running = SB.wait(job, '--timeout', '1')
        SB.stop(job)
        self.assertEqual(sent.json['status'], 'sent')
        self.assertEqual((running.json['status'], running.json['turn']), ('running', 1))
        self.assertNotIn('": ', sent.out + running.out)

    def test_wait_after_the_wrapper_died_mid_turn_is_incomplete(self):
        job, _ = SB.start('wrapper-died', 'claude:sonnet', STUB_MODE='slow', STUB_SLEEP='8')
        SB.wait(job, '--timeout', '60')
        SB.send(job)
        self.assertTrue(wait_until(lambda: any(e['kind'] == 'turn_start' and e['turn'] == 2
                                               for e in events(job))))
        wrappers = harness.wrapper_pids(job)
        self.assertTrue(wrappers)
        os.killpg(wrappers[0], signal.SIGKILL)
        time.sleep(0.3)
        run = SB.wait(job, '--timeout', '10')
        self.assertEqual(run.rc, 1)
        self.assertEqual((run.json['status'], run.json['reason'], run.json['turn']),
                         ('incomplete', 'wrapper-gone', 2))

    def test_a_signalled_one_shot_buy_stops_its_seat_first(self):
        job = SB.path('signalled')
        buy = SB.popen('--out', job, 'claude:sonnet', SB.task, STUB_MODE='park',
                       STUB_LOG=job + '.stub.jsonl')
        self.assertTrue(wait_until(lambda: SB.stub_log(job).seats()[0].parked))
        cli = SB.stub_log(job).seats()[0].pid
        buy.send_signal(signal.SIGTERM)
        buy.communicate(timeout=30)
        self.assertEqual(buy.returncode, 143)
        self.assertEqual(harness.wrapper_pids(job), [])
        self.assertFalse(harness.alive(cli))
        self.assertEqual(envelope(job)['status'], 'incomplete')

    def test_quota_is_the_read_only_board(self):
        run = SB.hands('quota', '--json', 'claude', XDG_STATE_HOME=SB.path('quota-state'))
        self.assertEqual(run.rc, 0)
        pools = json.loads(run.out)['pools']
        self.assertEqual([(p['pool'], p['status']) for p in pools], [('claude', 'unknown')])
        self.assertIn('--probe claude', pools[0]['why'])


class EventsView(unittest.TestCase):
    """`hands.sh events DIR` renders events.jsonl, here the recorded streams of real seats."""

    @classmethod
    def setUpClass(cls):
        cls.views = {}
        for cli in ('grok', 'claude', 'opencode'):
            job = SB.path('view-' + cli)
            os.makedirs(job)
            shutil.copy(os.path.join(harness.FIXTURES, '%s-events-sample.jsonl' % cli),
                        os.path.join(job, 'events.jsonl'))
            cls.views[cli] = (job, SB.hands('events', job).out)

    def test_the_grok_stream_renders_to_a_few_readable_lines(self):
        lines = self.views['grok'][1].split('\n')[:-1]
        self.assertTrue(20 <= len(lines) <= 25, len(lines))
        self.assertFalse([line for line in lines if line.startswith('{')])
        self.assertEqual(len([line for line in lines if line.startswith('> t')]), 3)
        tools = [line for line in lines if line.startswith('$ ')]
        self.assertEqual([t.split(' {')[0] for t in tools],
                         ['$ read_file', '$ read_file', '$ run_terminal_command'])
        self.assertTrue(all(len(t) <= 340 for t in tools))
        text = '\n'.join(lines)
        self.assertEqual(re.findall(r'^── turn (\d) ([a-z]+) · \d\d:\d\d:\d\d$', text, re.M),
                         [('1', 'ok'), ('2', 'ok'), ('3', 'ok')])
        hits = re.findall(r'^· usage .* cache hit (\d+)%', text, re.M)
        self.assertEqual(hits, ['0', '99', '98'])
        self.assertLessEqual({'READY', 'HERON-7 | The second codeword is OTTER-3.', 'HERON-7'},
                             set(lines))

    def test_the_view_is_the_same_whole_or_fed_line_by_line(self):
        for cli, (job, once) in self.views.items():
            self.assertEqual(SB.hands('events', job).out, once)
            view = hands.EventView()
            lines = read_text(os.path.join(job, 'events.jsonl')).split('\n')[:-1]
            fed = ''.join(view.feed(line + '\n') for line in lines) + view.flush()
            self.assertTrue(once)
            self.assertEqual(fed, once, cli)

    def test_an_input_opencode_names_late_rides_one_line_under_its_call(self):
        lines = self.views['opencode'][1].split('\n')
        call = lines.index('$ read')
        self.assertTrue(lines[call + 1].startswith('  ↳ {"filePath": "/'))
        self.assertTrue(lines[call + 1].endswith('/inbox.jsonl"}'))
        self.assertEqual(sum(line.startswith('  ↳ ') for line in lines), 2)

    def test_follow_ends_with_the_seat(self):
        job, _ = SB.start('follow', 'claude:sonnet', STUB_MODE='park')
        wait_until(lambda: SB.stub_log(job).seats()[0].parked)
        follower = SB.popen('events', job, '--follow')
        time.sleep(1.5)
        self.assertIsNone(follower.poll())   # it follows a live seat
        SB.stop(job)
        out, _ = follower.communicate(timeout=2)
        self.assertEqual(follower.returncode, 0)
        closing = re.findall(r'^── turn (\d+) ([a-z-]+)', out, re.M)
        self.assertEqual(closing, [('1', 'incomplete')])   # the stopped turn is its last news


class JobsRoot(unittest.TestCase):
    """Where job dirs live, and the sweep that deletes the idle ones."""

    def own_dirs(self, root):
        return [n for n in os.listdir(root) if re.fullmatch(r'\d{8}-\d{6}-codex-\d+', n)]

    def mode(self, path):
        return os.stat(path).st_mode & 0o777

    def test_a_buy_without_out_lands_in_the_jobs_root(self):
        os.makedirs(SB.path('home-a'))
        os.makedirs(SB.path('home-b'))
        SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task,
                 XDG_STATE_HOME=SB.path('root-a'))
        SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task,
                 XDG_STATE_HOME=None, HOME=SB.path('home-a'))
        SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task, cwd=SB.path('tmp'),
                 XDG_STATE_HOME='relative', HOME=SB.path('home-b'))   # XDG ignores a relative one
        for root in (SB.path('root-a', 'hands'), SB.path('home-a', '.local', 'state', 'hands'),
                     SB.path('home-b', '.local', 'state', 'hands')):
            names = self.own_dirs(root)
            self.assertEqual(len(names), 1, root)
            job = os.path.join(root, names[0])
            self.assertEqual((self.mode(root), self.mode(job)), (0o700, 0o700))
            self.assertEqual(envelope(job)['status'], 'ok')
        self.assertFalse(os.path.exists(SB.path('tmp', 'hands')))
        self.assertFalse(os.path.exists(SB.path('tmp', 'relative')))

    def test_an_out_dir_is_private_and_its_parent_is_not(self):
        SB.buy('out-parent/x', 'codex:gpt-5.6-terra', '--cwd', SB.work)
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(self.mode(SB.path('out-parent')), 0o777 & ~mask)
        self.assertEqual(self.mode(SB.path('out-parent', 'x')), 0o700)
        os.makedirs(SB.path('out-existing'))
        os.chmod(SB.path('out-existing'), 0o755)
        SB.buy('out-existing', 'codex:gpt-5.6-terra', '--cwd', SB.work)
        self.assertEqual(self.mode(SB.path('out-existing')), 0o700)

    def test_the_sweep_deletes_only_idle_dirs_hands_named(self):
        state = SB.path('sweep-state')
        root = os.path.join(state, 'hands')
        SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task, XDG_STATE_HOME=state)
        SB.hands('--probe', 'claude', XDG_STATE_HOME=state)
        real = os.listdir(root)   # a buy's dir and a probe's, as hands names them today
        names = ['20250101-000000-codex-1', 'probe-1-claude', '20250101-000000-codex-2',
                 '20250101-000000-codex-3', 'keepme', '20250101-000000-codex-4',
                 '20250101-000000-codex-6']
        for name in names:
            os.makedirs(os.path.join(root, name, 'turn-1'))
            for file, text in (('session', '{"provider":"codex","cwd":"/"}'),
                               ('turn-1/result.txt', 'x'), ('job.log', '')):
                with open(os.path.join(root, name, file), 'w') as f:
                    f.write(text)
        os.makedirs(os.path.join(root, '20250101-000000-codex-4', '.git'))
        SB.write('sweep-state/hands/20250101-000000-codex-4/.git/HEAD', 'ref')
        SB.write('sweep-state/hands/20250101-000000-codex-6/CLAUDE.md', 'x')
        elsewhere = SB.path('sweep-elsewhere')
        os.makedirs(elsewhere)
        SB.write('sweep-elsewhere/session', 't')
        os.symlink(elsewhere, os.path.join(root, '20250101-000000-codex-5'))
        harness.age(31, root, elsewhere)
        young = time.time() - 29 * 86400
        os.utime(os.path.join(root, '20250101-000000-codex-2', 'turn-1', 'result.txt'),
                 (young, young))
        holder = subprocess.Popen([
            'python3', '-c', 'import fcntl, os, sys, time\n'
            'fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)\n'
            'fcntl.flock(fd, fcntl.LOCK_EX); print("held", flush=True); time.sleep(60)',
            os.path.join(root, '20250101-000000-codex-3', 'job.log')], stdout=subprocess.PIPE)
        holder.stdout.readline()   # the lock is held, as a live wrapper holds it
        holder.stdout.close()
        try:
            SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task, XDG_STATE_HOME=state)
        finally:
            holder.kill()
            holder.wait()
        left = set(os.listdir(root))
        self.assertFalse({'20250101-000000-codex-1', 'probe-1-claude',
                          '20250101-000000-codex-6'} & left)   # a CLAUDE.md is no repository
        self.assertLessEqual({'20250101-000000-codex-2', '20250101-000000-codex-3', 'keepme',
                              '20250101-000000-codex-4', '20250101-000000-codex-5'}, left)
        self.assertTrue(os.path.islink(os.path.join(root, '20250101-000000-codex-5')))
        self.assertEqual(os.listdir(elsewhere), ['session'])
        self.assertEqual(len(real), 2)
        self.assertTrue(any(n.startswith('probe-') for n in real))
        self.assertFalse(set(real) & left)   # today's names are swept too
        made = [n for n in self.own_dirs(root) if not n.startswith('2025')]
        self.assertEqual(len(made), 1)   # the buy's own dir is made

    def test_a_resume_is_use(self):
        state = SB.path('resume-state')
        SB.hands('--cwd', SB.work, 'codex:gpt-5.6-terra', SB.task, XDG_STATE_HOME=state)
        root = os.path.join(state, 'hands')
        job = os.path.join(root, self.own_dirs(root)[0])
        harness.age(31, job)
        touched = SB.run(['python3', os.path.join(harness.ROOT, 'seat.py'), 'send', job,
                          SB.turn_two], XDG_STATE_HOME=state)
        self.assertEqual(touched.rc, 3)   # no live seat: hands.py would relaunch it
        SB.run(['python3', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import hands; '
                'hands.sweep()', harness.ROOT], XDG_STATE_HOME=state)
        self.assertTrue(os.path.isdir(job))
        SB.send(job, XDG_STATE_HOME=state)
        SB.wait(job, '--timeout', '30')
        SB.stop(job)
        self.assertEqual(envelope(job, 2)['status'], 'ok')

    def test_the_sweep_judges_again_under_the_lock(self):
        root = SB.path('lock-state', 'hands')
        old = time.time() - 31 * 86400

        def make(name, with_log=True, subdir=None):
            path = os.path.join(root, name)
            os.makedirs(os.path.join(path, 'turn-1'))
            files = {'session': '{"provider":"codex","cwd":"/"}', 'turn-1/result.txt': 'x'}
            if with_log:
                files['job.log'] = ''
            for file, text in files.items():
                with open(os.path.join(path, file), 'w') as f:
                    f.write(text)
            if subdir:
                os.makedirs(os.path.join(path, subdir))
                open(os.path.join(path, subdir, 'f'), 'w').close()
            return path

        late = make('20250101-000000-codex-1')
        racing = make('20250101-000000-codex-2')
        partial = make('20250101-000000-codex-3', subdir='ro')
        never_ran = make('20250101-000000-codex-4', with_log=False)
        harness.age(31, root)
        os.chmod(os.path.join(partial, 'ro'), 0o500)
        os.utime(root, (old, old))
        late_log = os.stat(os.path.join(late, 'job.log')).st_ino
        real_flock = fcntl.flock
        real_rmtree = shutil.rmtree
        opened = {}

        def flock(fd, op):   # a write lands between the first judgment and the lock
            if os.fstat(fd).st_ino == late_log:
                open(os.path.join(late, 'turn-1', 'late.txt'), 'w').close()
            return real_flock(fd, op)

        def rmtree(path, *args, **kwargs):   # a relaunch opens job.log by the dir's own path
            if path.endswith('codex-2'):
                try:
                    os.close(os.open(os.path.join(racing, 'job.log'), os.O_WRONLY | os.O_CREAT))
                    opened['racing'] = 'opened'
                except OSError as e:
                    opened['racing'] = e.errno
            return real_rmtree(path, *args, **kwargs)

        jobs = seat.JOBS
        seat.JOBS = root
        hands.fcntl.flock = flock
        hands.shutil.rmtree = rmtree
        try:
            hands.sweep()
            hidden = os.path.join(root, '.20250101-000000-codex-3')
            left_hidden = os.path.isdir(hidden) and not os.path.lexists(partial)
            for path in (os.path.join(partial, 'ro'), os.path.join(hidden, 'ro')):
                if os.path.isdir(path):
                    os.chmod(path, 0o700)   # the delete's blocker is gone
            if os.path.isdir(hidden):
                harness.age(31, hidden)
            hands.sweep()   # 31 days on, the next sweep finishes it
        finally:
            seat.JOBS = jobs
            hands.fcntl.flock = real_flock
            hands.shutil.rmtree = real_rmtree
        self.assertTrue(os.path.isdir(late))
        self.assertEqual(opened.get('racing'), errno.ENOENT)
        self.assertFalse(os.path.lexists(racing))
        self.assertTrue(left_hidden)
        self.assertFalse(os.path.lexists(hidden))
        self.assertFalse(os.path.lexists(never_ran))   # and no job.log was made to lock it

    def test_the_sweep_stops_at_a_mount(self):
        path = SB.path('mount-test', '20250101-000000-codex-9')
        os.makedirs(os.path.join(path, 'mnt'))
        harness.age(31, path)
        cutoff = time.time() - hands.IDLE_LIMIT
        self.assertTrue(hands.is_stale(path, cutoff))
        real = os.lstat

        def lstat(p, *args, **kwargs):   # what lies under mnt is on another device
            info = real(p, *args, **kwargs)
            if p.endswith('/mnt'):
                return os.stat_result((info[0], info[1], info[2] + 1) + tuple(info[3:]))
            return info

        hands.os.lstat = lstat
        try:
            self.assertFalse(hands.is_stale(path, cutoff))
        finally:
            hands.os.lstat = real


if __name__ == '__main__':
    harness.main('test_cli')
