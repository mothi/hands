#!/usr/bin/env python3
"""OpenCode ACP launch, turn delivery, resume and shutdown against the stub CLI."""
import json
import os
import signal
import subprocess
import time
import unittest

import harness
from harness import (OPENCODE_SESSION, SeatChecks, agent, envelope, events, first_index,
                     progress, read_text, result, turn_ends, turn_started, wait_until)

import wires

OPENCODE = harness.SPECS['opencode']
SB = None


def setUpModule():
    global SB
    SB = harness.SeatSandbox('acp')
    os.makedirs(SB.path('gdir'))


def tearDownModule():
    SB.close()


def seat_transcript():
    return harness.without_cancel(harness.transcript('opencode-acp-seat-sample.jsonl'))


def hung():
    """A transcript whose first prompt never answers."""
    return SB.save_transcript('opencode-hung.jsonl', harness.hung_on_first_prompt(seat_transcript()))


def prompted(job):
    return turn_started(job) and 'session/prompt' in agent(job).methods()


def first_prompt_fails(rows, text):
    """The first prompt answered by a JSON-RPC error carrying text."""
    prompt = harness.request_index(rows, 'session/prompt')
    error = {'code': -32603, 'message': text.strip()}
    reply = {'jsonrpc': '2.0', 'id': rows[prompt]['msg']['id'], 'error': error}
    return rows[:prompt + 1] + [{'dir': 'in', 'msg': reply}]


def with_delays(rows, seconds, count):
    """A pause of `seconds` before the answers to each of the first `count` client lines."""
    edited = []
    for row in rows:
        edited.append(row)
        if row['dir'] == 'out' and count > 0:
            count -= 1
            edited.append({'dir': 'sleep', 'msg': seconds})
    return edited


class AcpSeats(SeatChecks):
    """The CLI's recorded 4-turn seat on one agent (kept up between turns by HANDS_IDLE), then a
    relaunch that resumes it."""

    @classmethod
    def setUpClass(cls):
        cls.opencode = SB.path('acp-opencode')
        run = SB.hands('start', '--effort', 'low', '--cwd', SB.path('gdir'), '--out',
                       'acp-opencode', OPENCODE, SB.task, OPENCODE_API_KEY='sk-caller-env',
                       HANDS_IDLE='60', STUB_LOG=cls.opencode + '.stub.jsonl')   # a relative --out
        cls.opencode_ack = run.json
        cls.before = {}
        job = cls.opencode
        for _ in range(3):
            SB.wait(job)
            SB.send(job)
        SB.wait(job)
        stream = harness.read_jsonl(os.path.join(job, 'stream.jsonl'))
        cls.before[job] = (events(job), result(job), stream)
        SB.stop(job)
        resume = os.path.join(harness.FIXTURES, 'opencode-acp-resume-sample.jsonl')
        cls.opencode_resume = SB.send(cls.opencode, STUB_REPLAY=resume,
                                      OPENCODE_API_KEY='sk-caller-env').json
        SB.wait(cls.opencode)
        SB.stop(cls.opencode)

    def assertFourTurnsOnOneAgent(self, job, ack, session, option):
        self.assertEqual((ack['status'], ack['expect'], ack['delivery']), ('spawned', 1, 'queue'))
        first = agent(job)
        self.assertEqual(first.methods(), ['initialize', 'session/new',
                                           'session/set_config_option'] + ['session/prompt'] * 4)
        params = first.messages()[2][1]
        self.assertEqual((params['configId'], params['value']), (option, 'low'))   # before a prompt
        for turn in (1, 2):
            end = envelope(job, turn)
            self.assertEqual((end['status'], end['session'], end['effort'], end['delivery']),
                             ('ok', session, 'low', 'queue'))
            self.assertEqual(result(job, turn), 'PONG')
        self.assertEqual(first.cwd, harness.handle(job)['cwd'])   # the agent runs in the seat's cwd

    def test_an_opencode_seat_runs_a_bare_acp_and_sets_its_effort(self):
        self.assertFourTurnsOnOneAgent(self.opencode, self.opencode_ack, OPENCODE_SESSION,
                                       'effort')
        first = agent(self.opencode)
        self.assertEqual(first.argv, ['acp'])
        self.assertNotIn('OPENCODE_CONFIG_CONTENT', first.env)   # 2.x takes no model from it

    def test_the_wire_speaks_the_event_vocabulary(self):
        kinds = ['turn_start', 'text', 'thought', 'tool_call', 'tool_result', 'file', 'usage',
                 'turn_end', 'progress']
        found, text, _ = self.before[self.opencode]
        self.assertVocabulary(found, kinds, ['ok', 'ok', 'cancelled', 'ok'], 'DONE', text)

    def test_a_cancelled_turn(self):
        job = self.opencode
        turn = [e for e in self.before[job][0] if e['turn'] == 3]
        end = envelope(job, 3)
        self.assertEqual((end['status'], end['rc']), ('cancelled', 1))
        self.assertEqual(result(job, 3), '')
        calls = [e['call'] for e in turn if e['kind'] == 'tool_call']
        uses = [e for e in turn if e['kind'] == 'usage']
        closes = [(e['call'], e['status'], e.get('synthetic')) for e in turn
                  if e['kind'] == 'tool_result']
        self.assertEqual((len(calls), len(uses)), (1, 1))
        # opencode closes its own, and reports zeros: usage unknown
        self.assertEqual(closes, [(calls[0], 'ok', None)])
        self.assertIs(uses[0].get('usage_unknown'), True)
        self.assertIsNone(end['usage'])

    def test_one_edit_reported_twice_is_one_file_event(self):
        # opencode sends an in_progress update again unchanged (turn 3's shell call has two);
        # here the edit's update of turn 4 comes twice
        rows = seat_transcript()
        edit = next(i for i, r in enumerate(rows) if r['dir'] == 'in'
                    and ((r['msg'].get('params') or {}).get('update') or {}).get('kind') == 'edit'
                    and r['msg']['params']['update'].get('locations'))
        rows.insert(edit + 1, rows[edit])
        job, _ = SB.start('acp-edit-twice', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          HANDS_IDLE='60', STUB_REPLAY=SB.save_transcript('oc-edit-twice.jsonl', rows))
        for _ in range(3):
            SB.wait(job)
            SB.send(job)
        SB.wait(job)
        SB.stop(job)
        stream = harness.read_jsonl(os.path.join(job, 'stream.jsonl'))
        updates = [(r['msg'].get('params') or {}).get('update') for r in stream
                   if r['dir'] == 'in' and isinstance(r['msg'], dict)]
        raw = [e for u in updates if isinstance(u, dict) and 'sessionUpdate' in u
               for e in wires.acp_events(u, 0) if e['kind'] == 'file']
        kept = [e for e in events(job) if e['kind'] == 'file' and e['turn'] == 4]
        self.assertEqual(len(raw), 2)   # the wire reports the one edit twice
        self.assertEqual([(e['call'], e['path']) for e in kept], [(raw[0]['call'], raw[0]['path'])])

    def test_usage_counts_reasoning_as_output(self):
        self.assertEqual(envelope(self.opencode, 4)['usage'],   # opencode's: 3 + 82 thought
                         {'in': 301, 'out': 85, 'cache_read': 12672, 'reasoning': 82})

    def assertResumed(self, job, ack, session):
        self.assertEqual(ack['expect'], 5)
        launches = SB.stub_log(job).seats()
        self.assertEqual(len(launches), 2)
        second = launches[1]
        self.assertEqual(second.methods(), ['initialize', 'session/resume',
                                            'session/set_config_option', 'session/prompt'])
        resume, option = second.messages()[1][1], second.messages()[2][1]
        self.assertEqual((resume['sessionId'], resume['cwd'], option['value']),
                         (session, harness.handle(job)['cwd'], 'low'))
        end = envelope(job)
        self.assertEqual((end['turn'], end['status'], end['session']), (5, 'ok', session))
        self.assertEqual(result(job, 5), 'PONG')
        return second

    def test_a_send_to_a_stopped_opencode_seat_resumes_its_session(self):
        second = self.assertResumed(self.opencode, self.opencode_resume, OPENCODE_SESSION)
        self.assertEqual((second.argv, second.cwd), (['acp'], harness.handle(self.opencode)['cwd']))
        self.assertNotIn('OPENCODE_CONFIG_CONTENT', second.env)

    def test_an_opencode_seat_runs_on_its_callers_store(self):
        launches = SB.stub_log(self.opencode).launches()
        self.assertEqual(len(launches), 2)   # one run per launch: no `auth import` before it
        for launch in launches:
            self.assertEqual(launch.argv, ['acp'])
            self.assertEqual(launch.values.get('XDG_DATA_HOME'), SB.store)
            self.assertNotIn('OPENCODE_API_KEY', launch.env)   # though the caller exported one
        self.assertFalse(os.path.exists(os.path.join(self.opencode, 'xdg')))
        line = read_text(os.path.join(self.opencode, 'prompt.txt')).rstrip('\n').split('\n')[-1]
        self.assertEqual(line, 'YOUR JOB DIR: %s (your outbox: %s/outbox.jsonl).'
                         % (self.opencode, self.opencode))   # absolute, from a relative --out

    def test_an_idle_stop_deletes_no_session(self):
        self.assertEqual([x for x in SB.stub_log(self.opencode).launches()
                          if x.argv[:1] == ['session']], [])

    def test_each_seat_runs_a_private_agent(self):
        for launch in SB.stub_log(self.opencode).seats():
            self.assertEqual(launch.argv, ['acp'])   # its own child server: no attach flag exists


def session_on_model(rows, model):
    """The new session starts on another model; the client must set the recorded one."""
    rows = json.loads(json.dumps(rows))
    new = harness.request_index(rows, 'session/new')
    answer = next(a for a in range(new + 1, len(rows))
                  if 'configOptions' in (harness.rpc_message(rows[a]).get('result') or {}))
    after = next(b for b in range(answer + 1, len(rows)) if rows[b]['dir'] == 'out')
    reply = harness.rpc_message(rows[answer])['result']
    options = json.loads(json.dumps(reply['configOptions']))
    wanted = next(o for o in options if o['id'] == 'model')['currentValue']
    next(o for o in reply['configOptions'] if o['id'] == 'model')['currentValue'] = model
    params = {'sessionId': reply.get('sessionId'), 'configId': 'model', 'value': wanted}
    rows[after:after] = [
        {'dir': 'out', 'msg': {'jsonrpc': '2.0', 'id': 9001,
                               'method': 'session/set_config_option', 'params': params}},
        {'dir': 'in', 'msg': {'jsonrpc': '2.0', 'id': 9001, 'result': {'configOptions': options}}},
    ]
    return rows


class AcpLaunch(SeatChecks):
    """What an ACP launch sets, refuses, and how its failures are typed."""

    def test_a_session_on_another_model_is_set_to_the_seats(self):
        rows = session_on_model(seat_transcript(), 'opencode-go/glm-5.3')
        job, _ = SB.start('acp-model', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=SB.save_transcript('oc-model.jsonl', rows))
        SB.wait(job)
        SB.stop(job)
        first = agent(job)
        self.assertEqual(first.methods()[:5], ['initialize', 'session/new',
                                               'session/set_config_option',
                                               'session/set_config_option', 'session/prompt'])
        sets = [(p['configId'], p['value']) for _, p in first.messages()[2:4]]
        self.assertEqual(sets, [('model', 'opencode-go/deepseek-v4.1-flash'), ('effort', 'low')])
        self.assertEqual((envelope(job, 1)['status'], result(job, 1)), ('ok', 'PONG'))

    def test_what_the_agent_does_not_list_is_refused_before_any_prompt(self):
        refusals = [
            SB.one_shot('acp-effort-xhigh', OPENCODE, '--effort', 'xhigh', '--cwd', SB.path('gdir'))
            + ('effort-unsupported',),
            SB.one_shot('acp-no-effort', OPENCODE, '--cwd', SB.path('gdir'))
            + ('effort-unsupported',),   # the engine never guesses one
            SB.start('acp-no-model', 'opencode:opencode-go/nope-9.9', '--effort', 'low',
                     '--cwd', SB.path('gdir')) + ('model-mismatch',),       # never substituted
        ]
        for job, run, reason in refusals:
            self.assertEqual(run.rc, 2, job)
            end = envelope(job)
            self.assertEqual((end['status'], end['rc'], end['reason']), ('refused', 2, reason))
            methods = agent(job).methods()
            self.assertIn('initialize', methods)
            self.assertNotIn('session/prompt', methods)
            self.assertFalse(os.path.exists(os.path.join(job, 'turn-1')))
        why = envelope(refusals[2][0])['reset']
        self.assertIn('runs opencode-go/deepseek-v4.1-flash and lists '
                      'opencode-go/deepseek-v4-flash, opencode-go/deepseek-v4-flash-vision-exp, ',
                      why)
        self.assertIn('; the seat names opencode-go/nope-9.9', why)

    def test_a_usage_limit_answering_a_prompt_is_provider_quota_dead(self):
        text = read_text(os.path.join(harness.FIXTURES, 'opencode-openrouter-402.txt'))
        rows = first_prompt_fails(harness.transcript('opencode-acp-seat-sample.jsonl'),
                                          text)
        job, _ = SB.one_shot('acp-402', OPENCODE, '--effort', 'low', STUB_REPLAY=SB.save_transcript('402.jsonl', rows))
        self.assertEqual((envelope(job)['status'], envelope(job)['session']),
                         ('provider-quota-dead', OPENCODE_SESSION))

    def test_a_missing_login_answering_a_prompt_is_auth_expired(self):
        text = read_text(os.path.join(harness.FIXTURES, 'opencode-missing-api-key.txt'))
        rows = first_prompt_fails(harness.transcript('opencode-acp-seat-sample.jsonl'),
                                          text)
        job, _ = SB.one_shot('acp-no-key', OPENCODE, '--effort', 'low',
                             STUB_REPLAY=SB.save_transcript('no-key.jsonl', rows))
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['session']),
                         ('auth-expired', 1, OPENCODE_SESSION))

    def test_a_stop_during_the_launch(self):
        rows = seat_transcript()
        cases = {'ends-3s': with_delays(rows, 3, 1),        # the handshake ends 3 s in
                 'still-going': with_delays(rows, 35, 2)}   # still answering at the stop
        for name, case in cases.items():
            job = SB.path('acp-stop-launch-' + name)
            starting = SB.popen('start', '--effort', 'low', '--cwd', SB.path('gdir'), '--out', job,
                                OPENCODE, SB.task, STUB_REPLAY=SB.save_transcript(name + '.jsonl', case),
                                STUB_LOG=job + '.stub.jsonl')
            self.assertTrue(wait_until(lambda: SB.stub_log(job).launches()
                                       and agent(job).lines))   # the wrapper is in the handshake
            stopped = SB.stop(job)
            starting.communicate(timeout=60)
            self.assertEqual(stopped.rc, 0)
            end = envelope(job)
            self.assertEqual((end['status'], end['rc']), ('incomplete', 143), name)
            self.assertNotIn('Traceback', read_text(os.path.join(job, 'job.log')))
            self.assertGone(job)
            if name == 'ends-3s':
                self.assertEqual(end['turn'], 1)
                self.assertNotIn('session/prompt', agent(job).methods())   # MSG was never sent


class AcpClose(SeatChecks):
    """Stops, deadlines and what an ACP seat leaves behind when one cuts its turn."""

    def test_a_stop_mid_turn_reaps_the_agent_and_its_group(self):
        job, _ = SB.start('acp-stop', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=hung(), STUB_MODE='stubborn')
        self.assertTrue(wait_until(lambda: turn_started(job) and agent(job).children))
        self.assertEqual(SB.stop(job).rc, 0)
        cli = agent(job)
        self.assertIn('term', cli.ended)   # it ignores stdin EOF: the TERM ended it
        self.assertGone(job, *cli.children)
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['turn'], end['session']),
                         ('incomplete', 143, 1, OPENCODE_SESSION))

    def test_a_deadline_mid_turn_is_a_timeout_that_keeps_its_session(self):
        job, _ = SB.one_shot('acp-deadline', OPENCODE, '--effort', 'low', '--timeout', '3',
                             '--cwd', SB.path('gdir'), STUB_REPLAY=hung())
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['turn'], end['session']),
                         ('timeout', 142, 1, OPENCODE_SESSION))
        self.assertEqual(agent(job).ended[:1], ['eof'])   # closed politely: stdin first, no TERM

    def test_a_turn_the_agent_cancels_at_the_deadline_is_still_a_timeout(self):
        job, _ = SB.one_shot('acp-deadline-oc', OPENCODE, '--effort', 'low', '--timeout', '3',
                             '--cwd', SB.path('gdir'), STUB_REPLAY=hung(),
                             STUB_MODE='termcancel')
        end = envelope(job)
        self.assertEqual((end['status'], end['rc'], end['turn'], end['session']),
                         ('timeout', 142, 1, OPENCODE_SESSION))
        self.assertEqual(agent(job).ended[:1], ['eof'])
        self.assertEqual([x for x in SB.stub_log(job).launches() if x.argv[:1] == ['session']], [])

    def deletes(self, job):
        return [x for x in SB.stub_log(job).launches() if x.argv[:1] == ['session']]

    def test_an_abandoned_opencode_turn_has_its_session_deleted(self):
        job, _ = SB.start('acp-abandoned', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=hung())
        self.assertTrue(wait_until(lambda: prompted(job)))
        SB.stop(job)
        refused = SB.save_transcript('oc-gone.jsonl', harness.resume_refused(
            harness.transcript('opencode-acp-resume-sample.jsonl')))
        started = time.time()
        later = SB.send(job, STUB_REPLAY=refused)
        self.assertLess(time.time() - started, 30)
        self.assertEqual(agent(job).ended[:1], ['eof'])   # the stop closed it politely
        deletes = self.deletes(job)
        self.assertEqual([(d.argv, d.cwd) for d in deletes],
                         [(['session', 'delete', OPENCODE_SESSION, '--standalone'],
                           SB.path('gdir'))])
        found = events(job)
        deleted = first_index(found, lambda e: e['kind'] == 'progress' and str(
            e.get('text', '')).startswith('session %s deleted (rc 0)' % OPENCODE_SESSION))
        ended = first_index(found, lambda e: e['kind'] == 'turn_end')
        self.assertTrue(-1 < deleted < ended)   # before the turn's envelope lands
        end = envelope(job, 1)
        self.assertEqual((end['status'], end['rc'], end['session']),
                         ('incomplete', 143, OPENCODE_SESSION))
        self.assertEqual(later.rc, 1)
        self.assertEqual((later.json['status'], later.json['reason'], later.json['session']),
                         ('error', 'handshake', OPENCODE_SESSION))

    def test_an_agent_that_dies_with_an_error_mid_turn_has_its_session_deleted(self):
        job, _ = SB.start('acp-term-error', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=hung(), STUB_MODE='termerror')
        self.assertTrue(wait_until(lambda: prompted(job)))
        os.kill(agent(job).pid, signal.SIGTERM)   # from outside: a stop would close it politely
        SB.wait(job, '--for', 'exit', '--timeout', '30')
        SB.stop(job)
        deletes = self.deletes(job)
        self.assertEqual(len(deletes), 1)
        self.assertIn(OPENCODE_SESSION, deletes[0].argv)
        self.assertEqual(deletes[0].cwd, SB.path('gdir'))

    def test_an_acp_seat_ends_with_its_turn_and_a_send_resumes_it(self):
        job, _ = SB.start('acp-ends', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'))
        SB.wait(job, '--timeout', '30')
        self.assertGone(job)   # no stop: the wrapper left when turn 1 ended
        first = agent(job)
        self.assertEqual(first.methods(), ['initialize', 'session/new', 'session/set_config_option',
                                           'session/prompt'])
        self.assertEqual(first.ended, ['eof'])
        resume = os.path.join(harness.FIXTURES, 'opencode-acp-resume-sample.jsonl')
        ack = SB.send(job, STUB_REPLAY=resume).json
        SB.wait(job, '--timeout', '30')
        self.assertGone(job)
        self.assertEqual((ack['status'], ack['expect']), ('spawned', 2))
        second = agent(job, 1)
        self.assertEqual(second.methods(), ['initialize', 'session/resume',
                                            'session/set_config_option', 'session/prompt'])
        self.assertEqual(second.messages()[1][1]['sessionId'], OPENCODE_SESSION)
        self.assertEqual([envelope(job, n)['status'] for n in (1, 2)], ['ok', 'ok'])
        self.assertEqual(result(job, 2), 'PONG')
        self.assertEqual([e.get('idle') for e in progress(job, 'exit rc=')], [True, True])

    def test_a_seat_started_without_a_timeout_has_no_deadline(self):
        job, _ = SB.start('acp-no-limit', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=hung())
        self.assertTrue(wait_until(lambda: prompted(job)))
        wrapper = harness.wrapper_pids(job)[0]
        table = subprocess.run(['ps', '-A', '-ww', '-o', 'ppid=,command='], capture_output=True,
                               text=True).stdout
        children = [line for line in table.splitlines() if line.split()[:1] == [str(wrapper)]]
        SB.stop(job)
        self.assertIsNone(harness.handle(job)['timeout'])
        self.assertTrue(any('seat.py reap 0 ' in line for line in children), children)

    def test_driver_mail_to_a_busy_seat_waits_behind_its_turn(self):
        job, _ = SB.start('acp-mail', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_MODE='slow', STUB_SLEEP='4')
        SB.hands('mail', job, 'while you work')
        # two 4-second turns: more than the default 10-second wait on a slow runner
        self.assertTrue(wait_until(lambda: os.path.exists(
            os.path.join(job, 'turn-2', 'envelope.json')), 30))
        SB.stop(job)
        found = events(job)
        marker = first_index(found, lambda e: e['kind'] == 'progress' and e.get('inbox') == 1)
        end_1 = first_index(found, lambda e: e['kind'] == 'turn_end' and e['turn'] == 1)
        self.assertTrue(-1 < marker < end_1)
        self.assertEqual(found[marker]['expect'], 2)   # it queues: the next turn answers it
        self.assertEqual(turn_ends(job), [(1, 'ok'), (2, 'ok')])
        prompts = [p['prompt'][0]['text'] for m, p in agent(job).messages()
                   if m == 'session/prompt']
        self.assertEqual(prompts[1], 'MAIL from driver:\nwhile you work')

    def test_a_send_while_a_turn_runs_queues_behind_it(self):
        job, _ = SB.start('acp-queue', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_MODE='slow')
        sent = SB.send(job).json
        SB.wait(job, '--timeout', '30')
        SB.stop(job)
        self.assertEqual((sent['status'], sent['expect'], sent['delivery']), ('sent', 2, 'queue'))
        found = events(job)
        marker = first_index(found, lambda e: e['kind'] == 'progress' and e.get('expect') == 2)
        end_1 = first_index(found, lambda e: e['kind'] == 'turn_end' and e['turn'] == 1)
        start_2 = first_index(found, lambda e: e['kind'] == 'turn_start' and e['turn'] == 2)
        self.assertTrue(-1 < marker < end_1 < start_2)
        self.assertEqual(turn_ends(job), [(1, 'ok'), (2, 'ok')])
        self.assertEqual(len(SB.stub_log(job).seats()), 1)
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('ok', 0))
        self.assertTrue(result(job, 1).strip())
        prompts = [m for m in agent(job).methods()
                   if m.startswith('session/') and m != 'session/set_config_option']
        self.assertEqual(prompts, ['session/new', 'session/prompt', 'session/prompt'])   # no cancel


class SlowLaunch(unittest.TestCase):
    def test_a_launch_slower_than_a_minute_still_acks(self):
        # Both replies are within the per-request bound, but the launch takes over a minute.
        slow = SB.save_transcript('opencode-slow.jsonl', with_delays(seat_transcript(), 35, 2))
        started = time.monotonic()
        _, run = SB.start('slow-launch', OPENCODE, '--effort', 'low', '--cwd', SB.path('gdir'),
                          STUB_REPLAY=slow)
        self.assertEqual(run.rc, 0, run.err)
        self.assertEqual(run.json['status'], 'spawned')
        self.assertGreater(time.monotonic() - started, 60)


if __name__ == '__main__':
    harness.main('test_seat_acp')
