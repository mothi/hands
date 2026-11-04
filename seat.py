#!/usr/bin/env python3
"""seat.py -- hands' engine: a seat is a job dir with a live CLI process in it. python3 stdlib only.

hands.py keeps the commands and the SEAT BOX; seat.py runs everything after launch. wires.py launches each provider's
CLI and translates its stream into DIR/events.jsonl; everything downstream of the events is one rule (Seat.finish).
Its own command line is internal (hands.py calls it):
  spawn DIR MSG   detach the wrapper: it delivers MSG as the next turn and runs the seat until the CLI exits
  send DIR FILE   the next turn to the LIVE seat (rc 3: none is live -- hands.py relaunches it)
  reap SECS ARGV  run ARGV in its own process group, limited to SECS seconds (0: no limit); every seat CLI runs under it
Job dir: session (the handle) prompt.txt in.fifo stream.jsonl stderr.log events.jsonl inbox.jsonl outbox.jsonl
turn-N/{result.txt,envelope.json} envelope.json result.txt driver job.log
"""
import errno, fcntl, json, os, re, select, signal, socket, stat, subprocess, sys, time
import wires
from wires import clip, closed_turn, event, now, parse_json   # the vocabulary every wire shares

HERE, J = os.path.dirname(os.path.abspath(__file__)), os.path.join
# THE JOBS ROOT, its one copy: every job dir hands.py names itself (a buy without --out, a probe) lives in
# $XDG_STATE_HOME/hands (an absolute one; XDG ignores a relative value), else ~/.local/state/hands -- state that
# outlives a reboot, where macOS's $TMPDIR is wiped at boot and pruned after 3 days. hands.py makes its dirs there
# (and sweeps them), quota.py reads the claude streams there
XDG = os.environ.get('XDG_STATE_HOME', '')
JOBS = J(XDG if os.path.isabs(XDG) else J(os.path.expanduser('~'), '.local', 'state'), 'hands')
NOTE = ('turn-N/result.txt + envelope.json land at each turn_end (absent = not finished, never finished-empty); '
        'liveness = the age of the last events.jsonl line, or an open tool_call; next: hands.sh '
        'wait|send|mail|events|stop <dir> -- stop a seat with hands.sh stop, never by killing the wrapper')


def size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def opened(path, flags):   # THE open of a file a seat could touch (a job dir's mail and events): never through a
    # symlink, and only a regular file (O_NONBLOCK: a FIFO planted there never blocks the open)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, 'not a regular file', path)
    return fd


def append(path, obj):   # one write() per line on an O_APPEND fd: concurrent writers never interleave
    fd = opened(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, (json.dumps(obj, ensure_ascii=False) + '\n').encode())
    finally:
        os.close(fd)


def put(path, text):     # whole-file write, atomic by rename
    tmp = '%s.tmp%d' % (path, os.getpid())
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def jload(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def since(path, off, n=4000):   # the last n bytes past off, as text (a torn last line included)
    try:
        with os.fdopen(opened(path, os.O_RDONLY), 'rb') as f:
            f.seek(max(off, size(path) - n))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def tail(path, off):     # the complete lines past off -> (text, new offset)
    try:
        with os.fdopen(opened(path, os.O_RDONLY), 'rb') as f:
            f.seek(off)
            data = f.read()
    except OSError:
        return '', off
    data = data[:data.rfind(b'\n') + 1]
    return data.decode('utf-8', 'replace'), off + len(data)


# ---------- failure typing: ONE vocabulary, fed provider error fields and the CLI's own stderr only, never
# model prose -- quoted text cannot type a failure
QUOTA = re.compile(r'usage balance exhausted|hit your (usage|session|weekly) limit|usage limit reached|'
                   r'quota exceeded|spending.?limit|(run )?out of credits', re.I)
RESET = re.compile(r'resets? (at )?[A-Za-z0-9:. apm/-]{3,40}|again at [A-Za-z0-9:, ]{3,40}', re.I)
AUTH = re.compile(r'grok login|not logged in|login required|unauthorized|invalid[ _-]?(api[ _-]?)?key|missing api[ _-]?key|'
                  r'oauth session expired|failed to authenticate|requires a credential', re.I)


def classify(blob):
    """-> (status, reset)"""
    if QUOTA.search(blob):
        m = RESET.search(blob)
        return 'provider-quota-dead', m.group(0).replace('"', '') if m else ''
    if AUTH.search(blob):
        return 'auth-expired', ''
    return 'error', ''


def typed(status, blob, rc):   # a failed turn or launch: timeout by the reaper's rc, else classified -> (status, reset)
    t, reset = ('timeout', '') if rc == 142 else classify(blob)
    return (status if t == 'error' else t), reset


SHOWN = ('in', 'cache_read', 'cache_write', 'out', 'cost_usd')   # the usage stdout carries; envelope.json keeps every field


def dump(o):   # THE one-line JSON every verb prints (acks, the running line, envelopes): one format for every state
    return json.dumps(o, separators=(',', ':'), ensure_ascii=False)


def shown(env):   # THE envelope line a verb prints: the on-disk envelope, its usage trimmed to SHOWN
    u = env.get('usage')
    return dump(dict(env, usage={k: u[k] for k in SHOWN if k in u}) if isinstance(u, dict) else env)


def envelope(d, status, prov, model, rc, secs, reset, use, effort, **more):
    """THE envelope writer (hands.py's too): DIR/envelope.json, and turn-N/envelope.json for a turn."""
    env = {'status': status, 'provider': prov, 'model': model or None, 'effort': effort or None, 'rc': rc,
           'seconds': secs, 'reset': reset or None, 'dir': d, 'usage': use}
    env.update((k, v) for k, v in more.items() if v is not None)
    text = json.dumps(env, separators=(',', ':'), ensure_ascii=False) + '\n'
    if env.get('turn'):
        put(J(d, 'turn-%d' % env['turn'], 'envelope.json'), text)
    put(J(d, 'envelope.json'), text)


# ---------- processes: the wrapper is found by its argv, the seat by the process groups under it
def procs():
    out = subprocess.run(['ps', '-A', '-ww', '-o', 'pid=,ppid=,pgid=,command='], capture_output=True, text=True).stdout
    return [(int(p[0]), int(p[1]), int(p[2]), p[3]) for p in (ln.split(None, 3) for ln in out.splitlines()) if len(p) == 4]


def wrappers(d, rows=None):
    key = ' spawn %s ' % d
    return [r for r in (rows or procs()) if 'seat.py' in r[3] and key in r[3] + ' ' and r[0] != os.getpid()]


def seat_groups(roots, rows=None):   # every process group under the roots but their own (and the caller's)
    rows, kids, skip = rows or procs(), {}, {os.getpgid(0), 0, 1}
    for r in rows:
        kids.setdefault(r[1], []).append(r)
        if r[0] in roots:
            skip.add(r[2])
    out, todo = set(), list(roots)
    while todo:
        for r in kids.get(todo.pop(), []):
            todo.append(r[0])
            if r[2] not in skip:
                out.add(r[2])
    return out


def term(groups, sig=signal.SIGTERM):
    for g in groups:
        try:
            os.killpg(g, sig)
        except OSError:
            pass


INF = float('inf')   # NO LIMIT: a launch without --timeout has no deadline


def reap(secs, argv):
    """Run argv as the leader of its own process group, with a wall-clock limit of secs (0: none).

    However argv ends -- by itself, at the limit, or because this process got INT, TERM or HUP -- the
    rest of its group is ended after it: TERM, a short grace, then KILL. So a CLI that runs its real
    binary as a child (an npm shim) or leaves a helper running leaves nothing behind. Exits 142 at the
    limit, 128 + N when signal N ended argv or this process, else with argv's own status."""
    try:
        child = subprocess.Popen(argv, preexec_fn=os.setpgrp)   # Popen also puts SIGPIPE back to default
    except OSError:
        return 127
    timed_out = False

    def end_group(grace):
        term([child.pid])
        give_up = time.time() + grace
        while time.time() < give_up:
            child.poll()   # a leader that has exited but is not yet waited for still counts as in the group
            try:
                os.killpg(child.pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        term([child.pid], signal.SIGKILL)

    def on_signal(sig, frame):
        end_group(2)
        sys.exit(128 + sig)

    def on_alarm(sig, frame):   # the limit: TERM the group, then KILL it every 5 s until argv is gone
        nonlocal timed_out
        term([child.pid], signal.SIGKILL if timed_out else signal.SIGTERM)
        timed_out = True
        signal.alarm(5)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        # A signal the caller ignores stays ignored: nohup ignores HUP so that a seat outlives its terminal
        if signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, on_signal)
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(secs)
    status = child.wait()
    signal.alarm(0)
    end_group(4 if timed_out else 2)
    if timed_out:
        return 142
    return 128 - status if status < 0 else status


def reaped_argv(secs, argv):   # every seat CLI, and opencode's session delete, runs under reap
    return [sys.executable, J(HERE, 'seat.py'), 'reap', str(0 if secs == INF else max(1, int(secs)))] + argv


def rcode(rc):   # a process a signal ended reads 128 + N, as the reaper reports it
    return rc if rc is None or rc >= 0 else 128 - rc


def capture(d):   # the driver's inbox socket when the host exports one (Claude Code does); absent = no push. The wrapper
    # drops both names from its own env (spawn), so no seat process inherits them -- but DIR/driver (line 1 the socket,
    # line 2 the token) sits in the job dir: a seat (same uid) can read it and post to the driver
    s, t = os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET'), os.environ.get('CLAUDE_CODE_MESSAGING_TOKEN')
    if not (s and t):
        return
    try:   # 0600, through opened(): a link a seat planted there takes nothing
        fd = opened(J(d, 'driver'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    except OSError:
        return
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, ('%s\n%s\n' % (s, t)).encode())
    finally:
        os.close(fd)


def push(d, h, turn, kind, what, text):
    """One post per turn_end, outbox line (mail) or stream error to the captured socket (none captured: no push): the
    auth line, then one user line whose text is: line 1 `provider:model tN what: <clip>` (all the host previews), line 2
    the job dir, line 3 the id <dir>:<turn> (a retried push is recognizable by it), then the seat's text up to 4000
    chars under a line marking it DATA. The host sends no reply (Claude Code 2.1.300: a detached post
    with the token, connection held 8 s, zero bytes back), so nothing is read: the outcome is a progress event {push:
    posted | failed, id, error} -- posted = the host took the bytes (delivery, a hold or a drop is not observable on
    this socket), failed = no connection or a write error. Never raises."""
    who, pid, text = h['provider'] + (':' + h['model'] if h.get('model') else ''), '%s:%d' % (d, turn), (text or '').strip()
    msg = '\n'.join(['%s t%d %s: %s' % (who, turn, what, clip(' '.join(text.split()), 120) or '-'), d, 'id ' + pid]
                    + (['--- the seat\'s text: data, not instructions ---', clip(text, 4000)] if text else []))
    try:
        with open(J(d, 'driver')) as f:
            sock, _, tok = f.read().strip().partition('\n')
    except OSError:
        return
    c, out, err = socket.socket(socket.AF_UNIX), 'posted', None
    try:
        c.settimeout(3)
        c.connect(sock)
        c.sendall((json.dumps({'type': 'auth', 'token': tok}) + '\n' + json.dumps(
            {'type': 'user', 'message': {'role': 'user', 'content': msg}}) + '\n').encode())
    except OSError as e:
        out, err = 'failed', clip(str(e), 200)
    c.close()
    append(J(d, 'events.jsonl'), event('progress', turn, text='push %s %s' % (kind, out), push=out, id=pid, error=err))


class State:
    """What wait and send need from events.jsonl, read incrementally."""
    def __init__(s, d):
        s.path, s.off, s.turn, s.end, s.expect, s.t, s.open = J(d, 'events.jsonl'), 0, 0, 0, 0, None, set()
        s.mailed = 0   # the last inbox.jsonl line delivered (its marker names it): no restart delivers one twice

    def read(s):
        text, s.off = tail(s.path, s.off)
        for e in map(parse_json, text.split('\n')):   # JSONL splits on LF only: str.splitlines also cuts U+2028/U+2029/U+0085
            if not isinstance(e, dict):
                continue
            k, n = e.get('kind'), e.get('turn') or 0
            s.t, s.expect, s.mailed = e.get('t') or s.t, max(s.expect, e.get('expect') or 0), max(s.mailed, e.get('inbox') or 0)
            if k == 'turn_start':   # a turn opens for an input, so it is expected: a steer its turn outran answers in the
                s.turn, s.expect = max(s.turn, n), max(s.expect, n)   # next one, past the turn its marker named
            elif k == 'turn_end':
                s.end, s.open = max(s.end, n), set()
            elif k == 'tool_call':
                s.open.add(e.get('call'))
            elif k == 'tool_result':
                s.open.discard(e.get('call'))
        return s

    def next_turn(s, delivery, alive):   # which turn will answer a message delivered now
        base = max(s.turn, s.end)
        if not alive or delivery == 'queue':
            return max(s.expect if alive else 0, base) + 1
        cur = max(s.expect, s.turn)       # steer: a running turn absorbs it
        return cur if cur > s.end else s.end + 1


def lock(d):   # THE delivery lock (events.jsonl's): one record at a time, its turn computed under it
    f = open(J(d, 'events.jsonl'), 'ab')
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def marker(d, text, expect, turn, **mail):   # THE delivery marker (a send, MSG, mail), written before the message
    # itself, so a delivery that dies mid-write is typed. mail: the inbox line it delivers (inbox=its line number)
    append(J(d, 'events.jsonl'), event('progress', turn, expect=expect, bytes=len(text.encode()), text='input: ' + clip(' '.join(text.split()), 200), **mail))


def record(prov, text):   # a message as one in.fifo line
    return json.dumps(wires.input_record(prov, text), ensure_ascii=False).encode()


def deliver(d, prov, fd, text, expect, turn):   # a send's delivery, through in.fifo. The record is framed LF + line + LF:
    # a killed sender's torn bytes end at the next record's leading LF (whole() drops them)
    marker(d, text, expect, turn)
    data = b'\n' + record(prov, text) + b'\n'
    while data:
        data = data[os.write(fd, data):]


def label(o):   # an inbox line (the driver's `hands.sh mail`) as its seat reads it
    return 'MAIL from driver:\n%s' % o.get('text')


def whole(d, lines):   # in.fifo -> the records to deliver: whole JSON-object lines only; a torn one is dropped, typed, never fused
    out = []
    for ln in lines:
        if ln.strip() and isinstance(parse_json(ln.decode('utf-8', 'replace')), dict):
            out.append(ln)
        elif ln.strip():
            append(J(d, 'events.jsonl'), event('progress', State(d).read().turn, text='torn input dropped: ' + clip(ln.decode('utf-8', 'replace'), 120)))
    return out


class Seat:
    """The wrapper: it outlives the CLI, owns the translation, and writes every turn's envelope."""
    def __init__(s, d, h, msg, ready):
        s.d, s.h, s.msg, s.ready, s.prov = d, h, msg, ready, h['provider']
        s.fam, s.delivery = wires.PROVIDERS[s.prov][:2]
        s.errs, s.mail_off, s.wfd, s.W = [], size(J(d, 'outbox.jsonl')), None, None
        s.seen, s.halt, s.late = -1, False, False   # halt: a stop; late: the deadline
        # inq: the records the CLI is to get next, in order (in.fifo's, then the wrapper's own); ibuf: in.fifo's torn tail;
        # sent: MSG is delivered; post: when the outbox and inbox are next looked at
        s.inq, s.ibuf, s.sent, s.post = [], b'', False, 0

    def put(s, e):
        append(J(s.d, 'events.jsonl'), e)
        if e['kind'] == 'error':
            s.errs.append(e)
        if e['kind'] == 'turn_start':
            s.handle(e.get('session'))

    def handle(s, sid):   # the resume handle: the FIRST session a turn shows, never replaced
        if sid and not s.h.get('session'):
            s.h['session'] = sid
            put(J(s.d, 'session'), json.dumps(s.h) + '\n')

    def pending(s):
        return State(s.d).read().expect > s.W.turn

    def mail(s):   # each new outbox line -> one push to the driver
        text, s.mail_off = tail(J(s.d, 'outbox.jsonl'), s.mail_off)
        turn = State(s.d).read().turn if text else 0   # the turn the mail arrived in
        for ln in text.split('\n')[:-1]:   # LF only, as State.read
            if ln.strip():
                o = parse_json(ln)
                push(s.d, s.h, turn, 'mail', 'mail to driver', str(o.get('text') if isinstance(o, dict) else o))

    def inbox(s):   # each inbox line not yet delivered -> the conversation, as a send's message, in order, once
        f = J(s.d, 'inbox.jsonl')
        if size(f) == s.seen:
            return
        held = s.held()
        if held is None:
            return
        with held:
            s.seen = size(f)
            with open(f, 'rb') as g:
                lines = g.read(s.seen).split(b'\n')[:-1]
            st = State(s.d).read()
            for n, ln in enumerate(lines, 1):
                o = parse_json(ln.decode('utf-8', 'replace'))
                if n <= st.mailed or not isinstance(o, dict):
                    continue
                s.take(label(o), st.next_turn(s.delivery, True), st.turn, inbox=n)
                st.read()

    def held(s):   # THE delivery lock, never waited for (a sender holding it may be blocked writing in.fifo, which only
        # this loop reads), with in.fifo drained under it: what a sender delivered before goes first. None: try again later
        f = open(J(s.d, 'events.jsonl'), 'ab')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        s.read_fifo()
        return f

    def take(s, text, expect, turn, **mail):   # the wrapper's own delivery (MSG, mail), under held(): straight into inq
        marker(s.d, text, expect, turn, **mail)
        s.inq.append(record(s.prov, text))

    def read_fifo(s):   # in.fifo's whole records -> inq
        while True:
            try:
                chunk = os.read(s.rfd, 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            *done, s.ibuf = (s.ibuf + chunk).split(b'\n')
            s.inq.extend(whole(s.d, done))

    def tick(s):   # every loop pass: MSG first; then, once a second, the outbox pushed and the inbox delivered
        if not s.send_msg(0):
            return
        if time.time() >= s.post:
            s.post = time.time() + 1
            s.mail()
            s.inbox()

    def send_msg(s, wait):   # MSG, as soon as the lock is free (tried for `wait` seconds) -> whether it is delivered
        t0 = time.time()
        while not s.sent:
            held = s.held()
            if held is not None:
                with held:
                    s.take(s.msg, s.first, s.first - 1)
                s.sent = True
                s.acked(s.first)
            elif time.time() - t0 >= wait:
                return False
            else:
                time.sleep(0.05)
        return True

    def acked(s, n):   # the spawn caller's ACK: the turn MSG opens, or 0 = the launch was typed before any turn
        if s.ready is not None:
            try:
                os.write(s.ready, b'%d\n' % n)
            except OSError:   # the caller is gone (killed, or a stop during the launch): the seat runs on
                pass
            os.close(s.ready)
            s.ready = None

    def letgo(s, rfd):   # the CLI is gone: no reader, so a send finds the seat gone; the writer goes under the lock.
        # MSG, when a stop came before the loop delivered it, is marked first: the close-out types its turn
        s.send_msg(5)
        os.close(rfd)
        with lock(s.d):
            os.close(s.wfd)
            s.wfd = None

    def fifo(s):
        f = J(s.d, 'in.fifo')
        if not (os.path.exists(f) and stat.S_ISFIFO(os.stat(f).st_mode)):
            if os.path.lexists(f):
                os.remove(f)
            os.mkfifo(f, 0o600)
        s.rfd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)   # the reader first, so neither open blocks
        s.wfd = os.open(f, os.O_WRONLY)                    # the wrapper holds a writer: no EOF until it lets go
        return s.rfd

    def run(s):
        d, h = s.d, s.h
        signal.signal(signal.SIGTERM, lambda *a: setattr(s, 'halt', True))   # a stop: the wrapper closes its CLI
        s.first, s.t_first, s.eoff, st = 0, time.time(), size(J(d, 'stderr.log')), State(d).read()
        # a wrapper killed before its close-out left turns delivered and never answered: they close first, as its close-out
        # would have, so this launch numbers its turns past every marker already written (else a wait counts to the wrong turn)
        s.finish([closed_turn(n, 'incomplete', session=h.get('session')) for n in range(st.end + 1, st.expect + 1)], None)
        s.first = State(d).read().next_turn(s.delivery, False)
        deadline = s.limit()
        # THE POLITE CLOSE (every wire), at the deadline and at a stop alike: the wrapper ends the CLI itself, with
        # Cli.close (stdin EOF first, TERM only after its grace) -- opencode keeps a turn's execution claim on a TERM and
        # releases it on a polite close (opencode 2.0.22); codex's app-server reaps its commands on EOF, which an interrupt
        # alone does not (codex-cli 0.160), so a codex turn in flight is interrupted first. The reaper is a backstop 10 s
        # past the deadline: it ends the CLI's group itself if the wrapper's close has not
        wrap = lambda argv: reaped_argv(deadline + 10 - time.time(), argv)
        if s.fam != 'acp':   # claude and codex translate on one Wire for the whole launch; ACP per turn (acp_turn)
            s.W = wires.StreamTranslator(s.prov, s.first - 1, h.get('session'), s.put, s.pending)
        seat, why = wires.launch(h, wrap, s.log, J(d, 'stderr.log'), s.feed)
        if why:
            return s.early(*why, seat=seat)
        if s.W and seat.session_id:   # codex: the thread its handshake started or resumed
            s.W.session = seat.session_id
        s.fifo()
        s.waiting, s.n, s.asked = [], s.first, False
        while not s.halt and seat.proc.poll() is None and time.time() < deadline:
            s.tick()
            s.waiting += s.inq
            s.inq = []
            s.hand_on(seat)
            r, w = seat.fds()
            r, w, _ = select.select(r + [s.rfd], w, [], 0.5)
            seat.on_ready(r, w)
            if s.rfd in r:
                s.read_fifo()
        s.late = seat.proc.poll() is None and time.time() >= deadline   # set BEFORE the close the turn reads it in
        if seat.proc.poll() is None and s.W and s.W.turn_id:   # codex outlived the loop: the deadline or a stop ends its turn
            try:
                seat.wait_for_answer('turn/interrupt', {'threadId': s.W.session, 'turnId': s.W.turn_id}, 5)
            except wires.WireError:
                pass
        s.letgo(s.rfd)
        rc = s.cut(rcode(seat.close(2)))   # close() answers a prompt whose pipe an escaped child still holds
        s.mail()                           # the lines still waiting were delivered and never run: the close-out types them
        s.close_out(rc, s.W)

    def log(s, line, way):   # stream.jsonl: claude's stdout verbatim; every JSON-RPC line, both ways, stamped
        if s.fam != 'stdin':
            append(J(s.d, 'stream.jsonl'), {'t': now(), 'dir': way, 'msg': parse_json(line)})
        elif way == 'in':
            with open(J(s.d, 'stream.jsonl'), 'ab') as f:
                f.write(line.encode() + b'\n')

    def hand_on(s, seat):   # the lines waiting -> the CLI. claude takes each at once (it queues or steers them itself);
        # an ACP agent one prompt at a time (ACP v1 has no queue), turn `n` next; codex one turn/start at a time, each sent
        # once the last is answered (`asked`), so it steers the turn its predecessor started
        if s.fam == 'stdin' and s.waiting:
            seat.write(b''.join(ln + b'\n' for ln in s.waiting))
            s.waiting = []
        elif s.fam == 'acp' and s.waiting and not seat.busy():
            s.acp_turn(seat, s.n, wires.record_text(s.waiting.pop(0)))
            s.n += 1
        elif s.fam == 'rpc' and s.waiting and not s.asked:
            s.rpc_turn(seat, s.waiting.pop(0))

    def feed(s, o):   # what the CLI said (a claude stdout line; a codex notification) -> its events, and the turns it
        # closed through finish; a stream error that closed none is pushed
        if not isinstance(o, dict):
            s.W.emit('progress', text='non-JSON line: ' + clip(o, 120))
            return
        s.errs = []
        recs = s.W.feed(o)
        s.finish(recs, s.cut(None))
        for e in s.errs if not recs else []:
            push(s.d, s.h, e['turn'], 'error', 'stream error', e.get('text'))

    def finish(s, recs, rc):
        """ONE rule for every wire: typed status -> turn files -> envelope -> usage + turn_end events -> push."""
        d, h = s.d, s.h
        for r in recs:
            status, text, reason, reset = r['status'], r['text'], None, ''
            s.handle(r['session'])
            if r['session'] and r['session'] != h.get('session'):   # SESSION CONSTANCY: a turn on another
                status, reason, text = 'error', 'session-changed', ''   # conversation (a resume that started fresh)
            elif status == 'ok' and not text.strip():
                status, reason, text = 'error', 'empty', ''
            elif status != 'ok':   # typed from provider error fields and the CLI's stderr, never from prose; a turn a stop
                # ended is incomplete, whatever its agent said as it went (codex: interrupted; opencode: cancelled)
                status, reset = ('incomplete', '') if s.halt else typed(
                    status, r['error_text'] + ('\n' + since(J(d, 'stderr.log'), s.eoff) if rc is not None else ''), rc)
                text = ''
            # a turn its CLI answered ok reads rc 0, whatever ended the CLI after it (a stop, the deadline)
            rc_ = 0 if status == 'ok' else rc if rc not in (None, 0) else 1
            n, session, use = r['turn'], h.get('session') or r['session'], r['usage']
            if use and type(r['extra'].get('cost_usd')) in (int, float):   # the turn's cost, when the wire reports it
                use = dict(use, cost_usd=r['extra']['cost_usd'])
            os.makedirs(J(d, 'turn-%d' % n), exist_ok=True)
            put(J(d, 'turn-%d' % n, 'result.txt'), text)
            t0 = s.t_first if n == s.first else (r['started'] or s.t_first)
            envelope(d, status, s.prov, h.get('model'), rc_, int(time.time() - t0), reset, use, h.get('effort'),
                     session=session, turn=n, delivery=s.delivery, reason=reason)
            if status == 'ok':   # result.txt -> the latest ok turn's answer
                if os.path.lexists(J(d, '.result.lnk')):
                    os.remove(J(d, '.result.lnk'))
                os.symlink(J('turn-%d' % n, 'result.txt'), J(d, '.result.lnk'))
                os.replace(J(d, '.result.lnk'), J(d, 'result.txt'))
            if r['usage'] or r['extra']:
                s.put(event('usage', n, usage=r['usage'], **r['extra']))
            s.put(event('turn_end', n, status=status, text=text, session=session, usage=r['usage'], final=r['final'], rc=rc_, reason=reason))
            push(d, h, n, 'turn_end', status, text or reason or reset)

    def close_out(s, rc, W=None):   # the open turn, then every turn delivered and never answered, closes; then the exit
        if W and W.current:
            s.finish([W.cut()], rc)
        st = State(s.d).read()
        s.finish([closed_turn(n, 'incomplete', session=W.session if W else s.h.get('session')) for n in range(st.end + 1, st.expect + 1)], rc)
        s.put(event('progress', W.turn if W else st.turn, text='exit rc=%s' % rc))

    def early(s, status, reason, why, seat=None):
        """A launch typed before any turn: its envelope, no turn files; spawn prints it (rc 2 = refused)."""
        rc, reset = 2, why
        if status != 'refused':   # typed like any failure: the error, the CLI's stderr, the reaper's rc
            rc = rcode(seat.close(2)) if seat else 1
            status, reset = typed(status, why + '\n' + since(J(s.d, 'stderr.log'), s.eoff), rc)
            reset = reset or clip(why)
            if s.halt:   # a stop during the launch ended it: incomplete, rc 143, as any turn a stop ends
                status, rc, reset = 'incomplete', 143, ''
        s.put(event('error', s.first, text=clip('%s: %s' % (reason, why))))
        envelope(s.d, status, s.prov, s.h.get('model'), rc, int(time.time() - s.t_first), reset, None, s.h.get('effort'),
                 session=s.h.get('session'), delivery=s.delivery, reason=reason)
        s.acked(0)
        if seat and status == 'refused':   # a failure closed it above, for its rc
            seat.close(2)

    def limit(s):   # the launch's deadline: its --timeout from the first turn's start; none given = no deadline (INF)
        return s.t_first + int(s.h['timeout']) if s.h.get('timeout') else INF

    def cut(s, rc):   # the rc of what the wrapper's own close ended: the deadline's reads 142 (typed timeout) and a stop's
        return 142 if s.late else 143 if s.halt else rc   # 143, as the reaper's alarm and a TERM would; else the CLI's own

    def rpc_turn(s, rpc, ln):   # THE rpc delivery: every line is a turn/start, which the app-server steers into the turn in
        # flight or starts the next turn with (its protocol: turnTrigger is "ignored when this request steers an already-
        # active turn") -- so no client guess of the open turn races its end. The
        # seat's effort rides every one. A turn/start it refuses on an idle thread is that turn, failed; an
        # agent gone mid-delivery leaves the turn to the close-out
        inp = [{'type': 'text', 'text': wires.record_text(ln)}]
        p = dict({'threadId': s.W.session, 'input': inp}, **({'effort': s.h['effort']} if s.h.get('effort') else {}))
        s.asked = True
        rpc.request('turn/start', p, s.rpc_answered)

    def rpc_answered(s, r):   # turn/start's answer: the next line may go; a refusal on an idle thread is that turn, failed
        s.asked = False
        if r is None or 'error' not in r:
            return
        e = r['error'] if isinstance(r['error'], dict) else {'message': str(r['error'])}
        why = 'turn/start refused: %s %s' % (e.get('code'), e.get('message'))
        recs = s.W.open_turn() if s.W.current is None else None
        s.W.emit('error', text=clip(why))
        if recs is not None:
            s.finish(recs + [s.W.close('error', '', None, 'none', why)], None)

    def forget(s, sid, n):   # GUARD 2, ABANDONED TURNS: a turn whose agent went mid-turn other than by the
        # deadline's close (a stop -- a polite close too, kept here until a live opencode check shows it releases the claim
        # --, a crash, a kill) may keep its execution claim, which opencode's managed background service
        # resumes at its next boot; its session is deleted -- the wire's own command (wires.forget; None = nothing to delete), in
        # the seat's cwd, under the reaper, its output to stderr.log (no provider error: failures are typed from
        # past it) -- before the turn's envelope lands, so a sync buy's stop never races it
        argv = wires.forget_command(s.prov, sid, s.h.get('cli'))
        if argv:
            with open(J(s.d, 'stderr.log'), 'ab') as ef:
                rc = rcode(subprocess.call(reaped_argv(60, argv), stdin=subprocess.DEVNULL, stdout=ef, stderr=ef, cwd=s.h['cwd']))
            s.eoff = size(J(s.d, 'stderr.log'))
            s.put(event('progress', n, text='session %s deleted (rc %s): its turn was abandoned, so nothing may resume it' % (sid, rc)))

    def acp_turn(s, seat, n, text):   # turn n's prompt goes out; acp_done closes the turn when the agent answers
        seen, off, t0 = set(), size(J(s.d, 'stderr.log')), time.time()

        def sink(u):
            for e in wires.acp_events(u, n, now(), s.prov):
                if e['kind'] == 'file':   # grok reports one edit twice: one file event per call and path
                    if (e.get('call'), e.get('path')) in seen:
                        continue
                    seen.add((e.get('call'), e.get('path')))
                s.put(e)
        seat.prompt(text, sink, lambda res: s.acp_done(seat, n, res, off, t0))

    def acp_done(s, seat, n, res, off, t0):
        *head, end = wires.acp_events(res, n, now(), s.prov)
        use = ([e for e in head if e['kind'] == 'usage'] or [{}])[0]
        for e in head:
            if e['kind'] == 'error':
                s.put(e)
        rc, err = None, ''
        if end['status'] != 'ok':   # typed from the wire's error fields and this turn's stderr, never from prose
            try:   # did its agent go with it? A killed agent words its end three ways: incomplete, cancelled (opencode on
                # a TERM, sometimes), or an error (opencode 2.0.22 when its server died first); an error from
                # an agent that lives on (a provider refusal) leaves rc None. The wrapper's own close reads its cut, else the reaper's rc
                rc = rcode(seat.proc.wait(10 if end['status'] in ('incomplete', 'cancelled') else 3))
            except subprocess.TimeoutExpired:
                pass
            err = json.dumps(res.get('error') or res.get('incomplete') or '', ensure_ascii=False) + '\n' + since(J(s.d, 'stderr.log'), off)
            if rc is not None and not s.late:   # its agent gone mid-turn, not by the deadline: the turn is abandoned
                s.forget(seat.session_id, n)
            rc = s.cut(rc)
        s.finish([closed_turn(n, end['status'], end.get('text'), use.get('usage'), end.get('final'), err, end.get('session'), t0,
                      {k: use[k] for k in ('usage_unknown', 'cost_usd') if k in use})], rc)


# ---------- the verbs
def spawn(d, h, msgfile):
    with open(msgfile) as f:
        msg = f.read()
    capture(d)
    for _ in range(5):   # ONE wrapper per seat: it holds job.log's lock for its whole life (the kernel drops it at its exit)
        log = os.open(J(d, 'job.log'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:   # a concurrent launch won (or the last wrapper is closing out): MSG is a send to that seat
            os.close(log)
        rc = send(d, h, msgfile)
        if rc != 3:
            return rc
    else:
        print('seat.py: %s: the seat lock stayed taken (see %s/job.log)' % (d, d), file=sys.stderr)
        return 1
    r, w = os.pipe()
    pid = os.fork()
    if pid:
        os.close(w)
        os.close(log)   # the wrapper holds the lock now
        # the ack, or EOF at the wrapper's exit: a slow launch (ACP handshake calls: each bounded) is
        # never reported undelivered while its wrapper runs; the cap only ends a wrapper stuck past every bound
        got = (os.read(r, 64) if select.select([r], [], [], 600)[0] else b'').strip()
        if got not in (b'', b'0'):
            print(dump({'status': 'spawned', 'pid': pid, 'dir': d, 'expect': int(got),
                        'delivery': wires.PROVIDERS[h['provider']].delivery, 'note': NOTE}))
            return 0
        env = jload(J(d, 'envelope.json')) if got == b'0' else None
        if env:   # the wrapper typed the launch before any turn: a refusal (rc 2) or a failure; its why on stderr
            print('seat.py: %s %s: %s' % (env.get('status'), env.get('reason'), env.get('reset')), file=sys.stderr)
            print(shown(env))
            return 2 if env.get('status') == 'refused' else 1
        print('seat.py: the wrapper never delivered the message (see %s/job.log)' % d, file=sys.stderr)
        if not wrappers(d):   # it died: the envelope is still owed
            envelope(d, 'error', h['provider'], h.get('model'), 1, 0, '', None, h.get('effort'), reason='wrapper-failed')
        return 1
    os.close(r)
    os.setsid()   # the wrapper outlives its caller's process group (a tool-call kill, a closed terminal)
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_IGN)   # nohup semantics, inherited by the seat: the reaper respects SIG_IGN
    # NAMES NO SEAT SEES, every family (the wrapper's env is every seat process's): the driver's inbox is not handed to
    # the seat (the push reads DIR/driver: see capture() for what that does and does not stop), and no opencode key
    # rides the env: an opencode seat runs on the user's own opencode login, and OPENCODE_API_KEY would reach every
    # seat's shell (a tool's `env` prints it into the transcript) and unlock the zen provider's paid models
    for k in ('CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'OPENCODE_API_KEY'):
        os.environ.pop(k, None)
    nul = os.open(os.devnull, os.O_RDONLY)
    for a, b in ((nul, 0), (log, 1), (log, 2)):   # job.log's locked description: no seat process inherits it
        os.dup2(a, b)
    try:
        Seat(d, h, msg, w).run()
    except BaseException:
        __import__('traceback').print_exc()
    finally:
        os._exit(0)


def send(d, h, path):
    with open(path) as f:
        msg = f.read()
    capture(d)
    prov = h['provider']
    for _ in range(150):   # a wrapper with no reader is starting or closing out: wait for either
        with lock(d):                            # one delivery at a time: a record is one uninterleaved write
            if not wrappers(d):
                try:                             # a resume is use: a buy's sweep keeps a dir written within KEEP
                    os.utime(d)
                except OSError:
                    pass
                return 3                         # the seat is gone: hands.py relaunches it
            try:
                fd = os.open(J(d, 'in.fifo'), os.O_WRONLY | os.O_NONBLOCK)
            except OSError:                      # ENXIO: no reader yet, or none any more
                fd = None
            if fd is not None:
                os.set_blocking(fd, True)
                st = State(d).read()
                sent = st.next_turn(wires.PROVIDERS[prov].delivery, True)
                try:
                    deliver(d, prov, fd, msg, sent, st.turn)
                except OSError:                  # its reader died mid-write: its wrapper types that turn
                    sent = None
                os.close(fd)
                if sent is not None:
                    print(dump({'status': 'sent', 'dir': d, 'expect': sent, 'delivery': wires.PROVIDERS[prov].delivery}))
                    return 0
        time.sleep(0.2)
    print('seat.py: %s has a live wrapper but no reader (a replay backoff?) -- send again shortly' % d, file=sys.stderr)
    return 1


def main(a):
    cmd = a[0] if a else ''
    if cmd == 'reap' and len(a) > 2:
        return reap(int(a[1]), a[2:])
    if cmd not in ('spawn', 'send') or len(a) < 3 or not os.path.isdir(a[1]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    d = os.path.realpath(a[1])
    if cmd == 'spawn' and a[1] != d:   # the wrapper is found by its argv: that argv carries the canonical dir
        os.execv(sys.executable, [sys.executable, J(HERE, 'seat.py'), 'spawn', d] + a[2:])
    h = jload(J(d, 'session'))
    if not h or not os.path.isfile(a[2]):
        print('seat.py: %s needs a persistent seat dir (a session handle) and an existing FILE' % cmd, file=sys.stderr)
        return 2
    return (spawn if cmd == 'spawn' else send)(d, h, a[2])


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
