"""What every hands test uses: a sandbox to run hands.sh in, readers for a job dir's files, the
stub CLIs' log, process checks, recorded transcripts, and a stand-in for the driver's socket.

Nothing here spends quota or reaches the network. The stub CLIs in tests/stubs come first on
PATH, the jobs root and the opencode store live in the sandbox, and the variables naming a real
driver session are dropped from the environment the moment this module is imported.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
HANDS = os.path.join(ROOT, 'hands.sh')
STUBS = os.path.join(TESTS, 'stubs')
FIXTURES = os.path.join(TESTS, 'fixtures')
sys.path.insert(0, ROOT)

# A run inside a driver session (Claude Code, Codex, OpenCode) inherits the variables that name
# it. No test may push to it, and no stub setting from the outer environment may leak into a test.
import push   # here, once ROOT is on the path
import seat
import wires

DRIVER_NAMES = set(push.DRIVER_NAMES)
for _name in list(os.environ):
    if _name in DRIVER_NAMES or _name.startswith('STUB_'):
        del os.environ[_name]

SPECS = {
    'claude': 'claude:sonnet',
    'codex': 'codex:gpt-5.6-terra',
    'opencode': 'opencode:opencode-go/deepseek-v4.1-flash',
}
CODEX_THREAD = 'th_stub123'
OPENCODE_SESSION = 'ses_f2c74432dffeoPDAL0Eq6RHjaI'        # in opencode-acp-seat-sample.jsonl


# ---------------------------------------------------------------------------------------------
# The sandbox


class Run:
    """One finished command: its exit code, stdout and stderr."""

    def __init__(self, rc, out, err):
        self.rc = rc
        self.out = out
        self.err = err

    @property
    def json(self):
        """The last line of stdout, parsed."""
        return last_json(self.out)


class Sandbox:
    """A temporary directory to run hands.sh in, with the stub CLIs first on PATH.

    Every job dir a test names is made in it, and so is the jobs root ($XDG_STATE_HOME/hands)
    and the opencode store ($XDG_DATA_HOME), which holds a fake key as a real one would. A seat's
    stub CLIs log to <job dir>.stub.jsonl unless the test names another STUB_LOG."""

    def __init__(self, name):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='hands-%s-' % name))
        for sub in ('tmp', 'cache', 'state', 'work'):
            os.makedirs(self.path(sub))
        self.work = self.path('work')
        self.store = self.path('store')
        os.makedirs(os.path.join(self.store, 'opencode'))
        self.write('store/opencode/auth.json', '{"opencode-go":{"key":"sk-test"}}')
        self.task = self.write('task.md', 'task\n')
        self.turn_two = self.write('turn-two.md', 'turn two\n')
        self.env = dict(os.environ)
        self.env.update(PATH=STUBS + os.pathsep + os.environ['PATH'], TMPDIR=self.path('tmp'),
                        XDG_CACHE_HOME=self.path('cache'), XDG_STATE_HOME=self.path('state'),
                        XDG_DATA_HOME=self.store)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, name, text):
        path = self.path(name)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def environment(self, changes):
        """This sandbox's environment with changes made: a None value unsets the name."""
        env = dict(self.env)
        for name, value in changes.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run(self, argv, cwd=None, timeout=120, **env):
        done = subprocess.run(argv, cwd=cwd or self.root, env=self.environment(env),
                              capture_output=True, text=True, timeout=timeout)
        return Run(done.returncode, done.stdout, done.stderr)

    def hands(self, *args, cwd=None, timeout=120, **env):
        """hands.sh with these arguments, in the sandbox (env: variables to set, None unsets)."""
        return self.run([HANDS] + list(args), cwd=cwd, timeout=timeout, **env)

    def popen(self, *args, cwd=None, **env):
        """hands.sh started in the background."""
        return subprocess.Popen([HANDS] + list(args), cwd=cwd or self.root,
                                env=self.environment(env), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    @staticmethod
    def stub_log(job):
        return StubLog(job + '.stub.jsonl')

    def seat_env(self, job, env):
        env.setdefault('STUB_LOG', job + '.stub.jsonl')
        return env

    def start(self, name, spec, *flags, task=None, **env):
        """hands.sh start FLAGS --out <name> SPEC TASK -> (the job dir, the run)."""
        job = self.path(name)
        run = self.hands('start', *flags, '--out', job, spec, task or self.task,
                         **self.seat_env(job, env))
        return job, run

    def one_shot(self, name, spec, *flags, task=None, **env):
        """The one-shot form: start, wait for the turn, stop -> (the job dir, the run)."""
        job = self.path(name)
        run = self.hands(*flags, '--out', job, spec, task or self.task,
                         **self.seat_env(job, env))
        return job, run

    def send(self, job, task=None, **env):
        return self.hands('send', job, task or self.turn_two, **self.seat_env(job, env))

    def wait(self, job, *flags, **env):
        return self.hands('wait', job, *flags, **env)

    def stop(self, job):
        return self.hands('stop', job)

    def close(self):
        """Stop every seat in the sandbox, then delete it."""
        for top, dirs, files in os.walk(self.root):
            if 'session' in files and os.path.isfile(os.path.join(top, 'session')):
                subprocess.run([HANDS, 'stop', top], env=self.env, capture_output=True)
        for top, dirs, files in os.walk(self.root):   # a test may leave a read-only dir behind
            for name in dirs:
                os.chmod(os.path.join(top, name), 0o700)
        shutil.rmtree(self.root, ignore_errors=True)


class SeatSandbox(Sandbox):
    def save_transcript(self, name, rows):
        return save_transcript(self.path(name), rows)

    def check_drivers(self):
        for top, _, files in os.walk(self.root):
            path = os.path.join(top, 'driver')
            if 'driver' in files and not os.path.islink(path):
                address = read_json(path)['address'][0]
                if not address.startswith((self.root + os.sep, 'test-')):
                    raise AssertionError('unexpected driver address in %s' % top)

    def close(self):
        try:
            self.check_drivers()
        finally:
            super().close()


# ---------------------------------------------------------------------------------------------
# A job dir's files


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f.read().split('\n') if line.strip()]


def last_json(text):
    lines = [line for line in text.split('\n') if line.strip()]
    return json.loads(lines[-1]) if lines else None


def events(job):
    return read_jsonl(os.path.join(job, 'events.jsonl'))


def envelope(job, turn=None):
    if turn is None:
        return read_json(os.path.join(job, 'envelope.json'))
    return read_json(os.path.join(job, 'turn-%d' % turn, 'envelope.json'))


def result(job, turn=None):
    if turn is None:
        return read_text(os.path.join(job, 'result.txt'))
    return read_text(os.path.join(job, 'turn-%d' % turn, 'result.txt'))


def handle(job):
    return read_json(os.path.join(job, 'session'))


def turn_ends(job):
    """[(turn, status)] of every turn_end in the events, in order."""
    return [(e['turn'], e['status']) for e in events(job) if e['kind'] == 'turn_end']


def first_index(items, test):
    return next((i for i, item in enumerate(items) if test(item)), -1)


# ---------------------------------------------------------------------------------------------
# What the stub CLIs logged


class Launch:
    """One run of a stub CLI: what it was started with, read and how it ended."""

    def __init__(self, record):
        self.cli = record['cli']
        self.pid = record['pid']
        self.argv = record['argv']
        self.cwd = record['cwd']
        self.env = record['env']           # the names of its environment variables
        self.values = record['values']     # the values of the few the stubs show
        self.lines = []                    # what it read on stdin
        self.ended = []                    # 'eof' and 'term', in the order they came
        self.children = []
        self.parked = False

    def messages(self):
        """The JSON-RPC lines it read that carry a method -> [(method, params)]."""
        found = []
        for line in self.lines:
            message = json.loads(line) if line.strip() else {}
            if 'method' in message:
                found.append((message['method'], message.get('params') or {}))
        return found

    def methods(self):
        return [method for method, _ in self.messages()]


class StubLog:
    def __init__(self, path):
        self.path = path

    def records(self):
        return read_jsonl(self.path) if os.path.exists(self.path) else []

    def launches(self, cli=None):
        found = {}
        for record in self.records():
            if record['event'] == 'launch':
                found[record['pid']] = Launch(record)
                continue
            launch = found.get(record['pid'])
            if launch is None:
                continue
            if record['event'] == 'in':
                launch.lines.append(record['line'])
            elif record['event'] in ('eof', 'term'):
                launch.ended.append(record['event'])
            elif record['event'] == 'child':
                launch.children.append(record['child'])
            elif record['event'] == 'parked':
                launch.parked = True
        return [launch for launch in found.values() if cli is None or launch.cli == cli]

    def seats(self):
        """The launches that ran a seat (not `opencode session ...`)."""
        return [launch for launch in self.launches() if launch.argv[:1] != ['session']]

    def events(self, name):
        return [record for record in self.records() if record['event'] == name]


# ---------------------------------------------------------------------------------------------
# Processes and waiting


def wait_until(check, timeout=10, step=0.1):
    """Call check() until it is true or timeout seconds pass -> its last value."""
    deadline = time.time() + timeout
    while True:
        try:
            value = check()
        except (OSError, ValueError, KeyError, IndexError):
            value = None
        if value or time.time() >= deadline:
            return value
        time.sleep(step)


def processes():
    """Every live process -> [(pid, command)]. A zombie is dead: it is left out."""
    table = subprocess.run(['ps', '-A', '-ww', '-o', 'pid=,stat=,command='],
                           capture_output=True, text=True).stdout
    rows = [line.split(None, 2) for line in table.splitlines()]
    return [(int(r[0]), r[2]) for r in rows if len(r) == 3 and not r[1].startswith('Z')]


def wrapper_pids(job):
    """The pids of the seat wrappers running job (found by their argv, as hands finds them)."""
    job = os.path.realpath(job)
    mine = re.compile(r'seat\.py spawn (?:%s|%s)( |$)'
                      % (re.escape(job), re.escape(seat.job_arg(job))))
    return [pid for pid, command in processes() if mine.search(command)]


def gone(job=None, pids=(), timeout=5):
    """Within timeout seconds: no wrapper of job, and none of pids, is left running."""
    def check():
        live = {pid for pid, _ in processes()}
        return not (job and wrapper_pids(job)) and not live & set(pids)
    return bool(wait_until(check, timeout))


def alive(pid):
    return pid in {p for p, _ in processes()}


def kill(pid, sig=signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def age(days, *paths):
    """Set every entry under each path, and the path itself, to `days` days old."""
    then = time.time() - days * 86400
    for root in paths:
        for top, dirs, files in os.walk(root, topdown=False):
            for name in dirs + files:
                os.utime(os.path.join(top, name), (then, then), follow_symlinks=False)
        os.utime(root, (then, then), follow_symlinks=False)


# ---------------------------------------------------------------------------------------------
# Recorded ACP transcripts, and the edits tests make to them


def transcript(name):
    return read_jsonl(os.path.join(FIXTURES, name))


def save_transcript(path, rows):
    with open(path, 'w') as f:
        f.writelines(json.dumps(row) + '\n' for row in rows)
    return path


def rpc_message(row):
    return row['msg'] if isinstance(row['msg'], dict) else {}


def request_index(rows, method, start=0):
    return next(i for i in range(start, len(rows))
                if rows[i]['dir'] == 'out' and rpc_message(rows[i]).get('method') == method)


def without_cancel(rows):
    return [r for r in rows if not (r['dir'] == 'out'
                                    and rpc_message(r).get('method') == 'session/cancel')]


def hung_on_first_prompt(rows):
    """The first prompt's updates, but never its answer: the turn stays open."""
    prompt = request_index(rows, 'session/prompt')
    prompt_id = rows[prompt]['msg']['id']
    answer = next(j for j in range(prompt + 1, len(rows))
                  if rows[j]['dir'] == 'in' and rpc_message(rows[j]).get('id') == prompt_id)
    return rows[:answer]


def resume_refused(rows):
    """session/resume answered 'Session not found'."""
    resume = request_index(rows, 'session/resume')
    error = {'code': -32602, 'message': 'Session not found'}
    reply = {'jsonrpc': '2.0', 'id': rows[resume]['msg']['id'], 'error': error}
    return rows[:resume + 1] + [{'dir': 'in', 'msg': reply}]


# ---------------------------------------------------------------------------------------------
# The driver's socket


class DriverSocket:
    """A stand-in for the driver's inbox: it records each connection's lines (a push is an auth
    line, then one user line) and answers `reply`. reply None sends nothing; 'HOLD' sends nothing
    and holds the connection open 3 s, as the host does."""

    def __init__(self, path):
        self.path = path
        self.reply = None
        self.served = []   # (the order its connection came in, its lines)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def connections(self):
        """Each connection's lines, in the order the connections came (each is read by a thread
        of its own, so they may finish in another order)."""
        return [lines for _, lines in sorted(self.served, key=lambda item: item[0])]

    def _accept(self):
        number = 0
        while True:
            try:
                connection = self.server.accept()[0]
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection, number), daemon=True).start()
            number += 1

    def _serve(self, connection, number):
        data = b''
        while data.count(b'\n') < 2:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
        self.served.append((number, [json.loads(line) for line in data.split(b'\n')
                                     if line.strip()]))
        try:
            if self.reply == 'HOLD':
                time.sleep(3)
            elif self.reply:
                connection.sendall(self.reply)
        except OSError:
            pass
        connection.close()

    def pushes(self, job, what):
        """job's pushes whose line 1 names `what` (a status, or 'mail to driver')
        -> [(auth, user, text lines)]."""
        job = os.path.realpath(job)
        found = []
        for lines in list(self.connections):
            if len(lines) != 2:
                continue
            text = lines[1]['message']['content'].split('\n')
            first = re.match(r'\S+ t\d+ %s: ' % re.escape(what), text[0])
            if len(text) > 2 and text[1] == job and first:
                found.append((lines[0], lines[1], text))
        return found

    def close(self):
        self.server.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# Shared seat checks


def agent(job, n=0):
    return Sandbox.stub_log(job).seats()[n]


def progress(job, text=None):
    found = [e for e in events(job) if e['kind'] == 'progress']
    return [e for e in found if text is None or text in str(e.get('text', ''))]


def turn_started(job, turn=1):
    return any(e['kind'] == 'turn_start' and e['turn'] == turn for e in events(job))


def file_exists(*parts):
    return wait_until(lambda: os.path.exists(os.path.join(*parts)))


class SeatChecks(unittest.TestCase):
    def assertVocabulary(self, found, kinds, statuses, last, result_text):
        """Every event uses the vocabulary; each tool_result and file pairs a tool_call; a usage
        precedes each turn_end; the turn_ends carry these statuses, a session and a final."""
        ends = [e for e in found if e['kind'] == 'turn_end']
        calls = {e.get('call') for e in found if e['kind'] == 'tool_call'}
        for e in found:
            self.assertIsInstance(e.get('t'), str)
            self.assertIn(e.get('kind'), wires.EVENT_KINDS)
            self.assertIsInstance(e.get('turn'), int)
        self.assertLessEqual(set(kinds), {e['kind'] for e in found})
        self.assertEqual([e['turn'] for e in ends], list(range(1, len(ends) + 1)))
        self.assertEqual([e['status'] for e in ends], statuses)
        self.assertTrue(all(e.get('session') and e.get('final') for e in ends))
        for e in found:
            if e['kind'] in ('tool_result', 'file'):
                self.assertIn(e.get('call'), calls)
        for i, e in enumerate(found):
            if e['kind'] == 'turn_end':
                self.assertTrue(any(x['kind'] == 'usage' and x['turn'] == e['turn']
                                    for x in found[:i]))
        self.assertEqual(ends[-1]['text'].strip(), last)
        self.assertEqual(result_text, ends[-1]['text'])

    def assertGone(self, job, *pids):
        self.assertTrue(gone(job, pids), 'a wrapper or a process outlived the seat')


# ---------------------------------------------------------------------------------------------
# Running a test file


def main(name):
    """Run this module's tests; print 'name: N passed, M failed'; exit 1 on any failure."""
    result = unittest.main(module='__main__', exit=False, verbosity=2).result
    bad = len(result.failures) + len(result.errors)
    print('%s: %d passed, %d failed' % (name, result.testsRun - bad, bad))
    sys.exit(1 if bad else 0)
