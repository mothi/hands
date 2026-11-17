#!/usr/bin/env python3
"""Claude stream-json seats against the stub CLI and recorded turns."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

import harness
from harness import (SeatChecks, agent, envelope, events, file_exists, progress, read_text,
                     result, turn_ends, turn_started, wait_until)

CLAUDE = harness.SPECS['claude']
SB = None


def setUpModule():
    global SB
    SB = harness.SeatSandbox('claude')


def tearDownModule():
    SB.close()


class ClaudeSeats(SeatChecks):
    @classmethod
    def setUpClass(cls):
        cls.one_shot, _ = SB.one_shot('claude-one-shot', CLAUDE)

    def option(self, launch, flag):
        return launch.argv[launch.argv.index(flag) + 1]

    def assertSettings(self, launch):
        """One --settings: refuse cross-session messages, and no hooks of the user's."""
        self.assertEqual(launch.argv.count('--settings'), 1)
        self.assertEqual(self.option(launch, '--settings'),
                         '{"crossSessionInbound":"refuse","disableAllHooks":true}')
        self.assertFalse([w for w in launch.argv if w.startswith('--setting-sources')])

    def test_a_claude_seat_runs_lean_and_streams_both_ways(self):
        cli = agent(self.one_shot)
        self.assertIn('bg=1', result(self.one_shot))    # background tasks off
        self.assertIn('mem=1', result(self.one_shot))   # auto memory off
        self.assertEqual((envelope(self.one_shot)['status'], envelope(self.one_shot)['session']),
                         ('ok', 'sess-stub'))
        self.assertEqual(self.option(cli, '--input-format'), 'stream-json')
        self.assertEqual(self.option(cli, '--output-format'), 'stream-json')
        lean = ('--verbose', '--strict-mcp-config', '--exclude-dynamic-system-prompt-sections')
        for flag in lean:
            self.assertIn(flag, cli.argv)
        self.assertSettings(cli)

    def test_a_seat_that_died_mid_turn_still_has_its_session(self):
        job, _ = SB.one_shot('claude-died', CLAUDE, STUB_MODE='initonly')
        self.assertNotEqual(envelope(job)['status'], 'ok')
        self.assertEqual(envelope(job)['session'], 'sess-stub')   # read from the first event
        path = os.path.join(job, 'result.txt')
        self.assertFalse(os.path.exists(path) and os.path.getsize(path))

    def test_a_recorded_claude_stream(self):
        replay = os.path.join(harness.FIXTURES, 'claude-stream-sample.jsonl')
        job, _ = SB.one_shot('claude-wire', CLAUDE, STUB_MODE='replay', STUB_REPLAY=replay)
        self.assertEqual(result(job), 'ok')
        self.assertEqual((envelope(job)['status'], envelope(job)['session']),
                         ('ok', '49c93750-efa1-4e50-9d05-6470e8eb5512'))

    def test_a_process_the_seat_forgot_dies_with_it(self):
        job, _ = SB.one_shot('claude-straggler', CLAUDE, STUB_MODE='straggler')
        child = agent(job).children[0]
        try:
            self.assertEqual(envelope(job)['status'], 'ok')
            self.assertTrue(wait_until(lambda: not harness.alive(child), timeout=3))
        finally:
            harness.kill(child)

    def test_a_cli_killed_from_outside_still_yields_its_session(self):
        job = SB.path('claude-killed')
        run = SB.popen('--out', job, CLAUDE, SB.task, STUB_MODE='park',
                       STUB_LOG=job + '.stub.jsonl')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        os.kill(agent(job).pid, signal.SIGTERM)
        run.communicate(timeout=30)
        end = envelope(job)
        self.assertNotEqual(end['status'], 'ok')
        self.assertEqual((end['rc'], end['session']), (143, 'sess-stub'))
        self.assertFalse(harness.alive(agent(job).pid))

    def test_a_seat_keeps_one_process_and_resumes_after_a_stop(self):
        job, run = SB.start('claude-seat', CLAUDE, '--effort', 'high', '--cwd', SB.path('tmp'),
                            HANDS_IDLE='60', STUB_MODE='count')
        SB.wait(job)
        SB.send(job)
        SB.wait(job)
        first, second = result(job, 1), result(job, 2)
        self.assertEqual((run.json['status'], run.json['expect'], run.json['delivery']),
                         ('spawned', 1, 'steer'))
        self.assertTrue(first.startswith('turn 1 pid ') and second.startswith('turn 2 pid '))
        self.assertEqual(first.split()[-1], second.split()[-1])   # the same process
        self.assertEqual(result(job), second)
        self.assertEqual(envelope(job, 1)['turn'], 1)
        end = envelope(job)
        self.assertEqual((end['turn'], end['delivery'], end['status']), (2, 'steer', 'ok'))
        SB.stop(job)
        relaunched = SB.send(job, STUB_MODE='count').json
        SB.wait(job)
        SB.stop(job)
        again = SB.stub_log(job).seats()[1]
        self.assertEqual(self.option(again, '--resume'), 'sess-stub')
        self.assertEqual((self.option(again, '--model'), self.option(again, '--effort')),
                         ('sonnet', 'high'))
        self.assertIn('bypassPermissions', again.argv)
        self.assertEqual(again.cwd, SB.path('tmp'))
        self.assertSettings(again)
        self.assertTrue(result(job, 3).startswith('turn 1 pid'))   # a new process answers turn 3
        end = envelope(job)
        self.assertEqual((end['turn'], end['status'], end['session']), (3, 'ok', 'sess-stub'))
        self.assertEqual((relaunched['status'], relaunched['expect']), ('spawned', 3))

    def test_a_turn_on_another_session_is_an_error(self):
        job, _ = SB.start('claude-new-session', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='count')
        SB.wait(job)
        SB.stop(job)
        SB.send(job, STUB_MODE='newsid')   # a resume that silently started fresh
        SB.wait(job)
        SB.stop(job)
        end = envelope(job)
        self.assertEqual((end['turn'], end['status'], end['reason'], end['session'], end['rc']),
                         (2, 'error', 'session-changed', 'sess-stub', 1))
        self.assertEqual(harness.handle(job)['session'], 'sess-stub')   # the first one is kept

    def test_a_recorded_three_turn_seat(self):
        replay = os.path.join(harness.FIXTURES, 'claude-stdin-sample.jsonl')
        job, _ = SB.start('claude-recorded', 'claude:claude-haiku-4-5-20251001', '--cwd',
                          SB.path('tmp'), HANDS_IDLE='60', STUB_MODE='replay', STUB_REPLAY=replay)
        for _ in range(2):
            SB.wait(job)
            SB.send(job)
        waited = SB.wait(job)
        SB.stop(job)
        kinds = ['turn_start', 'text', 'thought', 'tool_call', 'tool_result', 'usage', 'turn_end',
                 'progress']
        self.assertVocabulary(events(job), kinds, ['ok', 'ok', 'ok'], 'ZEBRA-42', result(job))
        # the line wait prints is envelope.json with its usage trimmed and priced
        line = waited.out.rstrip('\n').split('\n')[-1]
        printed = json.loads(line)
        disk = envelope(job)
        want = {'in': 10, 'cache_read': 31151, 'cache_write': 246, 'out': 98,
                'cost_usd': 0.0041071}
        self.assertEqual(list(printed['usage']), ['in', 'cache_read', 'cache_write', 'out',
                                                  'cost_usd'])
        self.assertEqual(printed['usage'], want)
        self.assertEqual(disk['usage'], dict(want, reasoning=87))
        self.assertEqual(dict(printed, usage=None), dict(disk, usage=None))
        self.assertLessEqual(len(json.dumps(printed['usage'], separators=(',', ':'))), 100)
        self.assertLess(len(line.encode()),
                        len(read_text(os.path.join(job, 'envelope.json')).rstrip('\n').encode()))
        # claude's turn cost is the step of its process-cumulative total_cost_usd
        self.assertEqual([envelope(job, n)['usage'].get('cost_usd') for n in (1, 2, 3)],
                         [0.059393, 0.0156844, 0.0041071])

    def test_failures_are_typed_from_provider_fields_and_stderr(self):
        statuses = {}
        for mode in ('quoted', 'quotedfail', 'quota', 'authfail'):
            job, _ = SB.one_shot('claude-fail-' + mode, CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE=mode)
            statuses[mode] = (envelope(job)['status'], envelope(job)['rc'])
        self.assertEqual(statuses['quoted'][0], 'ok')              # a quote is text
        self.assertEqual(statuses['quotedfail'], ('error', 1))     # a failure that quotes one
        self.assertEqual(statuses['quota'][0], 'provider-quota-dead')
        self.assertIn('resets 6:20am', envelope(SB.path('claude-fail-quota'))['reset'])
        self.assertEqual(statuses['authfail'][0], 'auth-expired')
        # an API-key seat out of credit: the same type, with no reset to wait for
        credit = read_text(os.path.join(harness.FIXTURES, 'claude-api-credit-low.txt'))
        job, _ = SB.one_shot('claude-fail-credit', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='quota',
                             STUB_FAIL_TEXT=credit.strip())
        self.assertEqual((envelope(job)['status'], envelope(job)['reset']),
                         ('provider-quota-dead', None))

    def test_a_deadline_mid_turn_is_a_timeout_that_keeps_its_session(self):
        for mode in ('park', 'deaf'):   # deaf ignores the TERM too: only the KILL ends it
            job, _ = SB.one_shot('claude-deadline-' + mode, CLAUDE, '--timeout', '3',
                                 '--cwd', SB.path('tmp'), STUB_MODE=mode)
            end = envelope(job)
            self.assertEqual((end['status'], end['rc'], end['session'], end['turn']),
                             ('timeout', 142, 'sess-stub', 1), mode)
            self.assertGone(job, agent(job).pid)

    def test_half_an_emoji_in_an_answer_still_lands(self):
        # a CLI that cuts a string mid-emoji escapes half of it, which no UTF-8 file can hold
        replay = SB.write('half-emoji.jsonl',
                          '{"type":"system","subtype":"init","session_id":"sess-stub"}\n'
                          '{"type":"result","subtype":"success","is_error":false,'
                          '"result":"cut \\ud83d here","session_id":"sess-stub"}\n')
        job, _ = SB.start('claude-surrogate', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='replay',
                          STUB_REPLAY=replay)
        waited = SB.wait(job, '--timeout', '10')
        SB.stop(job)
        self.assertEqual((waited.rc, envelope(job)['status']), (0, 'ok'))
        self.assertEqual(result(job, 1), 'cut ? here')
        self.assertEqual(turn_ends(job), [(1, 'ok')])

    def test_a_line_separator_inside_a_turn_end_is_no_line_break(self):
        task = SB.write('separator.md', 'echo a b\n')
        job, _ = SB.start('claude-separator', CLAUDE, '--cwd', SB.path('tmp'), task=task,
                          STUB_MODE='echo')
        waited = SB.wait(job, '--timeout', '10')
        SB.stop(job)
        self.assertEqual(waited.rc, 0)
        self.assertEqual((waited.json['status'], waited.json['turn']), ('ok', 1))
        with open(os.path.join(job, 'events.jsonl'), 'rb') as f:
            ends = [line for line in f.read().split(b'\n') if b'"turn_end"' in line]
        self.assertEqual(len(ends), 1)
        self.assertIn(' '.encode(), ends[0])
        self.assertIn(' ', result(job, 1))

    def test_a_send_to_a_busy_seat_never_cancels_its_turn(self):
        job, _ = SB.start('claude-busy', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='slow')
        sent = SB.send(job).json
        self.assertTrue(file_exists(job, 'turn-2', 'envelope.json'))
        SB.stop(job)
        self.assertEqual(turn_ends(job), [(1, 'ok'), (2, 'ok')])
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('ok', 0))
        self.assertTrue(result(job, 1).strip())
        self.assertEqual(len(SB.stub_log(job).seats()), 1)
        self.assertEqual(sent['status'], 'sent')
        self.assertEqual(sent['expect'], 1 if sent['delivery'] == 'steer' else 2)
        cli = agent(job)
        self.assertEqual([json.loads(line).get('type') for line in cli.lines], ['user', 'user'])
        self.assertEqual(cli.ended, ['eof'])   # then the stop's EOF: no control request

    def test_a_stops_rc_is_for_the_turns_it_cut(self):
        job, _ = SB.start('claude-stop-ok', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='slow',
                          STUB_ANSWER_ON_EOF='1')
        self.assertTrue(wait_until(lambda: turn_started(job)))
        self.assertEqual(SB.stop(job).rc, 0)   # EOF releases the turn's ok answer
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('ok', 0))
        self.assertTrue(result(job, 1).startswith('turn 1 pid'))
        job, _ = SB.start('claude-stop-fail', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='slow',
                          STUB_ANSWER_ON_EOF='1', STUB_FAIL='1')
        self.assertTrue(wait_until(lambda: turn_started(job)))
        self.assertEqual(SB.stop(job).rc, 0)   # a failure at EOF is the stop's
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('incomplete', 143))
        self.assertIn('API Error: 500', read_text(os.path.join(job, 'events.jsonl')))

    def test_a_relaunch_runs_the_cli_the_seat_recorded(self):
        job, _ = SB.start('claude-recorded-cli', CLAUDE, '--cwd', SB.path('tmp'))
        SB.wait(job, '--timeout', '10')
        SB.stop(job)
        os.makedirs(SB.path('alt-bin'))
        marker = SB.path('alt-ran')
        wrapper = SB.write('alt-bin/claude', '#!/bin/sh\ntouch "%s"\nexec "%s" "$@"\n'
                           % (marker, os.path.join(harness.STUBS, 'claude')))
        os.chmod(wrapper, 0o755)
        recorded = harness.handle(job)
        recorded['cli'] = wrapper
        with open(os.path.join(job, 'session'), 'w') as f:
            json.dump(recorded, f)
        SB.send(job)
        SB.wait(job, '--timeout', '10')
        SB.stop(job)
        self.assertTrue(os.path.exists(marker))   # not whichever claude PATH finds
        self.assertEqual((envelope(job)['status'], envelope(job)['turn']), ('ok', 2))

    def test_a_send_killed_mid_write_never_fuses_with_the_next(self):
        job, _ = SB.start('claude-torn', CLAUDE, '--cwd', SB.path('tmp'), HANDS_IDLE='60',
                          STUB_MODE='echo')
        SB.wait(job, '--timeout', '10')
        fd = os.open(os.path.join(job, 'in.fifo'), os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, b'{"type": "user", "message": {"role": "user", "content": '
                     b'[{"type": "text", "text": "TORN')   # a record with no end
        os.close(fd)
        SB.send(job)
        SB.wait(job, '--timeout', '10')
        SB.stop(job)
        read = [json.loads(line) for line in agent(job).lines]
        self.assertEqual([r.get('type') for r in read], ['user', 'user'])
        self.assertEqual(read[-1]['message']['content'][0]['text'], read_text(SB.turn_two))
        self.assertEqual((envelope(job, 2)['status'], envelope(job, 2)['turn']), ('ok', 2))
        self.assertEqual(result(job, 2), read_text(SB.turn_two))
        drops = progress(job, 'torn input dropped: ')
        self.assertEqual(len(drops), 1)
        self.assertIn('TORN', drops[0]['text'])

    def test_two_sends_racing_to_a_stopped_seat_relaunch_it_once(self):
        job, _ = SB.start('claude-race', CLAUDE, '--cwd', SB.path('tmp'), HANDS_IDLE='60',
                          STUB_MODE='count')
        SB.wait(job, '--timeout', '10')
        SB.stop(job)
        three = SB.write('turn-three.md', 'turn three\n')
        sends = [SB.popen('send', job, task, STUB_MODE='count', STUB_LOG=job + '.stub.jsonl',
                          HANDS_IDLE='60') for task in (SB.turn_two, three)]
        acks = sorted(harness.last_json(s.communicate(timeout=60)[0])['status'] for s in sends)
        SB.wait(job, '--timeout', '10')
        SB.stop(job)
        self.assertEqual(len(SB.stub_log(job).seats()), 2)   # the start, and ONE relaunch
        self.assertEqual(acks, ['sent', 'spawned'])
        self.assertEqual(turn_ends(job), [(1, 'ok'), (2, 'ok'), (3, 'ok')])
        pids = [result(job, n).split()[-1] for n in (1, 2, 3)]
        self.assertTrue(pids[1] == pids[2] != pids[0])
        # the race above rarely reaches the seat lock; held, it alone keeps a second wrapper out
        with open(os.path.join(job, 'job.log'), 'ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            spawned = SB.run([sys.executable, os.path.join(harness.ROOT, 'seat.py'), 'spawn', job,
                              three], STUB_MODE='count', STUB_LOG=job + '.stub.jsonl')
        SB.stop(job)
        self.assertEqual(spawned.rc, 1)
        self.assertIn('the seat lock stayed taken', spawned.err)
        self.assertEqual(len(SB.stub_log(job).seats()), 2)

    def test_driver_mail_reaches_live_and_stopped_seats_once(self):
        job, _ = SB.start('claude-mail', CLAUDE, '--cwd', SB.path('tmp'), HANDS_IDLE='60',
                          STUB_MODE='echo')
        SB.wait(job, '--timeout', '10')
        self.assertEqual(SB.hands('mail', job, 'from the driver').rc, 0)
        self.assertTrue(file_exists(job, 'turn-2', 'envelope.json'))
        self.assertEqual(result(job, 2), 'MAIL from driver:\nfrom the driver')   # the next turn
        SB.stop(job)
        self.assertEqual(SB.hands('mail', job, 'driver while out').rc, 1)   # no live seat
        self.assertEqual(len(SB.stub_log(job).seats()), 1)                 # and no launch
        SB.send(job, STUB_MODE='echo', HANDS_IDLE='60')
        self.assertTrue(file_exists(job, 'turn-4', 'envelope.json'))
        SB.stop(job)
        SB.send(job, STUB_MODE='echo', HANDS_IDLE='60')
        self.assertTrue(file_exists(job, 'turn-5', 'envelope.json'))
        time.sleep(1.5)
        SB.stop(job)
        self.assertEqual(len(SB.stub_log(job).seats()), 3)
        delivered = [e['inbox'] for e in progress(job) if e.get('inbox')]
        self.assertEqual(delivered, [1, 2])
        self.assertEqual(len(harness.read_jsonl(os.path.join(job, 'inbox.jsonl'))), 2)
        self.assertEqual([result(job, n) for n in (3, 4, 5)],   # the waiting line rides once
                         ['turn two\n', 'MAIL from driver:\ndriver while out', 'turn two\n'])
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-6')))

    def test_a_relaunch_answers_the_send_and_the_mail_it_hands_on_together(self):
        """A stopped seat with mail waiting gets a send: the relaunch hands claude both before
        any turn opens, claude answers each in a turn of its own, and the wrapper stays for the
        second."""
        job, _ = SB.start('claude-relaunch-mail', CLAUDE, '--cwd', SB.path('tmp'))
        SB.wait(job, '--timeout', '10')
        self.assertGone(job)
        self.assertEqual(SB.hands('mail', job, 'mail while down').rc, 1)
        SB.send(job, STUB_MODE='slow', STUB_SLEEP='3')
        self.assertTrue(harness.gone(job, timeout=30))
        relaunch = SB.stub_log(job).seats()[1]
        self.assertEqual([json.loads(line)['message']['content'][0]['text']
                          for line in relaunch.lines],
                         [read_text(SB.turn_two), 'MAIL from driver:\nmail while down'])
        self.assertEqual(relaunch.ended, ['eof'])   # closed after both turns, not mid-turn
        self.assertEqual(turn_ends(job), [(1, 'ok'), (2, 'ok'), (3, 'ok')])
        self.assertEqual([envelope(job, n)['status'] for n in (2, 3)], ['ok', 'ok'])
        self.assertTrue(result(job, 3).startswith('turn 2 pid '))

    def test_the_wrapper_never_waits_for_the_delivery_lock(self):
        job, _ = SB.start('claude-lock', CLAUDE, '--cwd', SB.path('tmp'), HANDS_IDLE='60',
                          STUB_MODE='echo')
        SB.wait(job, '--timeout', '10')
        # a sender holds the lock while it writes 1 MB, more than the pipe holds, into in.fifo
        sender = subprocess.run([sys.executable, '-c', LOCK_HOLDER, job], capture_output=True,
                                text=True, timeout=60)
        self.assertTrue(file_exists(job, 'turn-2', 'envelope.json'))
        SB.stop(job)
        self.assertIn('written', sender.stdout)   # the wrapper kept draining the pipe
        self.assertEqual(result(job, 2), 'MAIL from driver:\npending')   # then delivered the mail

    def test_a_stop_mid_turn_reaps_the_seat(self):
        job, _ = SB.start('claude-stop', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='park')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        self.assertEqual(SB.stop(job).rc, 0)
        self.assertGone(job, agent(job).pid)
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['session'], end['turn']),
                         ('incomplete', 143, 'sess-stub', 1))

    def test_a_cli_deaf_to_eof_and_term_is_killed(self):
        job, _ = SB.start('claude-deaf', CLAUDE, '--cwd', SB.path('tmp'), STUB_MODE='deaf')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        self.assertEqual(SB.stop(job).rc, 0)
        self.assertGone(job, agent(job).pid)
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['turn']), ('incomplete', 143, 1))


LOCK_HOLDER = '''
import fcntl, json, os, signal, sys, time
job = sys.argv[1]
lock = open(os.path.join(job, 'events.jsonl'), 'ab')
fcntl.flock(lock, fcntl.LOCK_EX)
with open(os.path.join(job, 'inbox.jsonl'), 'a') as f:
    f.write(json.dumps({'t': 'x', 'from': 'driver', 'text': 'pending'}) + '\\n')
time.sleep(2)   # the wrapper sees the mail and tries the lock
signal.alarm(10)
fd = os.open(os.path.join(job, 'in.fifo'), os.O_WRONLY | os.O_NONBLOCK)
os.set_blocking(fd, True)
data = b'\\n' + b'x' * (1 << 20) + b'\\n'   # not a record: the wrapper drops it
while data:
    data = data[os.write(fd, data):]
print('written')
'''


if __name__ == '__main__':
    harness.main('test_seat_claude')
