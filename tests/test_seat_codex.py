#!/usr/bin/env python3
"""Codex app-server seats against the stub CLI and recorded exchange."""
import os
import signal
import time

import harness
from harness import (CODEX_THREAD, SeatChecks, agent, envelope, events, file_exists,
                     first_index, progress, read_text, result, turn_ends, wait_until)

CODEX = harness.SPECS['codex']
CODEX_ARGV = ['--no-daemon', '--disable', 'memories', '--disable', 'hooks',
              '-c', 'web_search="live"', 'app-server']
CODEX_USAGE = {'in': 6, 'out': 3, 'cache_read': 4, 'reasoning': 1}   # the stub's two calls
SB = None


def setUpModule():
    global SB
    SB = harness.SeatSandbox('codex')


def tearDownModule():
    SB.close()


def strays(job):
    """The lines that arrived after their turn ended, by what they were."""
    return [e['text'].split(' belongs')[0] for e in progress(job, 'belongs to no open turn')]


class CodexSeats(SeatChecks):
    def turn_input(self, path, effort=None):
        params = {'threadId': CODEX_THREAD, 'input': [{'type': 'text', 'text': read_text(path)}]}
        if effort:
            params['effort'] = effort
        return params

    def test_a_seat_ends_with_its_turn_and_a_send_resumes_it(self):
        job, run = SB.start('codex-seat', CODEX, '--effort', 'high', '--cwd', SB.work)
        SB.wait(job, '--timeout', '30')
        self.assertGone(job)   # the wrapper leaves once its turn has ended: no stop needed
        first = SB.stub_log(job).seats()[0]
        self.assertEqual(first.ended, ['eof'])   # closed as an idle stop closes: no interrupt
        sent = SB.send(job).json
        SB.wait(job, '--timeout', '30')
        self.assertGone(job)
        launches = SB.stub_log(job).seats()
        self.assertEqual(len(launches), 2)
        first, second = launches
        self.assertEqual((first.argv, first.cwd), (CODEX_ARGV, SB.work))
        self.assertEqual(first.methods(), ['initialize', 'initialized', 'thread/start',
                                           'turn/start'])
        self.assertEqual(first.messages()[2][1], {'cwd': SB.work, 'sandbox': 'danger-full-access',
                                                  'approvalPolicy': 'never',
                                                  'model': 'gpt-5.6-terra', 'ephemeral': False})
        self.assertEqual(second.methods(), ['initialize', 'initialized', 'thread/resume',
                                            'turn/start'])   # the same thread, with its history
        self.assertEqual(second.messages()[2][1]['threadId'], CODEX_THREAD)
        self.assertEqual([first.messages()[3][1], second.messages()[3][1]],
                         [self.turn_input(os.path.join(job, 'prompt.txt'), 'high'),
                          self.turn_input(SB.turn_two, 'high')])
        self.assertEqual((run.json['status'], run.json['expect'], run.json['delivery']),
                         ('spawned', 1, 'steer'))
        self.assertEqual((sent['status'], sent['expect']), ('spawned', 2))
        for turn in (1, 2):
            end = envelope(job, turn)
            self.assertEqual((end['status'], end['session'], end['turn']),
                             ('ok', CODEX_THREAD, turn))
            self.assertEqual(result(job, turn), 'stub ok')
            self.assertEqual(end['usage'], CODEX_USAGE)   # input excludes the cached part
        self.assertEqual([e.get('idle') for e in progress(job, 'exit rc=')], [True, True])

    def test_an_idle_grace_keeps_one_app_server_for_the_next_send(self):
        job, _ = SB.start('codex-idle', CODEX, '--effort', 'high', '--cwd', SB.work,
                          HANDS_IDLE='60')
        SB.wait(job, '--timeout', '30')
        time.sleep(1.5)
        idle = bool(harness.wrapper_pids(job))   # the wrapper stays: a send is taken live
        sent = SB.send(job).json
        SB.wait(job, '--timeout', '30')
        stopped = SB.stop(job)
        launches = SB.stub_log(job).seats()
        self.assertEqual(len(launches), 1)
        cli = launches[0]
        self.assertEqual(cli.methods(), ['initialize', 'initialized', 'thread/start', 'turn/start',
                                         'turn/start'])
        self.assertEqual(cli.ended, ['eof'])   # an idle stop closes stdin: no interrupt
        self.assertEqual([p for _, p in cli.messages()[3:5]],
                         [self.turn_input(os.path.join(job, 'prompt.txt'), 'high'),
                          self.turn_input(SB.turn_two, 'high')])
        self.assertTrue(idle)
        self.assertEqual((sent['status'], sent['expect']), ('sent', 2))
        self.assertEqual([envelope(job, n)['status'] for n in (1, 2)], ['ok', 'ok'])
        self.assertEqual(stopped.rc, 0)
        self.assertGone(job)

    def test_a_recorded_wire_plays_four_turns_across_a_relaunch(self):
        replay = os.path.join(harness.FIXTURES, 'codex-appserver-sample.jsonl')
        job, _ = SB.start('codex-replay', 'codex:gpt-6-luna', '--effort', 'low', '--cwd', SB.work,
                          HANDS_IDLE='60', STUB_REPLAY=replay)
        SB.wait(job, '--timeout', '30')
        for turn in (2, 3, 4):
            if turn == 4:
                SB.stop(job)   # the fourth turn rides a resumed thread
            SB.send(job, STUB_REPLAY=replay)
            SB.wait(job, '--timeout', '30')
        SB.stop(job)
        thread = '01a193b2-8163-7903-9113-176b78d910aa'
        found = events(job)
        self.assertVocabulary(found, ['text', 'tool_call', 'usage', 'turn_end'],
                              ['ok', 'cancelled', 'ok', 'ok'], 'STEERED', result(job))
        self.assertEqual([result(job, n) for n in (1, 2, 3, 4)], ['SECOND', '', 'AFTER', 'STEERED'])
        self.assertEqual(len(SB.stub_log(job).seats()), 2)
        self.assertTrue(all(envelope(job, n)['session'] == thread for n in (1, 2, 3, 4)))
        # turn 1's command finished 14 s after turn 1 completed: a stray, never turn 2's. The
        # usage the resume re-sends before turn 4 opens is dropped without a line.
        self.assertEqual(strays(job), ['item/completed commandExecution'])
        late = 'exec-fb7fdd53-11e2-4278-b1ed-c5d03bdf103c'
        self.assertEqual([e['turn'] for e in found if e.get('call') == late], [1])
        self.assertFalse([e for e in found if e['kind'] == 'text' and e['turn'] == 1
                          and e['text'] != 'SECOND'])   # steered mid-turn: the final message only
        usage = [{'in': 4995, 'out': 36, 'cache_read': 29568, 'cache_write': 0, 'reasoning': 0},
                 {'in': 5441, 'out': 39, 'cache_read': 17152, 'cache_write': 0, 'reasoning': 0},
                 {'in': 312, 'out': 6, 'cache_read': 22400, 'cache_write': 0, 'reasoning': 0},
                 {'in': 3411, 'out': 7, 'cache_read': 22528, 'cache_write': 0, 'reasoning': 0}]
        self.assertEqual([envelope(job, n)['usage'] for n in (1, 2, 3, 4)], usage)   # per turn
        self.assertEqual([e['usage'] for e in found if e['kind'] == 'usage'], usage)

    def test_the_stream_log_leaves_out_fragments_and_the_resent_diff(self):
        """stream.jsonl keeps every message but those that repeat what it holds whole: a streamed
        fragment, whose item comes whole at item/completed, and the turn's diff, which codex
        sends again whole at every change."""
        rows = harness.transcript('codex-appserver-sample.jsonl')
        started = first_index(rows, lambda r: r['dir'] == 'in' and isinstance(r['msg'], dict)
                              and r['msg'].get('method') == 'turn/started')
        turn = rows[started]['msg']['params']
        diff = {'method': 'turn/diff/updated',
                'params': {'threadId': turn['threadId'], 'turnId': turn['turn']['id'],
                           'diff': 'diff --git a/x b/x\n+x\n'}}
        rows[started + 1:started + 1] = [dict(rows[started], msg=diff)] * 2
        job, _ = SB.start('codex-fragments', 'codex:gpt-6-luna', '--effort', 'low', '--cwd',
                          SB.work, HANDS_IDLE='60', STUB_REPLAY=SB.save_transcript('codex-fragments.jsonl', rows))
        SB.wait(job, '--timeout', '30')
        time.sleep(1)   # the command turn 1 left running reports just after the turn's end
        SB.stop(job)
        logged = [r['msg'].get('method')
                  for r in harness.read_jsonl(os.path.join(job, 'stream.jsonl'))
                  if r['dir'] == 'in' and isinstance(r['msg'], dict)]
        left_out = ('item/agentMessage/delta', 'item/commandExecution/outputDelta',
                    'turn/diff/updated')   # the recorded turn 1 holds one of each kind or more
        self.assertEqual([m for m in logged if m in left_out], [])
        for kept in ('turn/started', 'item/started', 'item/completed', 'thread/tokenUsage/updated',
                     'account/rateLimits/updated', 'turn/completed'):
            self.assertIn(kept, logged)
        self.assertIn(None, logged)   # the answers to the wrapper's own requests
        self.assertEqual((envelope(job, 1)['status'], result(job, 1)), ('ok', 'SECOND'))
        texts = [e['text'] for e in events(job) if e['kind'] == 'text' and e['turn'] == 1]
        self.assertEqual(texts, ['SECOND'])   # the whole item, never its fragments

    def test_file_changes_become_file_events(self):
        job, _ = SB.one_shot('codex-files', CODEX, '--cwd', SB.work, STUB_MODE='files')
        found = events(job)
        self.assertEqual([(e['call'], e['path']) for e in found if e['kind'] == 'file'],
                         [('fc-7', '/w/file-1.txt'), ('fc-11', '/w/file-2.txt'),
                          ('fc-11', '/w/file-1.txt')])
        self.assertEqual([e['call'] for e in found
                          if e['kind'] == 'tool_result' and e['status'] == 'error'], ['exec-8'])
        self.assertEqual((envelope(job)['status'], result(job)), ('ok', 'FINAL-3: the audit'))

    def test_failures_are_typed_from_error_fields_only(self):
        limit = read_text(os.path.join(harness.FIXTURES, 'codex-usage-limit.txt'))
        statuses = {}
        for mode in ('quota', 'noticedeath', 'notice', 'quoted', 'quotedfail'):
            job, _ = SB.one_shot('codex-fail-' + mode, CODEX, '--cwd', SB.work, STUB_MODE=mode,
                                 STUB_FAIL_TEXT=limit)
            statuses[mode] = envelope(job)['status']
        self.assertEqual(statuses, {'quota': 'provider-quota-dead',
                                    'noticedeath': 'provider-quota-dead',   # the notice types it
                                    'notice': 'ok',                         # a reconnect notice
                                    'quoted': 'ok',                         # a quote is text
                                    'quotedfail': 'error'})
        self.assertIn('Aug', envelope(SB.path('codex-fail-quota'))['reset'])
        billing = read_text(os.path.join(harness.FIXTURES, 'codex-api-insufficient-quota.txt'))
        job, _ = SB.one_shot('codex-fail-billing', CODEX, '--cwd', SB.work, STUB_MODE='quota',
                             STUB_FAIL_TEXT=billing.strip())
        self.assertEqual(envelope(job)['status'], 'provider-quota-dead')
        # the turn the dead app-server cut keeps the usage it reported, normalized
        self.assertEqual(envelope(SB.path('codex-fail-noticedeath'))['usage'],
                         {'in': 4, 'out': 2, 'cache_read': 2, 'reasoning': 1})
        job, _ = SB.one_shot('codex-refused', CODEX, '--cwd', SB.work, STUB_MODE='refuse',
                             STUB_FAIL_TEXT='invalid params: effort')
        self.assertEqual((envelope(job)['status'], envelope(job)['turn']), ('error', 1))
        self.assertIn('turn/start refused: -32602 invalid params: effort',
                      read_text(os.path.join(job, 'events.jsonl')))

    def test_a_send_while_a_turn_runs_is_steered_into_it(self):
        job, _ = SB.start('codex-steer', CODEX, '--cwd', SB.work, STUB_MODE='slow')
        sent = SB.send(job).json
        SB.wait(job, '--timeout', '30')
        SB.stop(job)
        self.assertEqual((sent['status'], sent['expect'], sent['delivery']), ('sent', 1, 'steer'))
        launches = SB.stub_log(job).seats()
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0].methods(), ['initialize', 'initialized', 'thread/start',
                                                 'turn/start', 'turn/start'])
        self.assertEqual(launches[0].messages()[4][1], self.turn_input(SB.turn_two))
        self.assertEqual(turn_ends(job), [(1, 'ok')])
        said = [read_text(os.path.join(job, 'prompt.txt')), read_text(SB.turn_two)]
        self.assertEqual(result(job, 1).split(': ', 1)[1], ' | '.join(said))   # one turn, both
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-2')))

    def test_one_turn_start_is_in_flight_at_a_time(self):
        job, _ = SB.start('codex-one-ask', CODEX, '--cwd', SB.work, STUB_ASK_DELAY='1')
        SB.send(job)
        self.assertTrue(file_exists(job, 'turn-2', 'envelope.json'))
        SB.stop(job)
        self.assertEqual(SB.stub_log(job).events('overlap'), [])
        stream = read_text(os.path.join(job, 'stream.jsonl'))
        self.assertEqual(stream.count('"method": "turn/start"'), 2)
        self.assertEqual([envelope(job, n)['status'] for n in (1, 2)], ['ok', 'ok'])

    def test_a_send_its_turn_outran_opens_the_next_turn(self):
        job, _ = SB.start('codex-stale', CODEX, '--cwd', SB.work, STUB_MODE='stale')
        sent = SB.send(job).json
        waited = SB.wait(job, '--timeout', '30').json
        SB.stop(job)
        self.assertEqual((sent['status'], sent['expect']), ('sent', 1))
        cli = agent(job)
        self.assertEqual((cli.methods()[3:], cli.ended), (['turn/start', 'turn/start'], ['eof']))
        self.assertEqual(cli.messages()[4][1]['input'],
                         [{'type': 'text', 'text': read_text(SB.turn_two)}])
        self.assertEqual(result(job, 1), 'turn 1: ' + read_text(os.path.join(job, 'prompt.txt')))
        self.assertEqual(result(job, 2), 'turn 2: ' + read_text(SB.turn_two))
        self.assertEqual((waited['turn'], waited['status']), (2, 'ok'))   # wait followed the input
        # the command turn 1 left running completes during turn 2: it and its usage are strays
        self.assertEqual([e['turn'] for e in events(job) if e.get('call') == 'exec-late-1'], [1])
        self.assertEqual(strays(job), ['item/completed commandExecution',
                                       'thread/tokenUsage/updated'])
        self.assertEqual(envelope(job, 2)['usage'], CODEX_USAGE)

    def assertInterrupted(self, job, status, rc):
        """The polite close of a turn: turn/interrupt naming it, then stdin EOF; nothing left."""
        cli = agent(job)
        self.assertEqual(cli.messages()[-1], ('turn/interrupt', {'threadId': CODEX_THREAD,
                                                                 'turnId': 'turn-1'}))
        self.assertEqual(cli.ended[:1], ['eof'])
        end = envelope(job, 1)
        self.assertEqual((end['status'], end['rc'], end['session']), (status, rc, CODEX_THREAD))
        self.assertGone(job, cli.pid, *cli.children)

    def test_a_stop_mid_turn_closes_it_as_a_deadline_does(self):
        job, _ = SB.start('codex-stop', CODEX, '--cwd', SB.work, STUB_MODE='park')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        SB.send(job)
        self.assertEqual(SB.stop(job).rc, 0)
        self.assertInterrupted(job, 'incomplete', 143)
        self.assertEqual(len(SB.stub_log(job).seats()), 1)
        self.assertEqual(envelope(job, 1)['usage'],   # what its first call reported, in uncached
                         {'in': 200, 'out': 100, 'cache_read': 800, 'reasoning': 40})
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-2')))

    def test_a_stop_halts_a_cli_that_exits_0_on_a_term(self):
        job, _ = SB.start('codex-exit-0', CODEX, '--cwd', SB.work, STUB_MODE='parkexit')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        SB.send(job)
        self.assertEqual(SB.stop(job).rc, 0)
        cli = agent(job)
        self.assertGone(job, cli.pid, *cli.children)
        self.assertEqual(len(SB.stub_log(job).seats()), 1)
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('incomplete', 143))

    def test_a_relaunch_after_a_killed_wrapper_closes_its_turn_first(self):
        job, _ = SB.start('codex-killed', CODEX, '--cwd', SB.work, HANDS_IDLE='60',
                          STUB_MODE='park')
        self.assertTrue(wait_until(lambda: agent(job).parked))
        SB.send(job)
        SB.send(job)
        for wrapper in harness.wrapper_pids(job):
            os.killpg(wrapper, signal.SIGKILL)
        cli = agent(job)
        for pid in [cli.pid] + cli.children:
            harness.kill(pid)
        time.sleep(0.3)
        relaunched = SB.send(job, STUB_MODE=None, HANDS_IDLE='60').json   # up for the next send
        SB.wait(job, '--timeout', '20')
        sent = SB.send(job).json
        waited = SB.wait(job, '--timeout', '20')
        SB.stop(job)
        self.assertEqual((relaunched['status'], relaunched['expect']), ('spawned', 2))
        self.assertEqual((sent['status'], sent['expect']), ('sent', 3))
        self.assertEqual(waited.rc, 0)
        self.assertEqual(envelope(job, 1)['status'], 'incomplete')
        self.assertEqual((envelope(job)['status'], envelope(job)['turn']), ('ok', 3))
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-4')))

    def test_a_deadline_closes_politely_and_leaves_a_resumable_checkpoint(self):
        job, _ = SB.one_shot('codex-deadline', CODEX, '--effort', 'high', '--timeout', '3',
                             '--cwd', SB.work, STUB_MODE='park')
        self.assertInterrupted(job, 'timeout', 142)
        relaunched = SB.send(job).json
        SB.wait(job, '--timeout', '30')
        SB.stop(job)
        second = SB.stub_log(job).seats()[1]
        self.assertEqual(second.methods(), ['initialize', 'initialized', 'thread/resume',
                                            'turn/start'])
        self.assertEqual(second.ended, ['eof'])
        self.assertEqual(second.messages()[2][1], {'threadId': CODEX_THREAD, 'cwd': SB.work,
                                                   'sandbox': 'danger-full-access',
                                                   'approvalPolicy': 'never',
                                                   'model': 'gpt-5.6-terra'})
        self.assertEqual(second.messages()[3][1], self.turn_input(SB.turn_two, 'high'))
        self.assertEqual((relaunched['status'], relaunched['expect']), ('spawned', 2))
        end = envelope(job, 2)
        self.assertEqual((end['status'], end['session'], end['turn']), ('ok', CODEX_THREAD, 2))
        for launch in SB.stub_log(job).seats():   # the user's hooks and memories, off every time
            root_options = launch.argv[:launch.argv.index('app-server')]
            for feature in ('hooks', 'memories'):
                pairs = [root_options[i:i + 2] for i in range(len(root_options))]
                self.assertIn(['--disable', feature], pairs)
        refused = SB.send(job, STUB_MODE='badresume')
        self.assertEqual(refused.rc, 1)   # a resume refused is a typed launch failure
        self.assertEqual((refused.json['status'], refused.json['reason']), ('error', 'handshake'))
        self.assertIn('no rollout found for thread id th_stub123', refused.json['reset'])
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-3')))   # never a fresh thread

    def assertCollaboratorIsNotTheSeat(self, job):
        found = events(job)
        self.assertEqual(turn_ends(job), [(1, 'ok')])
        self.assertEqual(result(job, 1), 'FINAL: the main answer')
        self.assertFalse([e for e in found if 'SUB' in (e.get('text') or '')
                          or e.get('call') == 'exec-sub'])
        lines = [(e['turn'], e['text']) for e in progress(job)
                 if e.get('text', '').startswith('collaborator')]
        self.assertEqual(lines, [(1, 'collaborator turn/started'),
                                 (1, 'collaborator item/completed reasoning'),
                                 (1, 'collaborator item/completed commandExecution'),
                                 (1, 'collaborator item/completed agentMessage'),
                                 (1, 'collaborator turn/completed')])
        # its tokens are the turn's: the main turn's two calls plus the collaborator's
        self.assertEqual(envelope(job, 1)['usage'],
                         {'in': 506, 'out': 53, 'cache_read': 4, 'reasoning': 1})

    def test_a_collaborators_thread_is_not_the_seats(self):
        job, _ = SB.one_shot('codex-collab', CODEX, '--cwd', SB.work, STUB_MODE='subagent')
        self.assertCollaboratorIsNotTheSeat(job)

    def test_a_main_answer_before_the_collaborator_ends_still_answers(self):
        job, _ = SB.one_shot('codex-collab-late', CODEX, '--cwd', SB.work, STUB_MODE='latesub')
        self.assertCollaboratorIsNotTheSeat(job)

    def test_a_turn_whose_completion_never_comes_runs_until_a_stop(self):
        job, _ = SB.start('codex-no-main', CODEX, '--cwd', SB.work, STUB_MODE='nomain')
        running = SB.wait(job, '--timeout', '3').json
        SB.stop(job)
        # no timer infers the end: the turn runs until something stops it
        self.assertEqual((running['status'], running['turn']), ('running', 1))
        end = envelope(job, 1)
        self.assertEqual((end['status'], end['rc'], end['usage']),
                         ('incomplete', 143, {'in': 506, 'out': 53, 'cache_read': 4,
                                              'reasoning': 1}))
        answers = [e for e in events(job) if e['kind'] == 'text' and e['turn'] == 1
                   and e['text'] == 'FINAL: the main answer']
        self.assertEqual(len(answers), 1)
        self.assertFalse(os.path.exists(os.path.join(job, 'turn-2')))

    def test_an_unauthorized_turn_is_auth_expired(self):
        job, _ = SB.one_shot('codex-unauthorized', CODEX, '--cwd', SB.work, STUB_MODE='unauthorized')
        self.assertEqual((envelope(job)['status'], envelope(job)['turn']), ('auth-expired', 1))

    def test_a_malformed_line_is_kept_raw_and_skipped(self):
        job, _ = SB.one_shot('codex-bad-line', CODEX, '--cwd', SB.work, STUB_MODE='badline')
        self.assertEqual((envelope(job)['status'], result(job)), ('ok', 'stub ok'))
        stream = harness.read_jsonl(os.path.join(job, 'stream.jsonl'))
        self.assertEqual(len([r for r in stream if r['dir'] == 'in' and isinstance(r['msg'], str)]),
                         1)

    def test_a_one_shot_codex_run_persists_its_thread(self):
        job, _ = SB.one_shot('codex-one-shot', CODEX)
        start = agent(job).messages()[2]
        self.assertEqual(start[0], 'thread/start')
        self.assertIs(start[1]['ephemeral'], False)   # what a resume needs
        self.assertEqual(envelope(job)['session'], CODEX_THREAD)

    def test_the_start_ack_states_the_output_contract(self):
        job, run = SB.start('codex-ack', CODEX, '--cwd', SB.path('tmp'))
        ack = run.json
        self.assertEqual((ack['status'], ack['expect'], ack['delivery']), ('spawned', 1, 'steer'))
        self.assertIn('turn-N', ack['note'])
        self.assertIn('never finished-empty', ack['note'])
        self.assertTrue(file_exists(job, 'envelope.json'))
        self.assertEqual((envelope(job)['status'], envelope(job)['turn']), ('ok', 1))
        self.assertEqual(result(job, 1), 'stub ok')
        SB.stop(job)

    def test_an_answer_followed_by_a_tool_is_voided(self):
        job, _ = SB.one_shot('codex-no-final', CODEX, STUB_MODE='nofinal')
        self.assertEqual((envelope(job)['status'], envelope(job)['reason']), ('error', 'empty'))
        self.assertFalse(os.path.exists(os.path.join(job, 'result.txt')))
        self.assertEqual(len([e for e in events(job)
                              if e['kind'] == 'tool_call' and e['call'] == 'exec-5']), 1)

    def test_driver_mail_to_a_busy_seat_joins_its_turn(self):
        job, _ = SB.start('codex-busy-mail', CODEX, '--cwd', SB.work, STUB_MODE='slow',
                          STUB_SLEEP='4')
        SB.hands('mail', job, 'while you work')
        self.assertTrue(file_exists(job, 'turn-1', 'envelope.json'))
        SB.stop(job)
        found = events(job)
        marker = first_index(found, lambda e: e['kind'] == 'progress' and e.get('inbox') == 1)
        end_1 = first_index(found, lambda e: e['kind'] == 'turn_end' and e['turn'] == 1)
        self.assertTrue(-1 < marker < end_1)
        self.assertEqual((envelope(job, 1)['status'], envelope(job, 1)['rc']), ('ok', 0))
        mail = 'MAIL from driver:\nwhile you work'
        self.assertTrue(result(job, 1).endswith(' | ' + mail))
        self.assertEqual(turn_ends(job), [(1, 'ok')])
        self.assertEqual(len(SB.stub_log(job).seats()), 1)
        steers = [p['input'] for m, p in agent(job).messages() if m == 'turn/start'][1:]
        self.assertEqual(steers, [[{'type': 'text', 'text': mail}]])

    def test_each_seat_runs_a_private_app_server(self):
        job, _ = SB.one_shot('codex-private', CODEX)
        for launch in SB.stub_log(job).seats():
            self.assertEqual(launch.argv[-1:], ['app-server'])   # on stdio, its own process
            self.assertIn('--no-daemon', launch.argv)
            self.assertFalse({'--remote', '--remote-auth-token-env', '--listen', 'daemon', 'proxy',
                              'remote-control'} & set(launch.argv))


if __name__ == '__main__':
    harness.main('test_seat_codex')
