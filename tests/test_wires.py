#!/usr/bin/env python3
"""Tests for wires.py with no seat around it: the ACP and JSON-RPC clients against the stub
agent (tests/stubs/grok) replaying recorded grok and opencode transcripts, and the translators,
usage normalizer and command builders on their own. Run: python3 tests/test_wires.py"""
import itertools
import json
import os
import select
import signal
import sys
import tempfile
import time
import unittest

import harness

import wires

AGENT = os.path.join(harness.STUBS, 'grok')
# The event kinds of each recorded turn, derived by hand from the raw update order.
GROK_TURNS = [
    'turn_start progress*3 thought*16 text*2 progress*2 usage turn_end',
    'turn_start progress*2 thought*39 text*2 progress*2 usage turn_end',
    'turn_start progress*3 thought*42 text*15 progress*3 tool_call progress*5 tool_result usage '
    'turn_end',
    'turn_start progress*2 thought*42 text progress*2 usage turn_end',
]
OPENCODE_TURNS = [
    'turn_start text progress usage turn_end',
    'turn_start text progress usage turn_end',
    'turn_start thought*24 tool_call progress*2 tool_result progress usage turn_end',
    'turn_start thought*8 text progress usage turn_end',
]


def update(**fields):
    return {'jsonrpc': '2.0', 'method': 'session/update',
            'params': {'sessionId': 's1', 'update': fields}}


def chunk(text):
    return {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}


def answer(request_id, **fields):
    return {'jsonrpc': '2.0', 'id': request_id, **fields}


HANDSHAKE = [
    ('out', {'id': 1, 'method': 'initialize'}),
    ('in', answer(1, result={'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}})),
    ('out', {'id': 2, 'method': 'session/new'}),
    ('in', answer(2, result={'sessionId': 's1'})),
]
# A malformed line, the agent's own requests, file events, then death mid-turn.
SYNTHETIC = HANDSHAKE + [
    ('out', {'id': 3, 'method': 'session/prompt'}),
    ('in', 'this line is not json {'),
    ('in', answer('p1', method='session/request_permission', params={
        'sessionId': 's1', 'toolCall': {'toolCallId': 'c1'}, 'options': [
            {'optionId': 'always', 'kind': 'allow_always'},
            {'optionId': 'once', 'kind': 'allow_once'},
            {'optionId': 'no', 'kind': 'reject_once'}]})),
    ('out', {'id': 'p1'}),
    ('in', answer('f1', method='fs/read_text_file',
                  params={'sessionId': 's1', 'path': '/etc/hosts'})),
    ('out', {'id': 'f1'}),
    ('in', update(sessionUpdate='tool_call', toolCallId='c1', title='write', kind='edit',
                  status='pending', locations=[{'path': '/w/a.txt'}])),
    ('in', update(sessionUpdate='tool_call_update', toolCallId='c1', status='completed',
                  content=[{'type': 'diff', 'path': '/w/a.txt', 'oldText': None,
                            'newText': 'x'}])),
    ('in', update(**chunk('Done'))),
    ('in', answer(3, result={'stopReason': 'end_turn'})),
    ('out', {'id': 5, 'method': 'session/prompt'}),
    ('in', update(sessionUpdate='tool_call', toolCallId='c2', title='bash', kind='execute',
                  status='in_progress')),
]


def efforts(value):
    return [{'id': 'effort', 'category': 'thought_level', 'currentValue': value,
             'options': [{'value': x} for x in ('default', 'low', 'high')]}]


# opencode 2.0.22: the set's reply carries the OLD value; a config update ~20 ms later the new one
LATE_UPDATE = [
    ('out', {'id': 1, 'method': 'initialize'}),
    ('in', answer(1, result={'protocolVersion': 1, 'agentCapabilities': {}})),
    ('out', {'id': 2, 'method': 'session/new'}),
    ('in', answer(2, result={'sessionId': 's1', 'configOptions': efforts('default')})),
    ('out', {'id': 3, 'method': 'session/set_config_option'}),
    ('in', update(sessionUpdate='config_option_update', configOptions=efforts('default'))),
    ('in', answer(3, result={'configOptions': efforts('default')})),
    ('sleep', 0.3),
    ('in', update(sessionUpdate='config_option_update', configOptions=efforts('low'))),
    ('out', {'id': 4, 'method': 'session/set_config_option'}),   # a value that never takes
    ('in', answer(4, result={'configOptions': efforts('low')})),
]


def kinds(found):
    """The event kinds, run-length coded: 'turn_start thought*16 text*2 ...'."""
    runs = ((k, len(list(g))) for k, g in itertools.groupby(e['kind'] for e in found))
    return ' '.join(k if n == 1 else '%s*%d' % (k, n) for k, n in runs)


class AcpClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hands-wires-')
        self.addCleanup(self.tmp.cleanup)
        self.log = harness.StubLog(os.path.join(self.tmp.name, 'stub.jsonl'))

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write_rows(self, name, rows):
        return harness.save_transcript(self.path(name),
                                       [{'dir': d, 'msg': m} for d, m in rows])

    def agent(self, transcript, mode='eof'):
        """The stub agent replaying transcript, under wires.AcpAgent. This client never cancels
        (a stop ends a seat), so the recorded cancel is dropped: the turn ends on its own."""
        rows = harness.without_cancel(harness.read_jsonl(transcript))
        replay = harness.save_transcript(self.path('replay.jsonl'), rows)
        env = {'STUB_REPLAY': replay, 'STUB_MODE': mode, 'STUB_LOG': self.log.path}
        client = wires.AcpAgent([AGENT], self.tmp.name, env=env)
        self.addCleanup(client.close, 0.5)
        return client

    def received(self):
        return [json.loads(line) for line in self.log.launches()[0].lines]

    def turn(self, client, number, text, provider=None):
        """One prompt, run to its answer as a seat's loop would -> (the answer, its events)."""
        found = []
        answers = []

        def on_update(u):
            found.extend(wires.acp_events(u, number, None, provider))

        client.prompt(text, on_update, answers.append)
        deadline = time.time() + 30
        while not answers and time.time() < deadline:
            client.step(0.5)
        readable = client.fds()[0]
        while readable and select.select(readable, [], [], 0.2)[0]:
            client.on_ready(readable, [])   # what it sends right after the answer is this turn's
            readable = client.fds()[0]
        return answers[0], found + wires.acp_events(answers[0], number, None, provider)

    def test_a_recorded_grok_seat_round_trips(self):
        client = self.agent(os.path.join(harness.FIXTURES, 'grok-acp-sample.jsonl'))
        self.assertTrue(client.initialize()['loadSession'])
        session = '01a0d35c-6d25-77c1-b7bd-1a71c642327e'
        self.assertEqual(client.new_session(), session)
        self.assertEqual((client.current_value('model'), client.current_value('thought_level')),
                         ('grok-4.6', 'xhigh'))
        self.assertEqual(client.listed_values('thought_level'), ['xhigh', 'high', 'medium', 'low'])
        self.assertIsNone(client.listed_values('nope'))
        self.assertEqual(client.set_option('thought_level', 'low'), 'low')
        turns = [self.turn(client, n, text, 'grok')
                 for n, text in ((1, 'PONG'), (2, 'said?'), (3, 'sleep'), (4, 'AFTER'))]
        self.assertEqual([kinds(found) for _, found in turns], GROK_TURNS)
        ends = [(f[-1]['status'], f[-1]['text'], f[-1]['final'], f[-1]['session'])
                for _, f in turns]
        self.assertEqual(ends, [(status, text, 'boundary', session) for status, text in
                                (('ok', 'PONG'), ('ok', 'PONG'), ('cancelled', ''),
                                 ('ok', 'AFTER'))])
        cancelled = turns[2][1]
        closed = [(e['call'], e['status'], e.get('synthetic')) for e in cancelled
                  if e['kind'] == 'tool_result']
        self.assertEqual(closed, [('call-44db5404-56c6-4801-b4cc-522d9916cad1-0', 'cancelled',
                                   True)])
        used = [e for e in cancelled if e['kind'] == 'usage'][0]   # real, though it was cancelled
        self.assertEqual((used['usage']['in'], used['usage']['cache_read'], used['cost_usd']),
                         (17746, 2816, 0.01272144))
        self.assertEqual(client.close(), 0)

    def test_a_recorded_opencode_seat_round_trips(self):
        client = self.agent(os.path.join(harness.FIXTURES, 'opencode-acp-sample.jsonl'))
        self.assertTrue(client.initialize()['loadSession'])
        self.assertEqual((client.new_session(), client.current_value('model'),
                          client.current_value('thought_level')),
                         ('ses_f2ca45c73ffe55AqfFsvtJPq2O', 'opencode-go/deepseek-v4.1-flash',
                          'low'))
        turns = [self.turn(client, n, text, 'opencode')
                 for n, text in ((1, 'PONG'), (2, 'said?'), (3, 'sleep'), (4, 'AFTER'))]
        self.assertEqual([kinds(found) for _, found in turns], OPENCODE_TURNS)
        self.assertEqual([(f[-1]['status'], f[-1]['text'], f[-1]['final']) for _, f in turns],
                         [('ok', 'PONG', 'boundary'), ('ok', 'PONG', 'boundary'),
                          ('cancelled', '', 'boundary'), ('ok', 'AFTER', 'boundary')])
        uses = [[e for e in found if e['kind'] == 'usage'][0] for _, found in turns]
        self.assertEqual(uses[0]['usage'], {'in': 10740, 'out': 3, 'cache_read': 1792})
        self.assertEqual((uses[2].get('usage_unknown'), 'usage' in uses[2]), (True, False))
        cancelled = turns[2][1]
        closed = [(e['status'], e.get('synthetic')) for e in cancelled
                  if e['kind'] == 'tool_result']
        self.assertEqual(closed, [('ok', None)])   # opencode closes its own aborted tool
        late = [e['input'].get('command') for e in cancelled
                if e['kind'] == 'progress' and isinstance(e.get('input'), dict)]
        self.assertEqual(late[:1], ['sleep 30'])   # an input sent late rides the update
        self.assertEqual(client.close(), 0)

    def test_agent_requests_a_malformed_line_and_a_death_mid_turn(self):
        client = self.agent(self.write_rows('synthetic.jsonl', SYNTHETIC), 'die')
        client.initialize()
        client.new_session()
        _, found = self.turn(client, 1, 'write a.txt')   # the reader lived past the bad line
        self.assertEqual(kinds(found),
                         'turn_start progress tool_call file tool_result file text turn_end')
        self.assertEqual((found[-1]['status'], found[-1]['text'], found[-1]['final']),
                         ('ok', 'Done', 'after-last-tool'))
        self.assertEqual({e['path'] for e in found if e['kind'] == 'file'}, {'/w/a.txt'})
        sent = self.received()
        self.assertEqual(sent[3]['result'], {'outcome': {'outcome': 'selected',
                                                         'optionId': 'once'}})
        self.assertEqual(sent[4]['error']['code'], -32601)   # fs/* was never offered
        _, found = self.turn(client, 2, 'the agent dies mid-turn')
        self.assertEqual(kinds(found), 'turn_start tool_call tool_result error turn_end')
        self.assertEqual((found[-1]['status'], found[2]['status'], found[2]['call']),
                         ('incomplete', 'cancelled', 'c2'))
        self.assertEqual(client.close(), 1)

    def test_close_frees_a_prompt_whose_pipe_an_escaped_child_holds(self):
        rows = SYNTHETIC[:4] + SYNTHETIC[-2:]
        client = self.agent(self.write_rows('escape.jsonl', rows), 'escape')
        client.initialize()
        client.new_session()
        answers = []
        deadline = time.time() + 10
        client.prompt('x', None, answers.append)
        while client.proc.poll() is None and time.time() < deadline:
            client.step(0.2)
        self.assertIsNotNone(client.proc.poll())   # the agent is gone, its detached child is not
        for _ in range(5):
            client.step(0.2)
        self.assertEqual(answers, [], 'EOF arrived: the escaped child did not hold the pipe')
        client.close(grace=0.5)
        harness.kill(self.log.launches()[0].children[0])
        self.assertIn('incomplete', answers[0] if answers else {})

    def test_a_big_prompt_while_the_agent_floods_its_stdout(self):
        rows = HANDSHAKE + [('in', update(**chunk('y' * 1000)))] * 2000 + [   # 2 MB first
            ('out', {'id': 3, 'method': 'session/prompt'}),
            ('in', answer(3, result={'stopReason': 'end_turn'}))]
        client = self.agent(self.write_rows('flood.jsonl', rows))
        client.initialize()
        client.new_session()

        def stuck(*_):
            raise AssertionError('deadlocked: both sides blocked writing')

        signal.signal(signal.SIGALRM, stuck)
        signal.alarm(20)
        try:
            _, found = self.turn(client, 1, 'x' * (1 << 20))   # more than a pipe holds
        finally:
            signal.alarm(0)
        self.assertEqual(found[-1]['status'], 'ok')

    def test_close_reads_to_eof_before_it_gives_up_on_an_answer(self):
        # the agent exits at stdin EOF; a child of its own session answers 0.3 s later
        reply = json.dumps(answer(1, result={'ok': 1}))
        child = 'import time; time.sleep(0.3); print(%r, flush=True)' % reply
        agent = ('import subprocess, sys; sys.stdin.read(); '
                 'subprocess.Popen([sys.executable, "-c", %r], start_new_session=True)' % child)
        client = wires.Rpc([sys.executable, '-c', agent], self.tmp.name)
        answers = []
        client.request('ping', {}, answers.append)
        client.close(2)
        self.assertEqual(answers, [answer(1, result={'ok': 1})])

    def test_close_terms_the_process_group(self):
        rows = [('out', {'id': 1, 'method': 'initialize'}),
                ('in', answer(1, result={'agentCapabilities': {}}))]
        client = self.agent(self.write_rows('init.jsonl', rows), 'stubborn')
        client.initialize()
        child = self.log.launches()[0].children[0]
        started = time.time()
        client.close(grace=0.5)   # it ignores stdin EOF: only a TERM ends it
        self.assertIn('term', self.log.launches()[0].ended)
        self.assertTrue(harness.wait_until(lambda: not harness.alive(child), timeout=5),
                        'the agent\'s child outlived close()')
        self.assertLess(time.time() - started, 5)

    def test_set_option_waits_for_a_late_config_update(self):
        client = self.agent(self.write_rows('late.jsonl', LATE_UPDATE))
        client.initialize()
        client.new_session()
        self.assertEqual(client.set_option('thought_level', 'low'), 'low')   # the update took it
        started = time.time()
        self.assertRaises(wires.WireError, client.set_option, 'thought_level', 'high')
        self.assertLess(time.time() - started, 5)   # a value never confirmed: refused, in bounds


class Translation(unittest.TestCase):
    def events(self, u):
        return wires.acp_events(u, 7, at='T')

    def test_usage_is_normalized_per_provider(self):
        cases = [
            ({'inputTokens': 20429, 'outputTokens': 18, 'totalTokens': 20447,
              'cachedReadTokens': 128, 'cacheCreationTokens': 0, 'reasoningTokens': 16}, 'grok',
             {'in': 20301, 'out': 18, 'cache_read': 128, 'cache_write': 0, 'reasoning': 16}),
            ({'inputTokens': 197, 'outputTokens': 3, 'totalTokens': 12764, 'thoughtTokens': 20,
              'cachedReadTokens': 12544}, 'opencode',
             {'in': 197, 'out': 23, 'cache_read': 12544, 'reasoning': 20}),
            ({'input_tokens': 10, 'cache_read_input_tokens': 21806,
              'cache_creation_input_tokens': 8449, 'output_tokens': 43,
              'output_tokens_details': {'thinking_tokens': 36}}, 'claude',
             {'in': 10, 'out': 43, 'cache_read': 21806, 'cache_write': 8449, 'reasoning': 36}),
            ({'input_tokens': 10, 'cached_input_tokens': 4, 'output_tokens': 3,
              'reasoning_output_tokens': 1}, 'codex',
             {'in': 6, 'out': 3, 'cache_read': 4, 'reasoning': 1}),
        ]
        for raw, provider, normal in cases:   # grok counts the cache inside its input
            self.assertEqual(wires.normalize_usage(raw, provider), normal, provider)
        self.assertIsNone(wires.normalize_usage({'totalTokens': 3}, 'opencode'))   # unknown

    def test_a_stop_reason_types_the_turn(self):
        for stop, status in (('end_turn', 'ok'), ('refusal', 'error'), ('max_tokens', 'truncated'),
                             (None, 'error'), ('max_turn_requests', 'truncated'),
                             ('cancelled', 'cancelled'), ('bogus', 'error')):
            end = self.events({'_client': 'turn_end', 'stopReason': stop, 'answer': 'A'})[-1]
            self.assertEqual((end['kind'], end['status'], end['text']),
                             ('turn_end', status, 'A' if status == 'ok' else ''))
        failed = self.events({'_client': 'turn_end', 'error': {'code': -32000, 'message': 'auth'}})
        self.assertEqual([(e['kind'], e.get('status')) for e in failed],
                         [('error', None), ('turn_end', 'error')])

    def test_updates_that_are_no_events(self):
        shell = {'sessionUpdate': 'tool_call', 'toolCallId': 'x', 'kind': 'execute',
                 'locations': [{'path': '/w'}]}
        self.assertEqual([e['kind'] for e in self.events(shell)], ['tool_call'])   # a cwd: no file
        self.assertEqual(self.events({'sessionUpdate': 'available_commands_update'}), [])

    def test_the_answer_is_the_last_text_after_the_last_tool(self):
        turn = wires.Turn()
        turn.add_text(None, "I'll run it.")
        turn.add_tool()
        self.assertEqual(turn.answer(), '')   # narration before a tool is never the answer
        turn = wires.Turn()
        turn.add_text('m1', 'the answer')
        turn.add_text('m2', ' \n')
        self.assertEqual(turn.answer(), '')   # a blank last message fails closed

    def test_the_acp_commands(self):
        # opencode 2.0 has no --cwd, and 2.x takes no model from OPENCODE_CONFIG_CONTENT on ACP:
        # model and effort are session options, and the seat runs on the user's own store
        self.assertEqual(wires.opencode_command({'model': 'opencode-go/deepseek-v4.1-flash',
                                                 'effort': 'low'}), (['opencode', 'acp'], {}))
        self.assertRaises(ValueError, wires.opencode_command, {'model': 'deepseek'})
        self.assertEqual(wires.forget_command('opencode', 'ses_1', '/x/opencode'),
                         ['/x/opencode', 'session', 'delete', 'ses_1', '--standalone'])
        self.assertIsNone(wires.forget_command('grok', 's1'))   # grok keeps no claim
        self.assertIsNone(wires.forget_command('opencode', None))
        self.assertEqual(wires.grok_command({'model': 'grok-4.6'})[0][-3:],
                         ['-m', 'grok-4.6', 'stdio'])
        self.assertEqual(wires.grok_command({})[0][-2:], ['--always-approve', 'stdio'])

    def test_the_suite_carries_no_driver_inbox(self):
        names = {'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN'}
        self.assertFalse(names & set(os.environ))


if __name__ == '__main__':
    harness.main('test_wires')
