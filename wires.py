#!/usr/bin/env python3
"""How hands talks to each provider's CLI.

The wrapper (seat.py) calls launch() to start a seat's CLI and run its handshake. It then hands
the CLI each message and turns what the CLI says into events: StreamTranslator does that for
claude and codex, acp_events() for grok and opencode.

PROVIDERS is the provider table. For each provider it says how the wrapper talks to the CLI
(the family), what a message sent to a busy seat does (the delivery), and how to start the CLI
(the command).

Python 3 standard library only.
"""
import collections
import datetime
import json
import os
import select
import signal
import subprocess
import time
import traceback


# ---------------------------------------------------------------------------------------------
# The vocabulary every wire shares


def now():
    """The local time in ISO 8601, to the second."""
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def event(kind, turn, at=None, **fields):
    """One line of events.jsonl. Fields that are None are left out."""
    line = {'t': at or now(), 'kind': kind, 'turn': turn}
    line.update((key, value) for key, value in fields.items() if value is not None)
    return line


def number(value):
    """The value if it is an int or a float (a bool is neither), else None."""
    return value if type(value) in (int, float) else None


def clip(value, limit=300):
    """The value as text, cut to `limit` characters with a note of its full length."""
    if value is not None and not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if value is None or len(value) <= limit:
        return value
    return value[:limit] + '...[%d chars]' % len(value)


def small(value, limit=600):
    """A tool's input as it is when it is short, else clipped to text."""
    if value is None or len(json.dumps(value, ensure_ascii=False)) <= limit:
        return value
    return clip(value, limit)


def parse_json(line):
    """The JSON value of a line, or the line itself when it is not JSON."""
    try:
        return json.loads(line)
    except ValueError:
        return line


# Each usage field, and the names the CLIs give it. A dotted name is a nested field.
USAGE_FIELDS = (
    ('in', 'input_tokens', 'inputTokens'),
    ('out', 'output_tokens', 'outputTokens'),
    ('cache_read', 'cache_read_input_tokens', 'cached_input_tokens', 'cachedReadTokens',
     'cachedInputTokens'),
    ('cache_write', 'cache_creation_input_tokens', 'cache_write_input_tokens',
     'cachedWriteTokens', 'cacheCreationTokens', 'cacheWriteInputTokens'),
    ('reasoning', 'reasoning_output_tokens', 'thoughtTokens', 'reasoningTokens',
     'reasoningOutputTokens', 'output_tokens_details.thinking_tokens'),
)
CACHE_INSIDE_INPUT = ('codex', 'grok')      # their input count includes the cached tokens
REASONING_OUTSIDE_OUTPUT = ('opencode',)    # its output count leaves out the reasoning tokens


def normalize_usage(raw, provider):
    """A CLI's usage report -> {in, out, cache_read, cache_write, reasoning}, or None.

    'in' is the uncached input and 'out' everything billed as output, whatever the CLI calls
    them and however it counts them."""
    usage = {}
    for field, *names in USAGE_FIELDS if isinstance(raw, dict) else ():
        for name in names:
            value = raw
            for part in name.split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if number(value) is not None:
                usage[field] = value
                break
    if provider in CACHE_INSIDE_INPUT and 'in' in usage:
        usage['in'] -= usage.get('cache_read', 0)
    if provider in REASONING_OUTSIDE_OUTPUT and 'out' in usage:
        usage['out'] += usage.get('reasoning', 0)
    return usage or None


def add_usage(a, b):
    """Two raw usage reports summed field by field (either may lack a field)."""
    a = a or {}
    b = b or {}
    total = {}
    for field in set(a) | set(b):
        if type(a.get(field, 0)) in (int, float) and type(b.get(field, 0)) in (int, float):
            total[field] = a.get(field, 0) + b.get(field, 0)
    return total


class Message:
    """One model message of a turn, as its text arrives."""

    def __init__(self, message_id):
        self.id = message_id
        self.chunks = []
        self.ended = False
        self.last = 0       # the turn's sequence number at its latest chunk


class Turn:
    """A turn's model messages in order, and the rule that picks its answer.

    The answer is the last message that has text. It is void when a tool call came after it
    (the model was not done) or when its text is only whitespace; an earlier message never
    stands in for it."""

    def __init__(self):
        self.messages = []
        self.sequence = 0
        self.last_tool = -1     # the sequence number of the last tool event
        self.usage = None
        self.started = time.time()
        self.open_calls = {}    # ACP: the tool calls not yet ended
        self.bounded = False    # ACP: the agent marked where its messages end

    def add_text(self, message_id, text):
        self.sequence += 1
        last = self.messages[-1] if self.messages else None
        if last is None or last.ended or last.id != message_id:
            self.messages.append(Message(message_id))
        self.messages[-1].chunks.append(text)
        self.messages[-1].last = self.sequence

    def end_message(self):
        if self.messages:
            self.messages[-1].ended = True

    def add_tool(self):
        self.sequence += 1
        self.last_tool = self.sequence
        self.end_message()

    def answer(self):
        for message in reversed(self.messages):
            text = ''.join(message.chunks)
            if text:
                return text if message.last > self.last_tool and text.strip() else ''
        return ''


def closed_turn(turn, status, text='', usage=None, final='none', error_text='', session=None,
                started=None, extra=None):
    """A turn a translator closed, as seat.py's finish() takes it.

    extra holds fields for the turn's usage event (usage_unknown, cost_usd)."""
    return {'turn': turn, 'status': status, 'text': text or '', 'usage': usage, 'final': final,
            'error_text': error_text, 'session': session, 'started': started,
            'extra': extra or {}}


# ---------------------------------------------------------------------------------------------
# A CLI process, and JSON-RPC over it


class WireError(Exception):
    """An error answer to a call, a CLI that went away, or a call that got no answer."""


class Cli:
    """One CLI process, talked to over its stdin and stdout.

    It runs in a session of its own, and its owner's select loop drives it: nothing here blocks
    or starts a thread. Bytes written wait in `pending` until the CLI's stdin takes them. Each
    whole line the CLI prints goes to on_line(). log(line, direction) sees every line, with
    direction 'in' (from the CLI) or 'out' (to it)."""

    session_id = None   # the conversation, once a handshake names it

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None, on_message=None):
        self.cwd = cwd
        self.log = log
        self.on_message = on_message    # gets each line: a JSON object as a dict, else its text
        self.pending = b''
        self.partial = b''              # the start of a line not yet ended
        self.at_eof = False
        self.stderr = open(stderr_path, 'ab') if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=dict(os.environ, **(env or {})), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True)
        self.stdin_fd = self.proc.stdin.fileno()
        self.stdout_fd = self.proc.stdout.fileno()
        os.set_blocking(self.stdin_fd, False)
        os.set_blocking(self.stdout_fd, False)

    def _log(self, line, direction):
        try:
            if self.log:
                self.log(line, direction)
        except Exception:   # a failing log never stops the wire
            pass

    def fds(self):
        """What a select should wait on for this CLI -> (readable, writable)."""
        readable = [] if self.at_eof else [self.stdout_fd]
        writable = [self.stdin_fd] if self.pending and self.stdin_fd is not None else []
        return readable, writable

    def on_ready(self, readable, writable):
        """Give the CLI's stdin what it takes; read its stdout and handle each whole line."""
        if self.pending and self.stdin_fd is not None and self.stdin_fd in writable:
            try:
                self.pending = self.pending[os.write(self.stdin_fd, self.pending):]
            except BlockingIOError:
                pass
            except OSError:   # the CLI closed its stdin: what it never read is lost with it
                self.pending = b''
        if self.at_eof or self.stdout_fd not in readable:
            return
        try:
            chunk = os.read(self.stdout_fd, 65536)
        except BlockingIOError:
            return
        if chunk:
            *lines, self.partial = (self.partial + chunk).split(b'\n')
        else:   # end of file: a last line with no newline still counts
            lines = [self.partial]
            self.partial = b''
        for raw in lines:
            self._read_line(raw)
        if not chunk:
            self._on_eof()

    def step(self, timeout):
        """Wait up to `timeout` seconds for this CLI alone, then on_ready()."""
        timeout = max(0, timeout)   # the clock may pass the caller's end before this call
        readable, writable = self.fds()
        if not readable and not writable:
            time.sleep(min(timeout, 0.05))
            return
        readable, writable, _ = select.select(readable, writable, [], timeout)
        self.on_ready(readable, writable)

    def _read_line(self, raw):
        line = raw.decode('utf-8', 'replace').rstrip('\r')
        if not line.strip():
            return
        self._log(line, 'in')
        try:
            self.on_line(line)
        except Exception:   # a failing callback never stops the wire; its traceback is logged
            traceback.print_exc()

    def on_line(self, line):
        message = parse_json(line)
        if self.on_message is not None:
            self.on_message(message if isinstance(message, dict) else line)

    def _on_eof(self):
        self.at_eof = True

    def write(self, data):
        """Queue bytes for the CLI's stdin, and give it what it takes now."""
        if self.stdin_fd is None:
            raise WireError('agent stdin closed')
        self.pending += data
        self.on_ready([], [self.stdin_fd])

    def _wait_for_exit(self, seconds):
        end = time.time() + seconds
        while self.proc.poll() is None and time.time() < end:
            self.step(min(0.1, end - time.time()))

    def close(self, grace=4.0):
        """End the CLI and return its exit code.

        Its stdin closes first: every CLI here exits on that. After `grace` seconds its process
        group gets TERM, then KILL. What it prints meanwhile is still read and handled, and a
        request it never answered gets None."""
        if self.stdin_fd is not None:
            self.pending = b''
            self.stdin_fd = None
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        self._wait_for_exit(grace)
        # The process is the CLI's reaper (seat.py reap). It answers TERM by ending the CLI's
        # own group (KILL within 2 s), then exits. It gets that long: a reaper killed first
        # would leave behind a CLI that ignores TERM.
        for sig, wait in ((signal.SIGTERM, grace + 3), (signal.SIGKILL, grace)):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break   # the group is gone
            self._wait_for_exit(wait)
        end = time.time() + grace   # the last lines, up to end of file
        while not self.at_eof and time.time() < end:
            self.step(min(0.1, end - time.time()))
        if not self.at_eof:   # a child outside the group still holds the pipe open
            self._on_eof()
        self.proc.stdout.close()
        if self.stderr is not subprocess.DEVNULL:
            self.stderr.close()
        return self.proc.poll()


class Rpc(Cli):
    """A Cli that speaks JSON-RPC 2.0, one message per line (codex's app-server, ACP agents).

    An answer goes to the callback its request named, a notification to on_message, and a
    request from the agent to answer_request()."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None, on_message=None):
        self.last_id = 0
        self.waiting = {}   # request id -> the callback for its answer
        Cli.__init__(self, argv, cwd, env, log, stderr_path, on_message)

    def on_line(self, line):
        message = parse_json(line)
        if isinstance(message, dict):   # anything else is logged and skipped
            self._dispatch(message)

    def _on_eof(self):
        """Nothing more will be answered: every waiting request gets None."""
        Cli._on_eof(self)
        callbacks = list(self.waiting.values())
        self.waiting = {}
        for callback in callbacks:
            try:
                callback(None)
            except Exception:
                pass

    def _dispatch(self, message):
        method = message.get('method')
        request_id = message.get('id')
        if method is None:   # the answer to one of our requests
            callback = self.waiting.pop(request_id, None)
            if callback:
                callback(message)
            return
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        if request_id is not None:
            self.answer_request(request_id, method, params)
        elif self.on_message is not None:
            self.on_message({'method': method, 'params': params})

    def answer_request(self, request_id, method, params):
        """A request from the agent. This client offers no methods."""
        error = {'code': -32601, 'message': 'Method not found: ' + str(method)}
        self.send({'jsonrpc': '2.0', 'id': request_id, 'error': error})

    def send(self, message):
        """Queue one JSON-RPC message for the agent."""
        if self.stdin_fd is None:
            raise WireError('agent stdin closed')
        line = json.dumps(message, ensure_ascii=False)
        self._log(line, 'out')
        self.write(line.encode('utf-8') + b'\n')

    def notify(self, method, params=None):
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        self.send(message)

    def request(self, method, params, on_answer):
        """Send a request. on_answer gets its answer, or None if the agent goes first."""
        if self.at_eof or self.stdin_fd is None:
            on_answer(None)
            return None
        self.last_id += 1
        self.waiting[self.last_id] = on_answer
        self.send({'jsonrpc': '2.0', 'id': self.last_id, 'method': method, 'params': params})
        return self.last_id

    def wait_for_answer(self, method, params, timeout):
        """Send a request and read the agent until it answers -> the answer (None: it went)."""
        answers = []
        request_id = self.request(method, params, answers.append)
        end = time.time() + timeout
        while not answers:
            if time.time() >= end:
                self.waiting.pop(request_id, None)
                raise WireError('no answer to %s within %ss' % (method, timeout))
            self.step(min(0.5, end - time.time()))
        return answers[0]

    def call(self, method, params, timeout=45):
        """A request that must succeed -> its result; WireError otherwise."""
        answer = self.wait_for_answer(method, params, timeout)
        if answer is None:
            raise WireError('agent exited before answering %s (rc=%s)'
                            % (method, self.proc.poll()))
        if 'error' in answer:
            error = answer['error']
            if not isinstance(error, dict):
                error = {'message': str(error)}
            raise WireError('%s failed: %s %s' % (method, error.get('code'), error.get('message')))
        result = answer.get('result')
        return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------------------------
# ACP agents: grok and opencode

ACP_STOP_STATUS = {   # stopReason -> the turn's status; anything else is an error
    'end_turn': 'ok',
    'cancelled': 'cancelled',
    'max_tokens': 'truncated',
    'max_turn_requests': 'truncated',
    'refusal': 'error',
}
ACP_TOOL_STATUS = {'completed': 'ok', 'failed': 'error', 'cancelled': 'cancelled'}
ACP_FILE_KINDS = ('edit', 'delete', 'move')   # a tool location is a touched file for these kinds
ACP_SESSION_STATE = ('available_commands_update', 'config_option_update', 'session_info_update',
                     'current_mode_update', 'user_message_chunk')   # not turn events


class AcpAgent(Rpc):
    """An Agent Client Protocol (v1) agent: `grok agent stdio` or `opencode acp`.

    It never calls `authenticate`: each seat runs on its own CLI's login."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None):
        self.session_id = None
        self.info = {}            # what initialize answered
        self.options = []         # the session's config options: model, effort, mode
        self.turn = None          # the running prompt's Turn
        self.on_update = None     # the running prompt's update callback
        Rpc.__init__(self, argv, cwd, env, log, stderr_path, self._on_notification)

    def _on_notification(self, message):
        """A session update (grok also sends _x.ai/session_notification): track it, pass it on."""
        update = message['params'].get('update')
        if not isinstance(update, dict) or 'sessionUpdate' not in update:
            return   # grok's queue, model and session chatter: logged only
        kind = update['sessionUpdate']
        if kind == 'config_option_update' and isinstance(update.get('configOptions'), list):
            self.options = update['configOptions']
        turn = self.turn
        on_update = self.on_update
        if turn is not None:
            if kind == 'response_completed':
                turn.bounded = True
            elif kind in ('agent_message_chunk', 'agent_thought_chunk') and update.get('messageId'):
                turn.bounded = True
            if kind == 'agent_message_chunk':
                turn.add_text(update.get('messageId'), update_text(update))
            elif kind == 'response_completed':
                turn.end_message()
            elif kind in ('tool_call', 'tool_call_update'):
                turn.add_tool()
                call_id = update.get('toolCallId')
                if update.get('status') in ACP_TOOL_STATUS:
                    turn.open_calls.pop(call_id, None)
                elif call_id:
                    turn.open_calls.setdefault(call_id, True)
        if on_update is not None:
            on_update(update)

    def answer_request(self, request_id, method, params):
        """A permission request gets allow_once, else the first allow option: an "always"
        answer could save a rule in the CLI's config. (fs and terminal were not offered.)"""
        if method != 'session/request_permission':
            return Rpc.answer_request(self, request_id, method, params)
        choices = [c for c in params.get('options') or [] if isinstance(c, dict)]
        pick = next((c for c in choices if c.get('kind') == 'allow_once'), None)
        if pick is None:
            pick = next((c for c in choices if str(c.get('kind', '')).startswith('allow')), None)
        if pick:
            outcome = {'outcome': 'selected', 'optionId': pick.get('optionId')}
        else:
            outcome = {'outcome': 'cancelled'}
        self.send({'jsonrpc': '2.0', 'id': request_id, 'result': {'outcome': outcome}})
        call = params.get('toolCall') if isinstance(params.get('toolCall'), dict) else {}
        if self.on_update is not None:
            self.on_update({'_client': 'permission', 'toolCallId': call.get('toolCallId'),
                            'outcome': outcome})

    def call(self, method, params, timeout=45):
        result = Rpc.call(self, method, params, timeout)
        if isinstance(result.get('configOptions'), list):
            self.options = result['configOptions']
        return result

    def initialize(self):
        """The ACP handshake (protocol version 1, no fs, no terminal) -> its capabilities."""
        capabilities = {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}
        self.info = self.call('initialize', {'protocolVersion': 1,
                                             'clientCapabilities': capabilities})
        return self.info.get('agentCapabilities') or {}

    def new_session(self):
        result = self.call('session/new', {'cwd': self.cwd, 'mcpServers': []})
        if not result.get('sessionId'):
            raise WireError('session/new answered without a sessionId')
        self.session_id = result['sessionId']
        return self.session_id

    def resume(self, session_id):
        """Reattach a stored session in this process. session/resume replays no history."""
        capabilities = self.info.get('agentCapabilities') or {}
        if 'resume' not in (capabilities.get('sessionCapabilities') or {}):
            raise WireError('agent does not advertise sessionCapabilities.resume')
        self.call('session/resume', {'sessionId': session_id, 'cwd': self.cwd, 'mcpServers': []})
        self.session_id = session_id

    def _option(self, key):
        """The config option whose id or category is `key` (model, thought_level, mode)."""
        for option in self.options:
            if isinstance(option, dict) and key in (option.get('id'), option.get('category')):
                return option
        return None

    def current_value(self, key):
        return (self._option(key) or {}).get('currentValue')

    def listed_values(self, key):
        """The values option `key` lists, or None when the agent has no such option."""
        option = self._option(key)
        if option is None:
            return None
        return [c.get('value') for c in option.get('options') or [] if isinstance(c, dict)]

    def set_option(self, key, value):
        """Set the option whose id or category is `key` -> its new value.

        None when there is no such option. WireError when the agent refuses, or when the value
        has not taken within 2 s: opencode may answer with the old value and confirm the new
        one in a config update just after."""
        option = self._option(key)
        if option is None:
            return None
        result = self.call('session/set_config_option',
                           {'sessionId': self.session_id, 'configId': option['id'], 'value': value})
        if not isinstance(result.get('configOptions'), list):
            option['currentValue'] = value   # an answer with no state: take it at its word
        late = time.time() + 2
        while self.current_value(option['id']) != value and time.time() < late and not self.at_eof:
            self.step(min(0.05, late - time.time()))
        if self.current_value(option['id']) != value:
            raise WireError('%s=%s did not take (currentValue %r)'
                            % (option['id'], value, self.current_value(option['id'])))
        return value

    def busy(self):
        return self.turn is not None

    def prompt(self, text, on_update, on_done):
        """Start a turn: session/prompt with one text block. It returns at once, and the
        owner's loop runs the turn.

        on_update gets a turn_start record, each session update of the turn, then a cancelled
        tool_call_update for each tool call left open (grok sends none after a cancel).
        on_done gets the prompt's result plus _client, sessionId, answer and final, and error
        (an error answer) or incomplete (the agent went away first)."""
        if not self.session_id:
            raise WireError('no session: call new_session() or resume() first')
        if self.turn is not None:
            raise WireError('a prompt is already running on this seat (ACP v1 has no queue)')
        turn = Turn()
        on_update = on_update or (lambda update: None)
        on_update({'_client': 'turn_start', 'sessionId': self.session_id})
        self.turn = turn
        self.on_update = on_update

        def answered(message):   # the turn is over: later updates belong to no turn
            self.turn = None
            self.on_update = None
            for call_id in list(turn.open_calls):
                on_update({'_client': 'tool_closed', 'sessionUpdate': 'tool_call_update',
                           'toolCallId': call_id, 'status': 'cancelled'})
            result = {}
            if message and isinstance(message.get('result'), dict):
                result = dict(message['result'])
            if message is None:
                result['incomplete'] = ('agent exited before answering session/prompt (rc=%s)'
                                        % self.proc.poll())
            elif 'error' in message:
                result['error'] = message['error']
            result.update(_client='turn_end', sessionId=self.session_id, answer=turn.answer(),
                          final='boundary' if turn.bounded else 'after-last-tool')
            on_done(result)

        prompt = [{'type': 'text', 'text': text}]
        self.request('session/prompt', {'sessionId': self.session_id, 'prompt': prompt}, answered)


def update_text(update):
    content = update.get('content')
    if isinstance(content, dict) and content.get('type') == 'text':
        return content.get('text') or ''
    return ''


def grok_tool(update):
    """grok names a tool and its kind in _meta["x.ai/tool"]."""
    meta = update.get('_meta') if isinstance(update.get('_meta'), dict) else {}
    tool = meta.get('x.ai/tool')
    return tool if isinstance(tool, dict) else {}


def tool_name(update):
    return update.get('name') or update.get('title') or grok_tool(update).get('name')


def touched_paths(update):
    """The files a tool touched: its diffs' paths, and its locations when its kind edits,
    deletes or moves (a shell tool's location is just its cwd)."""
    content = update.get('content') if isinstance(update.get('content'), list) else []
    paths = [c.get('path') for c in content if isinstance(c, dict) and c.get('type') == 'diff']
    if (update.get('kind') or grok_tool(update).get('kind')) in ACP_FILE_KINDS:
        paths += [x.get('path') for x in update.get('locations') or [] if isinstance(x, dict)]
    return list(dict.fromkeys(p for p in paths if p))


def acp_events(update, turn, at=None, provider=None):
    """One thing an AcpAgent passed on -> its events.

    That is a session update, a record the client made (turn_start, permission, tool_closed),
    or a prompt's result (turn_end). Session state gives no events."""
    at = at or now()

    def make(kind, **fields):
        return event(kind, turn, at, **fields)

    record = update.get('_client')
    kind = update.get('sessionUpdate')
    if record == 'turn_start':
        return [make('turn_start', session=update.get('sessionId'))]
    if record == 'turn_end':
        return acp_turn_end(update, make, provider)
    if record == 'permission':
        choice = (update.get('outcome') or {}).get('optionId') or 'cancelled'
        return [make('progress', call=update.get('toolCallId'), text='permission %s' % choice)]
    if kind in ('agent_message_chunk', 'agent_thought_chunk'):
        text_kind = 'text' if kind == 'agent_message_chunk' else 'thought'
        return [make(text_kind, text=update_text(update))]
    if kind in ('tool_call', 'tool_call_update'):
        call = update.get('toolCallId')
        status = update.get('status')
        name = tool_name(update)
        events = []
        if kind == 'tool_call':
            events.append(make('tool_call', call=call, tool=name,
                               input=small(update.get('rawInput'))))
        if status in ACP_TOOL_STATUS:
            output = clip(update.get('rawOutput') or update.get('content'))
            events.append(make('tool_result', call=call, status=ACP_TOOL_STATUS[status],
                               output=output, synthetic=True if record else None))
        elif kind == 'tool_call_update':
            events.append(make('progress', call=call, text='tool %s' % (status or 'update'),
                               input=small(update.get('rawInput') or None)))
        for path in touched_paths(update):
            events.append(make('file', call=call, path=path, tool=name))
        return events
    if kind == 'plan':
        entries = [e for e in update.get('entries') or [] if isinstance(e, dict)]
        steps = '; '.join('[%s] %s' % (e.get('status'), e.get('content')) for e in entries)
        return [make('progress', text=clip('plan: ' + steps))]
    if kind == 'usage_update':   # how full the context is, and the session's cost so far
        return [make('progress', text='context', context_used=update.get('used'),
                     context_size=update.get('size'), cost=update.get('cost'))]
    if kind in ACP_SESSION_STATE:
        return []
    return [make('progress', text=str(kind))]   # grok's own kinds: hooks, response ends


def acp_turn_end(result, make, provider):
    """A prompt's result -> the turn's usage event, an error event if it failed, and turn_end."""
    stop = result.get('stopReason')
    if 'incomplete' in result:
        status = 'incomplete'
    elif 'error' in result:
        status = 'error'
    else:
        status = ACP_STOP_STATUS.get(stop, 'error')
    meta = result.get('_meta') if isinstance(result.get('_meta'), dict) else {}
    raw = result.get('usage') if isinstance(result.get('usage'), dict) else meta.get('usage')
    events = []
    if isinstance(raw, dict):
        usage = normalize_usage(raw, provider) or {}
        ticks = number(raw.get('costUsdTicks'))   # grok: one tick is 1e-10 USD
        if any(usage.values()):
            cost = ticks / 1e10 if ticks is not None else None
            events.append(make('usage', usage=usage, cost_usd=cost))
        else:   # opencode reports 0 for a cancelled turn: unknown, not free
            events.append(make('usage', usage_unknown=True))
    error = result.get('error') if isinstance(result.get('error'), dict) else {}
    if status in ('error', 'incomplete'):
        why = result.get('incomplete')
        if not why and error:
            why = '%s %s' % (error.get('code'), error.get('message'))
        events.append(make('error', text=clip(why or 'stopReason %r' % stop)))
    answer = result.get('answer', '') if status == 'ok' else ''
    events.append(make('turn_end', status=status, text=answer, session=result.get('sessionId'),
                       final=result.get('final'), stop=stop))
    return events


# ---------------------------------------------------------------------------------------------
# The provider table
#
# No seat runs the user's hooks: hooks are the user's own code, written for their own sessions.
# Each command turns them off with its CLI's own switch: claude disableAllHooks, codex
# --disable hooks, grok its claude and cursor hook scanners. opencode runs no claude hooks, and
# its plugins have no switch short of replacing its whole config dir.


def claude_command(handle):
    """claude: one process for the whole seat -> (argv, env).

    Every seat has full access (bypassPermissions) and runs lean: no MCP servers from the host,
    the per-folder prompt sections moved into the first message (so the system prompt is the
    same in every folder), no auto-memory, and no background tasks (a turn left waiting on one
    would park). Its one settings source refuses messages from other sessions. Every relaunch
    sends the same flags, so a resume reuses the cached prompt."""
    settings = {'crossSessionInbound': 'refuse', 'disableAllHooks': True}
    argv = [handle.get('cli') or 'claude', '-p',
            '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
            '--model', handle.get('model') or 'sonnet',
            '--settings', json.dumps(settings, separators=(',', ':')),
            '--strict-mcp-config', '--exclude-dynamic-system-prompt-sections']
    if handle.get('effort'):
        argv += ['--effort', handle['effort']]
    argv += ['--permission-mode', 'bypassPermissions']
    if handle.get('session'):
        argv += ['--resume', handle['session']]
    env = {'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}
    return argv, env


def codex_command(handle):
    """codex: one app-server process per launch, over stdio -> (argv, env).

    --no-daemon keeps the seat off the user's shared app-server. Memories and hooks are off.
    Web search is live (the app-server has no --search flag). The cwd, sandbox, model and
    approvals go with the thread (codex_handshake), the effort with each turn/start."""
    argv = [handle.get('cli') or 'codex', '--no-daemon', '--disable', 'memories',
            '--disable', 'hooks', '-c', 'web_search="live"', 'app-server']
    return argv, {}


def grok_command(handle):
    """grok: an ACP agent -> (argv, env).

    -m pins the model. The effort is a session option (acp_handshake sets it): grok's own
    --reasoning-effort never reaches an ACP session. The autoupdater is off."""
    argv = [handle.get('cli') or 'grok', 'agent', '--no-leader', '--always-approve']
    if handle.get('model'):
        argv += ['-m', handle['model']]
    env = {'GROK_DISABLE_AUTOUPDATER': '1', 'GROK_CLAUDE_HOOKS_ENABLED': '0',
           'GROK_CURSOR_HOOKS_ENABLED': '0'}
    return argv + ['stdio'], env


def opencode_command(handle):
    """opencode: an ACP agent -> (argv, env).

    `opencode acp` takes no model flag, so the model is a session option, like the effort
    (acp_handshake sets both). The agent runs in the seat's cwd, and on the user's own opencode
    login and store."""
    if not (handle.get('model') or '').partition('/')[2]:
        raise ValueError('model-required', 'opencode seats name provider/model; the CLI '
                         "default is the driver's own pool")
    return [handle.get('cli') or 'opencode', 'acp'], {}


# family:   how the wrapper talks to the CLI: 'stdin' (claude's stream-json on its stdin and
#           stdout), 'rpc' (codex's app-server) or 'acp' (Agent Client Protocol)
# delivery: what a message sent to a busy seat does: 'steer' joins the running turn, 'queue'
#           waits for it to end (ACP v1 takes no input during a turn)
# command:  handle -> (argv, environment overrides)
Provider = collections.namedtuple('Provider', 'family delivery command')
PROVIDERS = {
    'claude': Provider('stdin', 'steer', claude_command),
    'codex': Provider('rpc', 'steer', codex_command),
    'grok': Provider('acp', 'queue', grok_command),
    'opencode': Provider('acp', 'queue', opencode_command),
}


def forget_command(provider, session, cli=None):
    """The command that deletes the stored session of a turn whose agent went away mid-turn,
    or None when the provider keeps nothing to resume.

    opencode keeps such a turn's claim, and the user's background opencode service would resume
    it at its next start. --standalone runs the delete on a private server."""
    if provider == 'opencode' and session:
        return [cli or 'opencode', 'session', 'delete', session, '--standalone']
    return None


def codex_handshake(handle, rpc):
    """initialize, then thread/start, or thread/resume for a seat that has a thread -> its id.

    Every seat has full access and never asks for approval (no one would answer). The thread
    is never ephemeral, so it can be resumed. With no model named, codex's own config picks."""
    rpc.call('initialize', {'clientInfo': {'name': 'hands', 'version': '2'}})
    rpc.notify('initialized')
    params = {'cwd': handle['cwd'], 'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}
    if handle.get('model'):
        params['model'] = handle['model']
    if handle.get('session'):
        result = rpc.call('thread/resume', dict(params, threadId=handle['session']))
    else:
        result = rpc.call('thread/start', dict(params, ephemeral=False))
    thread_id = (result.get('thread') or {}).get('id')
    if not thread_id:
        raise WireError('the app-server answered the thread without its id')
    return thread_id


def acp_handshake(agent, handle):
    """initialize, the session, then the model and the effort -> None, or (status, reason, why)
    when the seat is refused.

    A recorded session is resumed, never replayed. The model is set when the agent runs another
    one it lists; a seat never runs a substitute. The effort must be one the model lists."""
    model = handle.get('model')
    effort = handle.get('effort')
    agent.initialize()
    if handle.get('session'):
        agent.resume(handle['session'])
    else:
        agent.new_session()
    running = agent.current_value('model')
    if model and running != model and model in (agent.listed_values('model') or []):
        running = agent.set_option('model', model)
    efforts = agent.listed_values('thought_level') or []   # after the model: each lists its own
    if model and running != model:
        listed = ', '.join(agent.listed_values('model') or []) or 'no models'
        return ('refused', 'model-mismatch', 'the agent runs %s and lists %s; the seat names %s: '
                'never substituted' % (running, listed, model))
    if effort not in (efforts or [None]):
        return ('refused', 'effort-unsupported', '%s lists effort %s; the seat names %s'
                % (running, '|'.join(efforts) or 'none', effort or 'none'))
    if effort:
        agent.set_option('thought_level', effort)
    return None


def launch(handle, wrap, log=None, stderr_path=None, on_message=None):
    """Start a seat's CLI and run its handshake -> (cli, None), or (cli or None, (status,
    reason, why)) when the launch fails or is refused before any turn.

    wrap(argv) puts the CLI under the caller's reaper. on_message gets what a claude or codex
    CLI says; an ACP agent's updates go to each prompt's own callback."""
    family, _, command = PROVIDERS[handle['provider']]
    try:
        argv, env = command(handle)
    except ValueError as refusal:
        return None, ('refused',) + refusal.args
    if family == 'stdin':
        return Cli(wrap(argv), handle['cwd'], env, log, stderr_path, on_message), None
    if family == 'rpc':
        cli = Rpc(wrap(argv), handle['cwd'], env, log, stderr_path, on_message)
        try:
            cli.session_id = codex_handshake(handle, cli)
        except WireError as error:
            return cli, ('error', 'handshake', str(error))
        return cli, None
    agent = AcpAgent(wrap(argv), handle['cwd'], env, log, stderr_path)
    try:
        return agent, acp_handshake(agent, handle)
    except WireError as error:
        return agent, ('error', 'handshake', str(error))


# ---------------------------------------------------------------------------------------------
# Messages in, and the claude and codex translators


def input_record(provider, text):
    """A message as the line the wrapper reads from in.fifo.

    For claude that is claude's own stdin line, which the wrapper passes straight through."""
    if PROVIDERS[provider].family == 'stdin':
        content = [{'type': 'text', 'text': text}]
        return {'type': 'user', 'message': {'role': 'user', 'content': content}}
    return {'message': {'content': text}}


def record_text(record):
    """The text of an in.fifo record (bytes), for a CLI that is not claude."""
    message = parse_json(record.decode('utf-8', 'replace'))
    if isinstance(message, dict) and isinstance(message.get('message'), dict):
        return message['message']['content']
    return str(message)


CODEX_TOOL_ITEMS = {   # codex item type -> (tool name, the item field that holds its input)
    'commandExecution': ('shell', 'command'),
    'fileChange': ('apply_patch', 'changes'),
    'webSearch': ('web_search', 'query'),
    'mcpToolCall': (None, 'arguments'),   # named server/tool
}
CLAUDE_FILE_TOOLS = {   # claude tool -> the input field that names the file it writes
    'Edit': 'file_path',
    'Write': 'file_path',
    'MultiEdit': 'file_path',
    'NotebookEdit': 'notebook_path',
}


class StreamTranslator:
    """Turns what a claude or codex CLI says into events.

    feed() takes one message, emits its events through emit_event, and returns the turns it
    closed (usually none, sometimes one). The method for each provider is named after it."""

    def __init__(self, provider, last_turn, session, emit_event, input_waiting):
        self.provider = provider
        self.turn = last_turn               # the open turn's number, or the last one's
        self.before_first = last_turn       # until a turn opens, any line opens one
        self.session = session
        self.emit_event = emit_event
        self.input_waiting = input_waiting  # () -> whether a delivered message has no turn yet
        self.current = None                 # the open turn's Turn
        self.turn_id = None                 # codex: the open turn's id
        self.notes = []                     # codex: this turn's error notices
        self.calls = {}                     # codex: this turn's tool calls, item id -> input
        self.spent = 0                      # claude: its cumulative cost before this turn

    def emit(self, kind, **fields):
        self.emit_event(event(kind, self.turn, **fields))

    def open_turn(self):
        """A turn begins -> the turn that was open, closed as cut, if any."""
        cut = [self.cut()] if self.current else []
        self.turn += 1
        self.current = Turn()
        self.emit('turn_start', session=self.session)
        return cut

    def turn_for_line(self):
        """A line that belongs to a turn -> [] when one is open, else what open_turn() returns,
        or None: once a turn has ended, only a delivered message opens the next one."""
        if self.current:
            return []
        if self.turn == self.before_first or self.input_waiting():
            return self.open_turn()
        return None

    def close(self, status, text, usage, final, error_text='', turn=None, extra=None):
        started = self.current.started if self.current else None
        self.current = None
        self.turn_id = None
        return closed_turn(turn or self.turn, status, text, usage, final, error_text,
                           self.session, started, extra)

    def cut(self, turn=None):
        """The stream stopped inside, or before, a turn: no answer, and the usage so far."""
        usage = normalize_usage(self.current.usage, self.provider) if self.current else None
        return self.close('incomplete', '', usage, 'none', '\n'.join(self.notes), turn=turn)

    def stray(self, what):
        self.emit('progress', text='%s belongs to no open turn (the last was turn %d): not a turn'
                  % (what, self.turn))
        return []

    def tool_call(self, name, call, tool_input):
        self.emit('tool_call', tool=name, call=call, input=small(tool_input))
        field = CLAUDE_FILE_TOOLS.get(name)
        if field and isinstance(tool_input, dict) and tool_input.get(field):
            self.emit('file', path=tool_input[field], tool=name, call=call)
        self.current.add_tool()

    def feed(self, message):
        return getattr(self, self.provider)(message)

    def claude(self, line):
        kind = line.get('type')
        subtype = line.get('subtype')
        if kind == 'system' and subtype == 'init':   # claude sends init at the start of each turn
            self.session = line.get('session_id') or self.session
            return self.open_turn()
        if kind not in ('assistant', 'user', 'result', 'tool_progress'):
            self.emit('progress', text='%s%s' % (kind, '/' + subtype if subtype else ''))
            return []
        closed = self.turn_for_line()
        if closed is None:
            return self.stray(kind)
        if kind == 'result':
            return closed + [self.claude_result(line)]
        if kind == 'tool_progress':
            self.emit('progress', tool=line.get('tool_name'), call=line.get('parent_tool_use_id'),
                      text='heartbeat %ss' % line.get('elapsed_time_seconds'))
            return closed
        content = (line.get('message') or {}).get('content')
        if isinstance(content, str):
            self.emit('progress', text='user message: ' + clip(content, 120))
        for block in content if isinstance(content, list) else []:
            block_type = block.get('type') if isinstance(block, dict) else None
            if block_type == 'text':
                self.emit('text', text=block.get('text', ''))
            elif block_type == 'thinking':
                self.emit('thought', text=block.get('thinking', ''))
            elif block_type == 'tool_use':
                self.tool_call(block.get('name'), block.get('id'), block.get('input'))
            elif block_type == 'tool_result':
                self.emit('tool_result', call=block.get('tool_use_id'),
                          output=clip(block.get('content')),
                          status='error' if block.get('is_error') else 'ok')
            else:
                self.emit('progress', text='%s block %s' % (kind, block_type))
        return closed

    def claude_result(self, line):
        """A result line -> the turn it closes. Its `result` is the answer only on success."""
        self.session = line.get('session_id') or self.session
        failed = bool(line.get('is_error')) or line.get('subtype') not in (None, 'success')
        error_text = ''
        if failed:
            fields = ('result', 'errors', 'api_error_status', 'terminal_reason')
            error_text = ' '.join(str(line[k]) for k in fields if line.get(k))
            self.emit('error', text=clip(error_text or line.get('subtype')))
            status = 'error'
        elif line.get('stop_reason') == 'max_tokens':
            status = 'truncated'
        else:
            status = 'ok'
        text = line.get('result') if status == 'ok' else ''
        text = text if isinstance(text, str) else ''
        extra = None
        total = line.get('total_cost_usd')
        if type(total) in (int, float):   # claude reports the process's cost so far
            extra = {'cost_usd': round(total - self.spent, 10)}
            self.spent = total
        usage = normalize_usage(line.get('usage'), 'claude')
        return self.close(status, text, usage, 'field', error_text, extra=extra)

    def codex(self, message):
        """An app-server notification. A turn's answer is its last agentMessage.

        Every item, usage report and turn end names its thread and turn. Another thread's (a
        collaborator codex started reports over the same connection) is never the seat's text,
        calls or turns: while a seat turn is open, its usage counts toward that turn and its
        items and turns show as progress. Anything for a turn other than the open one (a command
        that finishes after its turn) is stray. Deltas and status chatter stay in stream.jsonl:
        each item arrives whole at item/completed."""
        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        item = params.get('item') if isinstance(params.get('item'), dict) else {}
        turn_id = params.get('turnId') or (params.get('turn') or {}).get('id')
        if params.get('threadId') not in (None, self.session):
            self.collaborator(method, params, item)
            return []
        if method == 'turn/started':
            closed = self.open_turn()
            self.turn_id = turn_id
            self.notes = []
            self.calls = {}
            return closed
        if method == 'error':   # a notice, such as a retry; a failed turn/completed is the failure
            error = params.get('error') if isinstance(params.get('error'), dict) else {}
            parts = (error.get('message'), error.get('codexErrorInfo'))
            self.notes.append(' '.join(str(x) for x in parts if x))
            self.emit('progress', text='error notice: ' + clip(self.notes[-1], 200))
            return []
        if method not in ('item/started', 'item/completed', 'thread/tokenUsage/updated',
                          'turn/completed', 'turn/plan/updated'):
            return []
        if self.current is None or turn_id != self.turn_id:
            return self.stray(' '.join(x for x in (method, item.get('type')) if x))
        kind = item.get('type')
        if method == 'thread/tokenUsage/updated':   # one per model call; `last` is that call's
            self.current.usage = add_usage(self.current.usage,
                                           (params.get('tokenUsage') or {}).get('last'))
        elif method == 'turn/completed':
            return [self.codex_turn_end(params.get('turn') or {})]
        elif method == 'turn/plan/updated':
            steps = [x for x in params.get('plan') or [] if isinstance(x, dict)]
            plan = '; '.join('[%s] %s' % (x.get('status'), x.get('step')) for x in steps)
            self.emit('progress', text=clip('plan: ' + plan, 300))
        elif kind == 'agentMessage' and method == 'item/completed':
            self.emit('text', text=item.get('text', ''))
            self.current.add_text(item.get('id'), item.get('text', ''))
            self.current.end_message()
        elif kind == 'reasoning' and method == 'item/completed':
            thought = ' '.join(item.get('summary') or item.get('content') or [])
            if thought:
                self.emit('thought', text=thought)
        elif kind in CODEX_TOOL_ITEMS:
            self.codex_tool(method, kind, item)
        elif kind not in ('userMessage', None) and method == 'item/completed':
            self.emit('progress', text=clip('item %s' % kind, 200))
        return []

    def collaborator(self, method, params, item):
        """A notification from another thread on the same app-server."""
        if self.current is None:
            return
        if method == 'thread/tokenUsage/updated':
            self.current.usage = add_usage(self.current.usage,
                                           (params.get('tokenUsage') or {}).get('last'))
        elif method in ('item/completed', 'turn/started', 'turn/completed'):
            words = ('collaborator', method, item.get('type'))
            self.emit('progress', text=' '.join(x for x in words if x))

    def codex_turn_end(self, turn):
        """turn/completed -> the turn it closes."""
        status = turn.get('status')
        error = turn.get('error') if isinstance(turn.get('error'), dict) else {}
        usage = normalize_usage(self.current.usage, 'codex')
        if status == 'completed':
            return self.close('ok', self.current.answer(), usage, 'boundary')
        parts = (error.get('message'), error.get('codexErrorInfo'))
        why = ' '.join(str(x) for x in parts if x) or 'turn %s' % status
        if status == 'interrupted':
            return self.close('cancelled', '', usage, 'boundary', '\n'.join(self.notes + [why]))
        self.emit('error', text=clip(why))
        return self.close('error', '', usage, 'boundary', '\n'.join(self.notes + [why]))

    def codex_tool(self, method, kind, item):
        """A tool item, at its start or its completion (one first seen at completion still
        opens its call)."""
        name, field = CODEX_TOOL_ITEMS[kind]
        name = name or '%s/%s' % (item.get('server'), item.get('tool'))
        tool_input = item.get(field)
        item_id = item.get('id')
        if item_id not in self.calls:
            self.calls[item_id] = tool_input
            self.tool_call(name, item_id, tool_input)
        if method != 'item/completed':
            return
        failed = (item.get('status') in ('failed', 'declined')
                  or item.get('exitCode') not in (None, 0) or item.get('error'))
        output = clip(item.get('aggregatedOutput') or item.get('result') or item.get('error'))
        # webSearch has no status, and its query may arrive only now
        late_input = None if self.calls[item_id] else small(tool_input)
        self.emit('tool_result', tool=name, call=item_id, status='error' if failed else 'ok',
                  input=late_input, output=output)
        changes = item.get('changes') if kind == 'fileChange' else None
        for change in changes if isinstance(changes, list) else []:
            if isinstance(change, dict) and change.get('path'):
                self.emit('file', path=change['path'], tool=name, call=item_id)
        self.current.add_tool()
