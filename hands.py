#!/usr/bin/env python3
"""hands — run a task on another provider's coding-agent CLI (claude, codex, grok, opencode) as a subagent seat.
Each CLI keeps its own login; hands stores no credentials (a job dir keeps the host's push socket path and
token while you use it: README § Security model).

  hands.sh start [opts] provider[:model] prompt-file   buy a seat; returns at once: {"status":"spawned","dir":DIR,...}
  hands.sh [opts] provider[:model] prompt-file         sync: start + wait + stop
  hands.sh send DIR FILE        the next turn (a seat whose process is gone relaunches on its session)
  hands.sh mail DIR TEXT        a driver line the seat's live wrapper delivers
  hands.sh wait DIR [--for turn|mail|exit] [--timeout S]   blocks until the turn ends: the answer, then the envelope
  hands.sh events DIR [--follow]   a readable view of DIR/events.jsonl
  hands.sh stop DIR             close the seat as its deadline does (stdin EOF, TERM, KILL); the envelope still lands
  hands.sh --probe [claude|codex|grok|opencode|all]   a 1-token aliveness ping per provider (spends a little quota)
  hands.sh quota [--json] [provider ...]   the quota board (quota.py): read-only, it never spends

opts (all before the provider spec; the prompt is always a file):
  --cwd DIR           where the seat works (default: the current dir). A seat has full read and write access,
                      as you: point it at a worktree or a scratch dir
  --out DIR           the job dir, new or empty, made 0700 (default: a fresh dir in $XDG_STATE_HOME/hands, else
                      ~/.local/state/hands, where every buy sweeps dirs idle for 30 days)
  --timeout SECS      a wall clock per launch (default: none; the seat runs until its work ends or a stop)
  --effort <tier>     claude, codex: low|medium|high|xhigh|max, passed through (codex adds ultra where it lists it);
                      grok, opencode: required, a value the model lists (grok-4.6: low|medium|high|xhigh)

envelope: {"status":"ok|error|incomplete|cancelled|truncated|timeout|auth-expired|provider-quota-dead|not-installed|refused",
  "provider","model","effort","rc","seconds","reset","dir","usage","session","turn","delivery",...}
  exit 0 ok, 1 any other status, 2 refused or a usage error. README.md is the manual;
  CONTRIBUTING.md has the engine's rules.
"""
import fcntl, json, math, os, re, shutil, signal, subprocess, sys, tempfile, time

import quota
import seat
import wires

SEAT_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seat.py')
KEEP = 30 * 86400   # how long a dir in the jobs root with no live wrapper outlives its last write
NAMED = re.compile(r'\d{8}-\d{6}-[a-z]+-\d+|probe-\d+-[a-z]+')   # the names hands.py gives there (buy, probe)

# The seat contract. It heads every buy's prompt (a probe's ping too) and names no job dir, so
# every seat's prompt shares one prefix through the task: the provider's prompt cache can reuse it.
# The one line that differs per seat, seat_line(), comes after the task.
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


def seat_line(job_dir):
    return 'YOUR JOB DIR: %s (your outbox: %s/outbox.jsonl).\n' % (job_dir, job_dir)


class Options:
    """The flags before the provider spec."""
    def __init__(self):
        self.cwd, self.out, self.timeout, self.effort, self.probe = os.getcwd(), '', '', '', ''
        self.start = False   # `start`: return once the seat is spawned


def say(message):
    print('hands: ' + message, file=sys.stderr)


def cli_path(provider):
    """The provider's CLI, also where opencode's installer puts it when it is not on PATH."""
    found = shutil.which(provider)
    fallback = os.path.expanduser('~/.opencode/bin/opencode')
    if not found and provider == 'opencode' and os.access(fallback, os.X_OK):
        return fallback
    return found


def empty(path):
    try:
        return not os.listdir(path)
    except OSError:   # not a dir, or unreadable: never written into
        return False


def emit(job_dir, status, provider, model, rc, options, reset='', reason=''):
    """Write DIR/envelope.json (seat.py's one envelope writer) and print it."""
    seat.envelope(job_dir, status, provider, model, rc, 0, reset, None, options.effort,
                  reason=reason or None)
    with open(os.path.join(job_dir, 'envelope.json')) as f:
        sys.stdout.write(f.read())


def buy(spec, prompt_file, options):
    """Make the job dir and its prompt, then launch the seat."""
    provider, _, model = spec.partition(':')
    # the provider names the default job dir, so only a plain lowercase name ever reaches a path
    if not re.fullmatch(r'[a-z0-9]+', provider):
        say("bad provider '%s' (claude|codex|grok|opencode)" % provider)
        return 2
    sweep()   # the 30-day cleanup of the jobs root, at every buy
    if options.out:   # a new or empty dir: a buy never writes over files it did not make
        job_dir = options.out
        if os.path.lexists(job_dir) and not empty(job_dir):
            say('--out %s is not empty — name a new or empty dir, or omit --out for a fresh one in '
                'the jobs root' % job_dir)
            return 2
        try:   # its parents keep your umask; the dir itself is 0700, an existing one too
            os.makedirs(job_dir, exist_ok=True)
            os.chmod(job_dir, 0o700)
        except OSError:
            pass
    else:
        job_dir = os.path.join(seat.JOBS, '%s-%s-%d' % (time.strftime('%Y%m%d-%H%M%S'), provider,
                                                         os.getpid()))
        old_umask = os.umask(0o077)   # prompts live here
        try:
            os.makedirs(job_dir, exist_ok=True)
        except OSError:
            pass
        os.umask(old_umask)
    if not (os.path.isdir(job_dir) and os.access(job_dir, os.W_OK | os.X_OK)):
        say('cannot create the job dir %s' % (options.out or 'in ' + seat.JOBS))
        return 2
    job_dir = os.path.realpath(job_dir)   # one canonical name: the prompt, the handle, the wrapper
    if not os.path.isfile(prompt_file):
        say('prompt file not found: %s' % prompt_file)
        if prompt_file.startswith('-'):
            say("'%s' is a FLAG in the prompt-file position — options must PRECEDE the "
                "provider positional (hands.sh [opts] provider[:model] prompt-file); flags after "
                "it are read as the prompt file" % prompt_file)
        emit(job_dir, 'error', provider, model, 2, options)
        return 2
    # prompt.txt is the one file every provider reads: the seat box, a blank line, the task, a
    # blank line, the job-dir line. The outbox it names and the inbox exist before the seat starts.
    with open(prompt_file, 'rb') as f:
        task = f.read()
    if task and not task.endswith(b'\n'):
        task += b'\n'
    with open(os.path.join(job_dir, 'prompt.txt'), 'wb') as f:
        f.write(SEAT_BOX.encode() + b'\n' + task + b'\n' + seat_line(job_dir).encode())
    for name in ('inbox.jsonl', 'outbox.jsonl'):
        open(os.path.join(job_dir, name), 'a').close()
    if provider not in wires.PROVIDERS:   # a typo, or a provider hands has no wire for (gemini)
        say("unknown provider '%s' (claude|codex|grok|opencode)" % provider)
        emit(job_dir, 'error', provider, model, 2, options)
        return 2
    return launch(provider, model, job_dir, options)


def launch(provider, model, job_dir, options):
    """Write the seat's handle, DIR/session, and spawn its wrapper. A sync buy then waits and stops.

    The handle is what every later send resumes with: provider, model, effort, the one canonical
    cwd, timeout, the CLI path, and the session id seat.py records at first sight."""
    cli = cli_path(provider)
    if not cli:
        emit(job_dir, 'not-installed', provider, model, 0, options)
        return 1
    if not os.path.isdir(options.cwd):
        say('--cwd %s unresolvable' % options.cwd)
        emit(job_dir, 'error', provider, model, 2, options)
        return 2
    timeout = seconds(options.timeout)
    if timeout is False:
        say('--timeout takes seconds')
        emit(job_dir, 'error', provider, model, 2, options)
        return 2
    handle = {'provider': provider, 'model': model or None, 'effort': options.effort or None,
              'cwd': os.path.realpath(options.cwd), 'timeout': timeout, 'cli': cli, 'session': None}
    with open(os.path.join(job_dir, 'session'), 'w') as f:
        f.write(json.dumps(handle, separators=(',', ':')) + '\n')
    prompt = os.path.join(job_dir, 'prompt.txt')
    if options.start:
        return spawn(job_dir, prompt)
    # A sync buy that is interrupted stops its seat first. A signal the caller ignores (nohup's
    # HUP) stays ignored.
    def interrupted(signum, frame):
        sys.stdout.flush()
        stop(job_dir)
        sys.exit(128 + signum)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(signum) != signal.SIG_IGN:
            signal.signal(signum, interrupted)
    ack = subprocess.run([sys.executable, SEAT_PY, 'spawn', job_dir, prompt],
                         stdout=subprocess.PIPE, text=True)
    if ack.returncode == 2:   # refused before any prompt: that envelope is the answer
        sys.stdout.write(ack.stdout)
        return 2
    rc = wait(job_dir, seat.jload(os.path.join(job_dir, 'session')), 'turn', None)
    sys.stdout.flush()
    stop(job_dir)
    return rc


def seconds(text):
    """--timeout's value: None when unset, a number, or False when it is not one."""
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
    # A subprocess, never in-process: the wrapper it forks is found by its argv, seat.py spawn DIR
    return subprocess.call([sys.executable, SEAT_PY, 'spawn', job_dir, message_file])


def send(job_dir, message_file):
    """The next turn: a live seat takes it now; a seat whose process is gone relaunches on its
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
    handle = seat.jload(os.path.join(job_dir, 'session'))
    options = Options()
    options.effort = handle.get('effort') or ''
    if not handle.get('session'):
        say('%s recorded no session (its seat never reached the model) — start a fresh seat'
            % job_dir)
        emit(job_dir, 'refused', handle['provider'], handle.get('model') or '', 2, options,
             reason='no-session')
        return 2
    return spawn(job_dir, message_file)


def report(d):   # the sync buy's contract: the answer (on ok), then the envelope line
    env = seat.jload(os.path.join(d, 'envelope.json'))
    if env is None:
        print(seat.dump({'status': 'error', 'dir': d, 'reason': 'no envelope'}))
        return 1
    if env.get('status') == 'ok':   # an ok envelope always names its turn, whose result.txt landed first
        with open(os.path.join(d, 'turn-%s' % env['turn'], 'result.txt')) as f:
            print(f.read().rstrip('\n'))
    print(seat.shown(env))
    return 0 if env.get('status') == 'ok' else 1


def wait(d, h, mode, timeout):
    t0, st, box, i = time.time(), seat.State(d), os.path.join(d, 'outbox.jsonl'), 0
    m0 = seat.size(box)
    while True:
        st.read()
        if mode == 'mail' and seat.size(box) > m0:
            sys.stdout.write(seat.tail(box, m0)[0])
            return 0
        if h and mode != 'exit' and st.expect and st.end >= st.expect:   # an idle seat mails nothing more
            return report(d)
        if i % 4 == 0 and not (seat.wrappers(d) if h else not os.path.exists(os.path.join(d, 'envelope.json'))):
            if h and st.read().expect and st.end < st.expect:   # a sent turn never ended, and no wrapper is left to end it:
                # never the previous turn's answer as this one's (the next send relaunches and closes it incomplete)
                print(seat.dump({'status': 'incomplete', 'reason': 'wrapper-gone', 'dir': d, 'turn': st.expect,
                            'session': h.get('session')}))
                return 1
            return report(d)
        if timeout is not None and time.time() - t0 >= timeout:
            print(seat.dump({'status': 'running', 'dir': d, 'turn': st.turn, 'expect': st.expect, 'last_event': st.t,
                              'open_tool_calls': len(st.open)}))
            return 1
        i += 1
        time.sleep(0.25)


def stop(d):   # TERM the seat's wrappers: each closes its CLI as at its deadline (Seat.cut types what that ends). What
    # is left at 10 s of the seat's groups -- those under its wrappers when the stop first finds any (none yet: its CLI is
    # still launching) -- is TERMed, and KILLed at 15 s. A group found then holds the turn in flight, so a group a wrapper
    # starts later (its close-out's own: an abandoned turn's session delete) is never the stop's
    t0, groups = time.time(), None
    while time.time() - t0 < 20:
        ws = seat.wrappers(d)
        if not ws:
            break
        groups = groups or seat.seat_groups([w[0] for w in ws])
        for w in ws:
            try:
                os.kill(w[0], signal.SIGTERM)
            except OSError:
                pass
        if time.time() - t0 >= 10:
            seat.term(groups, signal.SIGTERM if time.time() - t0 < 15 else signal.SIGKILL)
        time.sleep(0.2)
    return 1 if seat.wrappers(d) else 0


def one(v, n):   # a value as ONE line of at most n chars (+ the clip mark)
    return wires.clip(' '.join(v.split()) if isinstance(v, str) else json.dumps(v, ensure_ascii=False), n)


class View:
    """THE readable view of events.jsonl (`events DIR [--follow]`; the raw stream is `tail -f DIR/events.jsonl`).
    Streamed text merges into one block per run, a thought run into one clipped line; one line per tool call (input
    clipped), plus one ↳ line when the call learns its input later; a line per delivered input, turn_start and
    turn_end (status, reason); a usage line per turn with its cache-hit %; errors, failed tool results, a push the host
    did not take. Everything else (heartbeats, hooks, file events, exits) stays raw-only. Pure: the same events render
    the same bytes however they arrive -- a block closes on the next event, never on a pause."""
    def __init__(v):
        v.block, v.calls = None, {}   # block = [kind, text] while text/thought deltas arrive; calls: call -> input shown

    def feed(v, text):   # complete JSONL lines (LF-split) -> rendered text
        return ''.join(v.event(e) for e in map(wires.jparse, text.split('\n')) if isinstance(e, dict))

    def close(v):
        k, s = v.block or (None, '')
        v.block = None
        return ('~ %s\n' % one(s, 200) if k == 'thought' else s.strip() + '\n') if s.strip() else ''

    def event(v, e):
        k, n, call, inp, at = e.get('kind'), e.get('turn'), e.get('call'), e.get('input'), (e.get('t') or '')[11:19]
        if k in ('text', 'thought'):
            if v.block and v.block[0] == k:
                v.block[1] += e.get('text') or ''
                return ''
            out, v.block = v.close(), [k, e.get('text') or '']
            return out
        out = v.close()
        if k == 'tool_call':
            v.calls[call] = inp
            out += '$ %s%s\n' % (e.get('tool') or '?', ' ' + one(inp, 300) if inp else '')
        elif call is not None and call in v.calls and inp not in (None, {}, v.calls[call]):   # the call's input, learnt late
            v.calls[call] = inp
            out += '  ↳ %s\n' % one(inp, 300)
        if k == 'turn_start':
            out += '── turn %s · %s\n' % (n, at)
        elif k == 'turn_end':
            out += '── turn %s %s%s · %s\n' % (n, e.get('status'), ' (%s)' % e['reason'] if e.get('reason') else '', at)
        elif k == 'usage':
            u = e.get('usage') or {}
            tot = sum(u.get(x) or 0 for x in ('in', 'cache_read', 'cache_write'))
            out += '· usage %s%s%s\n' % (' '.join('%s=%s' % (x, u[x]) for x in ('in', 'out', 'cache_read', 'cache_write', 'reasoning') if x in u) or 'unknown',
                                         ' · cache hit %d%%' % int(100.0 * (u.get('cache_read') or 0) / tot + 0.5) if tot else '',
                                         ' · $%.4f' % e['cost_usd'] if isinstance(e.get('cost_usd'), (int, float)) else '')
        elif k == 'tool_result' and e.get('status') != 'ok':
            out += '  ! %s %s: %s\n' % (e.get('tool') or call, e.get('status'), one(e.get('output') or '', 200))
        elif k == 'error':
            out += '!! %s\n' % one(e.get('text') or '', 300)
        elif k == 'progress' and e.get('expect'):
            out += '> t%s %s\n' % (e['expect'], e.get('text'))
        elif k == 'progress' and e.get('push') == 'failed':
            out += '!! %s: %s\n' % (e.get('text'), e.get('error'))
        return out


def events(d, follow):   # THE view, one-shot or followed; --follow ends with the seat: no wrapper alive (sampled
    v, off = View(), 0   # BEFORE the read) and the file drained
    while True:
        live = follow and seat.wrappers(d)
        text, off = seat.tail(os.path.join(d, 'events.jsonl'), off)
        sys.stdout.write(v.feed(text) + ('' if live else v.close()))
        sys.stdout.flush()
        if not live:
            return 0
        time.sleep(0.5)


def sweep():   # THE RETENTION, at every buy and probe, before it makes its dir in the jobs root (seat.JOBS): a dir
    # directly in the jobs root that hands.py named (an --out dir outside it is never swept), with nothing in it written
    # within KEEP and no repository in it, goes (a link there never, nor its target: job.log is never made) --
    # under its job.log lock, the lock a wrapper holds for its whole life (no job.log: no wrapper ever ran there, a buy
    # refused before its launch, with no session to resume). Under the lock it is judged again (a resume touches its dir
    # before it relaunches: send) and renamed out of every address, to .<name>, before the delete, so a relaunch racing
    # it finds no dir. A .<name> dir is a delete begun and not finished (killed, or stopped part way): it has no address
    # and was judged under its lock, so the next sweep that finds it stale finishes it in place
    cut = time.time() - KEEP
    try:
        names = os.listdir(seat.JOBS)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(seat.JOBS, n)
        m = n[1:] if n[:1] == '.' else n   # .<name>: a delete begun and not finished
        if not NAMED.fullmatch(m) or os.path.islink(p) or not os.path.isdir(p) or not stale(p, cut):
            continue
        if n[:1] == '.':
            shutil.rmtree(p, ignore_errors=True)
            continue
        try:
            fd = seat.opened(os.path.join(p, 'job.log'), os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:
            fd = None
        except OSError:
            continue
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if stale(p, cut):
                os.rename(p, os.path.join(seat.JOBS, '.' + n))
                shutil.rmtree(os.path.join(seat.JOBS, '.' + n), ignore_errors=True)
        except OSError:   # a wrapper holds it: the seat is live
            pass
        finally:
            if fd is not None:
                os.close(fd)


def stale(p, cut):   # nothing under p (p included, by lstat: a link's own time) written at or after cut, no .git in it
    # (a repository), and no other filesystem mounted in it (never ours to delete); an unreadable part is not stale
    def fail(e):
        raise e
    try:
        top = os.lstat(p)
        if top.st_mtime >= cut:
            return False
        for here, dirs, files in os.walk(p, onerror=fail):
            for n in dirs + files:
                q = os.lstat(os.path.join(here, n))
                if n == '.git' or q.st_mtime >= cut or q.st_dev != top.st_dev:
                    return False
    except OSError:
        return False
    return True


def verb(a):
    """mail, wait, events and stop: DIR first, as seat.py wrote it."""
    cmd = a[0]
    if len(a) < 2 or not os.path.isdir(a[1]):
        say('usage: hands.sh %s DIR ...' % cmd)
        return 2
    d = os.path.realpath(a[1])
    h = seat.jload(os.path.join(d, 'session'))
    if cmd == 'mail':   # the driver's line; the seat's live wrapper delivers it
        if len(a) < 3:
            return 2
        try:   # opened like any engine append: never through a link the seat planted
            seat.append(os.path.join(d, 'inbox.jsonl'), {'t': wires.now(), 'from': 'driver', 'text': ' '.join(a[2:])})
        except OSError as e:
            say('%s/inbox.jsonl: %s -- not written' % (d, e.strerror or e))
            return 2
        if h and not seat.wrappers(d):
            say('%s has no live seat: the mail waits in its inbox until that seat runs again (send relaunches it)' % d)
            return 1
        return 0
    if cmd == 'wait':
        mode = a[a.index('--for') + 1] if '--for' in a[:-1] else 'turn'
        tmo = float(a[a.index('--timeout') + 1]) if '--timeout' in a[:-1] else None
        return wait(d, h, mode, tmo) if mode in ('turn', 'mail', 'exit') else 2
    if cmd == 'events':
        return events(d, '--follow' in a)
    rc, env = stop(d), seat.jload(os.path.join(d, 'envelope.json'))
    if env:
        print(seat.shown(env))
    return rc


def probe(which):
    """A one-line ping per provider, on its smallest model: it proves the CLI and its login.

    Claude's ping also refreshes the quota board: its stream's rate_limit_event is the claude
    reading. A probe never pushes into the driver's session."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE_CODE_MESSAGING_')}
    providers = list(wires.PROVIDERS) if which == 'all' else which.split()
    sweep()
    rc = 0
    with tempfile.NamedTemporaryFile('w', prefix='hands-probe.', delete=False) as ping:
        ping.write('Reply with exactly: OK')
    for provider in providers:
        if not cli_path(provider):
            print(json.dumps({'status': 'not-installed', 'provider': provider, 'model': None,
                              'rc': 0, 'seconds': 0, 'reset': None, 'dir': None,
                              'usage': None}, separators=(',', ':')))
            continue
        # opencode 2.x lists no working free model: its cheapest Go model. An ACP seat (grok,
        # opencode) always names an effort its model lists.
        spec, effort = {'claude': ('claude:haiku', ''), 'grok': ('grok', 'low'),
                        'opencode': ('opencode:opencode-go/deepseek-v4.1-flash', 'low')
                        }.get(provider, (provider, ''))
        job_dir = os.path.join(seat.JOBS, 'probe-%d-%s' % (os.getpid(), provider))
        shutil.rmtree(job_dir, ignore_errors=True)
        # a ping runs in a neutral cwd: it has no business reading the caller's repo
        argv = [sys.executable, os.path.abspath(__file__), '--timeout', '240', '--out', job_dir,
                '--cwd', tempfile.gettempdir()]
        argv += ['--effort', effort] if effort else []
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
            print(json.dumps({'status': 'error', 'provider': provider, 'model': None, 'rc': 1,
                              'seconds': 0, 'reset': None, 'dir': None, 'usage': None},
                             separators=(',', ':')))
        sys.stdout.flush()
    os.unlink(ping.name)
    return rc


def main(args):
    if args[:1] == ['quota']:
        return quota.main(args[1:])
    if args[:1] == ['send']:
        if len(args) != 3:
            say('usage: hands.sh send DIR FILE')
            return 2
        return send(args[1], args[2])
    if args[:1] in (['mail'], ['wait'], ['events'], ['stop']):
        return verb(args)
    options = Options()
    if args[:1] == ['start']:
        options.start, args = True, args[1:]
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
                options.probe, args = args[1], args[1:]
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
