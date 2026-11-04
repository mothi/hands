#!/usr/bin/env python3
"""hands: run a task on another provider's coding-agent CLI (claude, codex, grok, opencode) as a
subagent seat. Each CLI keeps its own login; hands stores no credentials (while a seat runs, its
job dir keeps the host's push socket path and token: README, Security model).

  hands.sh start [opts] provider[:model] prompt-file
        start a seat and return at once: {"status":"spawned","dir":DIR,...}
  hands.sh [opts] provider[:model] prompt-file
        start a seat, wait for its answer, then stop it
  hands.sh send DIR FILE          the next turn (a seat whose process is gone relaunches)
  hands.sh mail DIR TEXT          a driver line, which the seat's live wrapper delivers
  hands.sh wait DIR [--for turn|mail|exit] [--timeout S]
        wait until the turn ends, then print the answer and the envelope
  hands.sh events DIR [--follow]  a readable view of DIR/events.jsonl
  hands.sh stop DIR               close the seat as its deadline does; its envelope still lands
  hands.sh --probe [claude|codex|grok|opencode|all]
        a one-token ping per provider (it spends a little quota)
  hands.sh quota [--json] [provider ...]
        the quota board: read-only, it never spends

opts (all before the provider spec; the prompt is always a file):
  --cwd DIR        where the seat works (default: the current dir). A seat has full read and
                   write access, as you: point it at a worktree or a scratch dir
  --out DIR        the job dir, new or empty, made 0700 (default: a fresh dir in
                   $XDG_STATE_HOME/hands, else ~/.local/state/hands, where every start deletes
                   dirs idle for 30 days)
  --timeout SECS   a wall clock per launch (default: none; the seat runs until its work ends
                   or a stop)
  --effort <tier>  claude, codex: low|medium|high|xhigh|max, passed through (codex adds ultra
                   where it lists it); grok, opencode: required, a value the model lists
                   (grok-4.6: low|medium|high|xhigh)

envelope: {"status":"ok|error|incomplete|cancelled|truncated|timeout|auth-expired|
             provider-quota-dead|not-installed|refused",
  "provider","model","effort","rc","seconds","reset","dir","usage","session","turn",
  "delivery",...}
  exit 0 ok, 1 any other status, 2 refused or a usage error. README.md is the manual;
  CONTRIBUTING.md has the engine's rules.
"""
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import quota
import seat
import wires

SEAT_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seat.py')

# A dir in the jobs root with no live wrapper is deleted this long after its last write.
IDLE_LIMIT = 30 * 86400
OWN_NAME = re.compile(r'\d{8}-\d{6}-[a-z]+-\d+|probe-\d+-[a-z]+')   # the names hands.py gives

# The seat contract. It heads every prompt and names no job dir, so every seat's prompt starts
# with the same text and the provider's prompt cache can reuse it. The line that differs per
# seat, seat_line(), comes after the task.
SEAT_BOX = """\
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
"""

PROBE_SPECS = {   # each provider's smallest model; an ACP seat names an effort its model lists
    'claude': ('claude:haiku', ''),
    'grok': ('grok', 'low'),
    'opencode': ('opencode:opencode-go/deepseek-v4.1-flash', 'low'),   # its cheapest that works
}


def seat_line(job_dir):
    return 'YOUR JOB DIR: %s (your outbox: %s/outbox.jsonl).\n' % (job_dir, job_dir)


class Options:
    """The flags before the provider spec."""

    def __init__(self):
        self.cwd = os.getcwd()
        self.out = ''
        self.timeout = ''
        self.effort = ''
        self.probe = ''
        self.start = False   # `start`: return once the seat is spawned


def say(message):
    print('hands: ' + message, file=sys.stderr)


def cli_path(provider):
    """The provider's CLI on PATH, or where opencode's installer puts it."""
    found = shutil.which(provider)
    fallback = os.path.expanduser('~/.opencode/bin/opencode')
    if not found and provider == 'opencode' and os.access(fallback, os.X_OK):
        return fallback
    return found


def is_empty_dir(path):
    try:
        return not os.listdir(path)
    except OSError:   # not a dir, or unreadable
        return False


# ---------------------------------------------------------------------------------------------
# Starting a seat


def buy(spec, prompt_file, options):
    """Make the job dir and its prompt, then launch the seat."""
    provider, _, model = spec.partition(':')
    if not re.fullmatch(r'[a-z0-9]+', provider):   # it names the job dir: only a plain name
        say("bad provider '%s' (claude|codex|grok|opencode)" % provider)
        return 2
    sweep()
    job_dir = make_job_dir(provider, options)
    if job_dir is None:
        return 2
    if not os.path.isfile(prompt_file):
        say('prompt file not found: %s' % prompt_file)
        if prompt_file.startswith('-'):
            say("'%s' is a FLAG in the prompt-file position — options must PRECEDE the "
                "provider positional (hands.sh [opts] provider[:model] prompt-file); flags "
                "after it are read as the prompt file" % prompt_file)
        end_early(job_dir, 'error', provider, model, 2, options)
        return 2
    write_prompt(job_dir, prompt_file)
    if provider not in wires.PROVIDERS:   # a typo, or a provider hands has no wire for
        say("unknown provider '%s' (claude|codex|grok|opencode)" % provider)
        end_early(job_dir, 'error', provider, model, 2, options)
        return 2
    return launch(provider, model, job_dir, options)


def make_job_dir(provider, options):
    """Make the seat's job dir -> its canonical path, or None (after saying why)."""
    if options.out:   # new or empty: a start never writes over files it did not make
        job_dir = options.out
        if os.path.lexists(job_dir) and not is_empty_dir(job_dir):
            say('--out %s is not empty — name a new or empty dir, or omit --out for a fresh one '
                'in the jobs root' % job_dir)
            return None
        try:   # its parents keep your umask; the dir itself is 0700, an existing one too
            os.makedirs(job_dir, exist_ok=True)
            os.chmod(job_dir, 0o700)
        except OSError:
            pass
    else:
        name = '%s-%s-%d' % (time.strftime('%Y%m%d-%H%M%S'), provider, os.getpid())
        job_dir = os.path.join(seat.JOBS, name)
        old_umask = os.umask(0o077)   # prompts live here
        try:
            os.makedirs(job_dir, exist_ok=True)
        except OSError:
            pass
        os.umask(old_umask)
    if not (os.path.isdir(job_dir) and os.access(job_dir, os.W_OK | os.X_OK)):
        say('cannot create the job dir %s' % (options.out or 'in ' + seat.JOBS))
        return None
    return os.path.realpath(job_dir)   # one name for the prompt, the handle and the wrapper


def write_prompt(job_dir, prompt_file):
    """Write prompt.txt, the one file every provider reads: the seat box, a blank line, the
    task, a blank line, the job-dir line. The inbox and the outbox it names exist from now."""
    with open(prompt_file, 'rb') as f:
        task = f.read()
    if task and not task.endswith(b'\n'):
        task += b'\n'
    with open(os.path.join(job_dir, 'prompt.txt'), 'wb') as f:
        f.write(SEAT_BOX.encode() + b'\n' + task + b'\n' + seat_line(job_dir).encode())
    for name in ('inbox.jsonl', 'outbox.jsonl'):
        open(os.path.join(job_dir, name), 'a').close()


def end_early(job_dir, status, provider, model, rc, options, reset='', reason=''):
    """A start that ends before its seat runs: write its envelope, and print it."""
    seat.write_envelope(job_dir, status, provider, model, rc, 0, reset, None, options.effort,
                        reason=reason or None)
    with open(os.path.join(job_dir, 'envelope.json')) as f:
        sys.stdout.write(f.read())


def launch(provider, model, job_dir, options):
    """Write the seat's handle, DIR/session, and spawn its wrapper. Without `start`, wait for
    the answer, then stop the seat.

    The handle is what every relaunch resumes with: the provider, model and effort, the
    canonical cwd, the timeout, the CLI's path, and the session id the wrapper records."""
    cli = cli_path(provider)
    if not cli:
        end_early(job_dir, 'not-installed', provider, model, 0, options)
        return 1
    if not os.path.isdir(options.cwd):
        say('--cwd %s unresolvable' % options.cwd)
        end_early(job_dir, 'error', provider, model, 2, options)
        return 2
    timeout = parse_timeout(options.timeout)
    if timeout is False:
        say('--timeout takes seconds')
        end_early(job_dir, 'error', provider, model, 2, options)
        return 2
    handle = {'provider': provider, 'model': model or None, 'effort': options.effort or None,
              'cwd': os.path.realpath(options.cwd), 'timeout': timeout, 'cli': cli,
              'session': None}
    with open(os.path.join(job_dir, 'session'), 'w') as f:
        f.write(json.dumps(handle, separators=(',', ':')) + '\n')
    prompt = os.path.join(job_dir, 'prompt.txt')
    if options.start:
        return spawn(job_dir, prompt)

    def interrupted(signum, frame):   # an interrupted start stops its seat first
        sys.stdout.flush()
        stop(job_dir)
        sys.exit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(signum) != signal.SIG_IGN:   # nohup's ignored HUP stays ignored
            signal.signal(signum, interrupted)
    ack = subprocess.run([sys.executable, SEAT_PY, 'spawn', job_dir, prompt],
                         stdout=subprocess.PIPE, text=True)
    if ack.returncode == 2:   # refused before any prompt: that envelope is the answer
        sys.stdout.write(ack.stdout)
        return 2
    rc = wait(job_dir, seat.read_json(os.path.join(job_dir, 'session')), 'turn', None)
    sys.stdout.flush()
    stop(job_dir)
    return rc


def parse_timeout(text):
    """--timeout's value -> None when unset, a number, or False when it is not one."""
    if text == '':
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return False
    return value if math.isfinite(value) else False


def spawn(job_dir, message_file):
    # Always a subprocess: the wrapper it forks is found by its command line, seat.py spawn DIR.
    return subprocess.call([sys.executable, SEAT_PY, 'spawn', job_dir, message_file])


# ---------------------------------------------------------------------------------------------
# The verbs on a seat


def send(job_dir, message_file):
    """The next turn. A live seat takes it now; a seat whose process is gone relaunches on its
    recorded session, with the same model, effort and cwd."""
    if not os.path.isfile(os.path.join(job_dir, 'session')):
        say('%s is not a seat dir (it holds no session handle)' % job_dir)
        return 2
    job_dir = os.path.realpath(job_dir)
    if not os.path.isfile(message_file):
        say('send file not found: %s' % message_file)
        return 2
    rc = seat.main(['send', job_dir, message_file])
    if rc != 3:
        return rc
    handle = seat.read_json(os.path.join(job_dir, 'session'))
    options = Options()
    options.effort = handle.get('effort') or ''
    if not handle.get('session'):
        say('%s recorded no session (its seat never reached the model) — start a fresh seat'
            % job_dir)
        end_early(job_dir, 'refused', handle['provider'], handle.get('model') or '', 2, options,
                  reason='no-session')
        return 2
    return spawn(job_dir, message_file)


def run_verb(args):
    """mail, wait, events or stop, on DIR."""
    verb = args[0]
    if len(args) < 2 or not os.path.isdir(args[1]):
        say('usage: hands.sh %s DIR ...' % verb)
        return 2
    job_dir = os.path.realpath(args[1])
    handle = seat.read_json(os.path.join(job_dir, 'session'))
    if verb == 'mail':
        return mail(job_dir, handle, args[2:])
    if verb == 'wait':
        mode = args[args.index('--for') + 1] if '--for' in args[:-1] else 'turn'
        timeout = float(args[args.index('--timeout') + 1]) if '--timeout' in args[:-1] else None
        return wait(job_dir, handle, mode, timeout) if mode in ('turn', 'mail', 'exit') else 2
    if verb == 'events':
        return events(job_dir, '--follow' in args)
    if verb != 'stop':
        return 2
    rc = stop(job_dir)
    envelope = seat.read_json(os.path.join(job_dir, 'envelope.json'))
    if envelope:
        print(seat.printed(envelope))
    return rc


def mail(job_dir, handle, words):
    """Append a driver line to the seat's inbox; the seat's live wrapper delivers it."""
    if not words:
        return 2
    line = {'t': wires.now(), 'from': 'driver', 'text': ' '.join(words)}
    try:   # never through a link the seat planted
        seat.append_json(os.path.join(job_dir, 'inbox.jsonl'), line)
    except OSError as e:
        say('%s/inbox.jsonl: %s -- not written' % (job_dir, e.strerror or e))
        return 2
    if handle and not seat.wrappers(job_dir):
        say('%s has no live seat: the mail waits in its inbox until that seat runs again (send '
            'relaunches it)' % job_dir)
        return 1
    return 0


def wait(job_dir, handle, mode, timeout):
    """Wait for the seat's turn to end (mode turn), for new mail (mail), or for its exit (exit),
    and print what came. A dir with no handle is waited on until its envelope lands."""
    started = time.time()
    state = seat.SeatState(job_dir)
    outbox = os.path.join(job_dir, 'outbox.jsonl')
    outbox_size = seat.file_size(outbox)
    checks = 0
    while True:
        state.read()
        if mode == 'mail' and seat.file_size(outbox) > outbox_size:
            sys.stdout.write(seat.read_lines(outbox, outbox_size)[0])
            return 0
        if handle and mode != 'exit' and state.expected and state.ended >= state.expected:
            return print_result(job_dir)   # an idle seat mails nothing more
        if checks % 4 == 0:
            if handle:
                alive = seat.wrappers(job_dir)
            else:
                alive = not os.path.exists(os.path.join(job_dir, 'envelope.json'))
            if not alive:
                return print_last(job_dir, handle, state)
        if timeout is not None and time.time() - started >= timeout:
            print(seat.one_line({'status': 'running', 'dir': job_dir, 'turn': state.turn,
                                 'expect': state.expected, 'last_event': state.last_event,
                                 'open_tool_calls': len(state.open_calls)}))
            return 1
        checks += 1
        time.sleep(0.25)


def print_last(job_dir, handle, state):
    """The seat is gone: print its last turn, unless a delivered turn never ended."""
    if handle and state.read().expected and state.ended < state.expected:
        # No wrapper is left to end that turn. Never print the previous turn's answer as its
        # own: the next send closes it as incomplete.
        print(seat.one_line({'status': 'incomplete', 'reason': 'wrapper-gone', 'dir': job_dir,
                             'turn': state.expected, 'session': handle.get('session')}))
        return 1
    return print_result(job_dir)


def print_result(job_dir):
    """A turn's answer (when it is ok), then its envelope line -> the exit code."""
    envelope = seat.read_json(os.path.join(job_dir, 'envelope.json'))
    if envelope is None:
        print(seat.one_line({'status': 'error', 'dir': job_dir, 'reason': 'no envelope'}))
        return 1
    if envelope.get('status') == 'ok':   # an ok envelope names its turn, whose answer is written
        with open(os.path.join(job_dir, 'turn-%s' % envelope['turn'], 'result.txt')) as f:
            print(f.read().rstrip('\n'))
    print(seat.printed(envelope))
    return 0 if envelope.get('status') == 'ok' else 1


def stop(job_dir):
    """Stop a seat -> 0, or 1 when a wrapper outlived the stop.

    Each wrapper gets TERM and closes its CLI as at its deadline. Whatever is left at 10 s of
    the process groups found under the wrappers at the first look gets TERM, and KILL at 15 s.
    Those groups hold the turn in flight; a group a wrapper starts later (an abandoned turn's
    session delete) is never the stop's to end."""
    started = time.time()
    groups = None
    while time.time() - started < 20:
        found = seat.wrappers(job_dir)
        if not found:
            break
        groups = groups or seat.groups_under([row[0] for row in found])
        for row in found:
            try:
                os.kill(row[0], signal.SIGTERM)
            except OSError:
                pass
        elapsed = time.time() - started
        if elapsed >= 10:
            seat.signal_groups(groups, signal.SIGTERM if elapsed < 15 else signal.SIGKILL)
        time.sleep(0.2)
    return 1 if seat.wrappers(job_dir) else 0


# ---------------------------------------------------------------------------------------------
# The events view


def flat(value, limit):
    """A value as one line of text, clipped to `limit` characters."""
    if isinstance(value, str):
        text = ' '.join(value.split())
    else:
        text = json.dumps(value, ensure_ascii=False)
    return wires.clip(text, limit)


class EventView:
    """The readable view of events.jsonl, for `hands.sh events`.

    A run of streamed text is one block, and a run of thoughts one clipped line. Each tool call
    is a line (its input clipped), plus a ↳ line when the call learns its input later. Each
    delivered input, turn start and turn end is a line, and so is each turn's usage with its
    cache-hit rate, each error, each failed tool result and each push the host did not take.
    The rest stays in the raw file. The same events always render the same text, however they
    arrive: a block ends at the next event, never at a pause."""

    def __init__(self):
        self.block = None   # [kind, text] while a run of text or thought arrives
        self.calls = {}     # tool call id -> the input shown for it

    def feed(self, text):
        """Complete JSONL lines -> their rendered text."""
        lines = map(wires.parse_json, text.split('\n'))
        return ''.join(self.render(line) for line in lines if isinstance(line, dict))

    def flush(self):
        """End the block in progress -> its text."""
        kind, text = self.block or (None, '')
        self.block = None
        if not text.strip():
            return ''
        if kind == 'thought':
            return '~ %s\n' % flat(text, 200)
        return text.strip() + '\n'

    def render(self, line):
        kind = line.get('kind')
        call = line.get('call')
        tool_input = line.get('input')
        if kind in ('text', 'thought'):
            if self.block and self.block[0] == kind:
                self.block[1] += line.get('text') or ''
                return ''
            out = self.flush()
            self.block = [kind, line.get('text') or '']
            return out
        out = self.flush()
        if kind == 'tool_call':
            self.calls[call] = tool_input
            shown = ' ' + flat(tool_input, 300) if tool_input else ''
            out += '$ %s%s\n' % (line.get('tool') or '?', shown)
        elif call is not None and call in self.calls:
            if tool_input not in (None, {}, self.calls[call]):   # the call's input, learnt late
                self.calls[call] = tool_input
                out += '  ↳ %s\n' % flat(tool_input, 300)
        at = (line.get('t') or '')[11:19]   # the time of day
        turn = line.get('turn')
        if kind == 'turn_start':
            out += '── turn %s · %s\n' % (turn, at)
        elif kind == 'turn_end':
            reason = ' (%s)' % line['reason'] if line.get('reason') else ''
            out += '── turn %s %s%s · %s\n' % (turn, line.get('status'), reason, at)
        elif kind == 'usage':
            out += usage_line(line)
        elif kind == 'tool_result' and line.get('status') != 'ok':
            output = flat(line.get('output') or '', 200)
            out += '  ! %s %s: %s\n' % (line.get('tool') or call, line.get('status'), output)
        elif kind == 'error':
            out += '!! %s\n' % flat(line.get('text') or '', 300)
        elif kind == 'progress' and line.get('expect'):
            out += '> t%s %s\n' % (line['expect'], line.get('text'))
        elif kind == 'progress' and line.get('push') == 'failed':
            out += '!! %s: %s\n' % (line.get('text'), line.get('error'))
        return out


def usage_line(line):
    """A usage event as its view line: the counts, the cache-hit rate, the cost."""
    usage = line.get('usage') or {}
    fields = ('in', 'out', 'cache_read', 'cache_write', 'reasoning')
    counts = ' '.join('%s=%s' % (k, usage[k]) for k in fields if k in usage) or 'unknown'
    prompt = sum(usage.get(k) or 0 for k in ('in', 'cache_read', 'cache_write'))
    cache = ''
    if prompt:
        cache = ' · cache hit %d%%' % int(100.0 * (usage.get('cache_read') or 0) / prompt + 0.5)
    cost = line.get('cost_usd')
    price = ' · $%.4f' % cost if isinstance(cost, (int, float)) else ''
    return '· usage %s%s%s\n' % (counts, cache, price)


def events(job_dir, follow):
    """Print the view of events.jsonl. With follow, keep printing until the seat has no wrapper
    (looked at before each read) and the file is drained."""
    view = EventView()
    offset = 0
    while True:
        live = follow and seat.wrappers(job_dir)
        text, offset = seat.read_lines(os.path.join(job_dir, 'events.jsonl'), offset)
        sys.stdout.write(view.feed(text) + ('' if live else view.flush()))
        sys.stdout.flush()
        if not live:
            return 0
        time.sleep(0.5)


# ---------------------------------------------------------------------------------------------
# The 30-day sweep of the jobs root


def sweep():
    """Delete the jobs root's idle dirs: those hands.py named, with nothing written in them for
    IDLE_LIMIT and no repository inside. A dir elsewhere (an --out dir) is never swept.

    A dir is judged again under its job.log lock, which a live wrapper holds for its whole life
    (a resume touches its dir before it relaunches). It is renamed to .<name> before the
    delete, so a relaunch that races the delete finds no dir. A .<name> dir is a delete that
    did not finish; the next sweep that finds it stale finishes it."""
    cutoff = time.time() - IDLE_LIMIT
    try:
        names = os.listdir(seat.JOBS)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(seat.JOBS, name)
        unfinished = name.startswith('.')
        own_name = name[1:] if unfinished else name
        if not OWN_NAME.fullmatch(own_name) or os.path.islink(path) or not os.path.isdir(path):
            continue
        if not is_stale(path, cutoff):
            continue
        if unfinished:
            shutil.rmtree(path, ignore_errors=True)
            continue
        try:
            fd = seat.open_regular(os.path.join(path, 'job.log'), os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:   # no wrapper ever ran there
            fd = None
        except OSError:
            continue
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if is_stale(path, cutoff):
                hidden = os.path.join(seat.JOBS, '.' + name)
                os.rename(path, hidden)
                shutil.rmtree(hidden, ignore_errors=True)
        except OSError:   # a wrapper holds the lock: the seat is live
            pass
        finally:
            if fd is not None:
                os.close(fd)


def is_stale(path, cutoff):
    """Whether nothing under path (path included; a link by its own time) was written at or
    after cutoff, with no .git inside and no other filesystem mounted inside. A part that
    cannot be read is not stale."""
    def fail(error):
        raise error

    try:
        top = os.lstat(path)
        if top.st_mtime >= cutoff:
            return False
        for here, dirs, files in os.walk(path, onerror=fail):
            for name in dirs + files:
                info = os.lstat(os.path.join(here, name))
                if name == '.git' or info.st_mtime >= cutoff or info.st_dev != top.st_dev:
                    return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# The probe, and the command line


def probe(which):
    """A one-line ping per provider, on its smallest model: it proves the CLI and its login.

    Claude's ping also refreshes the quota board (its stream's rate_limit_event is the claude
    reading). A probe never pushes to the driver."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE_CODE_MESSAGING_')}
    providers = list(wires.PROVIDERS) if which == 'all' else which.split()
    sweep()
    rc = 0
    with tempfile.NamedTemporaryFile('w', prefix='hands-probe.', delete=False) as ping:
        ping.write('Reply with exactly: OK')
    for provider in providers:
        if not cli_path(provider):
            print(probe_line('not-installed', provider, 0))
            continue
        spec, effort = PROBE_SPECS.get(provider, (provider, ''))
        job_dir = os.path.join(seat.JOBS, 'probe-%d-%s' % (os.getpid(), provider))
        shutil.rmtree(job_dir, ignore_errors=True)
        # a ping runs in a neutral cwd: it has no business reading your repo
        argv = [sys.executable, os.path.abspath(__file__), '--timeout', '240', '--out', job_dir,
                '--cwd', tempfile.gettempdir()]
        if effort:
            argv += ['--effort', effort]
        done = subprocess.call(argv + [spec, ping.name], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        if done == 0:
            print(json.dumps({'status': 'ok', 'provider': provider}, separators=(',', ':')))
            continue
        rc = 1
        try:
            with open(os.path.join(job_dir, 'envelope.json')) as f:
                sys.stdout.write(f.read())
        except OSError:
            print(probe_line('error', provider, 1))
        sys.stdout.flush()
    os.unlink(ping.name)
    return rc


def probe_line(status, provider, rc):
    """A probe's envelope line when there is no seat envelope to print."""
    return json.dumps({'status': status, 'provider': provider, 'model': None, 'rc': rc,
                       'seconds': 0, 'reset': None, 'dir': None, 'usage': None},
                      separators=(',', ':'))


def main(args):
    if args[:1] == ['quota']:
        return quota.main(args[1:])
    if args[:1] == ['send']:
        if len(args) != 3:
            say('usage: hands.sh send DIR FILE')
            return 2
        return send(args[1], args[2])
    if args[:1] in (['mail'], ['wait'], ['events'], ['stop']):
        return run_verb(args)
    options = Options()
    if args[:1] == ['start']:
        options.start = True
        args = args[1:]
    while args:
        flag = args[0]
        if flag in ('--cwd', '--timeout', '--out', '--effort'):
            if len(args) < 2:
                say('%s needs a value' % flag)
                return 2
            setattr(options, flag[2:], args[1])
            args = args[1:]
        elif flag == '--probe':
            if len(args) > 1 and not args[1].startswith('-'):
                options.probe = args[1]
                args = args[1:]
            else:
                options.probe = 'all'
        elif flag in ('-h', '--help'):
            print(__doc__.rstrip())
            return 0
        elif flag == '--':
            args = args[1:]
            break
        elif flag.startswith('-'):
            say('unknown flag %s' % flag)
            return 2
        else:
            break
        args = args[1:]
    if options.probe:
        return probe(options.probe)
    if len(args) >= 2:
        return buy(args[0], args[1], options)
    print(__doc__.rstrip())
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
