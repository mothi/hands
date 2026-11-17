#!/usr/bin/env python3
"""The wrapper: the background process that runs one seat.

hands.py makes the job dir and starts its wrapper (spawn). The wrapper launches the CLI
through wires.py, delivers messages, writes events.jsonl, and closes each turn with its
result.txt and envelope.json (close_turns). It runs until the CLI exits, the seat's deadline
passes, or `hands.sh stop` ends it.

Its command line is internal; hands.py calls it:
  spawn DIR FILE   start the seat's wrapper, with FILE as its first message
  send DIR FILE    deliver FILE to the seat's live wrapper (rc 3: there is none)
  reap SECS ARGV   run ARGV in its own process group for at most SECS seconds (0: no limit)

The first message rides in the launch. A send must reach a live seat now or relaunch it, so
it uses in.fifo. A nonblocking open tells the sender at once whether the wrapper is reading.
The wrapper closes the pipe under the delivery lock when it leaves. Mail goes to inbox.jsonl
so it can wait on disk with no seat running. Mail never starts a seat.

See the README's Job dir reference for the files and event fields.

Python 3 standard library only.
"""
import fcntl
import json
import math
import os
import select
import signal
import stat
import subprocess
import sys
import time
import traceback

import push
import wires
from wires import (DEADLINE_RC, NO_LIMIT, append_json, classify, clip, closed_turn, event,
                   exit_code, file_size, open_regular, parse_json, read_json, read_lines,
                   read_tail, reaper_argv, write_atomic)

# Where hands.py makes the job dirs it names itself (a start without --out, a probe). They are
# state that should outlive a reboot, so not in $TMPDIR, which macOS clears at boot.
STATE_HOME = os.environ.get('XDG_STATE_HOME', '')
if not os.path.isabs(STATE_HOME):   # the XDG spec ignores a relative value
    STATE_HOME = os.path.join(os.path.expanduser('~'), '.local', 'state')
JOBS = os.path.join(STATE_HOME, 'hands')

NOTE = ('turn-N/result.txt + envelope.json land at each turn_end (absent = not finished, never '
        'finished-empty); liveness = the age of the last events.jsonl line, or an open '
        'tool_call; the seat ends when its last turn ends, and send resumes it; next: hands.sh '
        'wait|send|mail|events <dir> -- hands.sh stop <dir> ends a turn in flight, never kill '
        'the wrapper')

PRINTED_USAGE = ('in', 'cache_read', 'cache_write', 'out', 'cost_usd')   # the file keeps all
STOP_RC = 143             # the rc of a CLI a stop ended: 128 + TERM, as a TERM's reads


# ---------------------------------------------------------------------------------------------
# The command line


def main(args):
    command = args[0] if args else ''
    if command == 'reap' and len(args) > 2:
        return reap(int(args[1]), args[2:])
    if command not in ('spawn', 'send') or len(args) < 3 or not os.path.isdir(args[1]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    # hands.py names the canonical dir, so the wrapper's command line (how it is found) does too
    job_dir = os.path.realpath(args[1])
    handle = read_json(os.path.join(job_dir, 'session'))
    if not handle or not os.path.isfile(args[2]):
        print('seat.py: %s needs a persistent job dir with a handle and an existing FILE'
              % command, file=sys.stderr)
        return 2
    return (spawn if command == 'spawn' else send)(job_dir, handle, args[2])


def spawn(job_dir, handle, message_file):
    """Start the seat's wrapper with its first message, and print the wrapper's ack."""
    with open(message_file) as f:
        message = f.read()
    push.save_driver_address(job_dir, live_seats)
    # One wrapper per seat: it holds job.log's lock for its whole life.
    for _ in range(5):
        log = os.open(os.path.join(job_dir, 'job.log'),
                      os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:   # another launch won, or the last wrapper is closing: send to it
            os.close(log)
        rc = send(job_dir, handle, message_file)
        if rc != 3:
            return rc
    else:
        print('seat.py: %s: the seat lock stayed taken (see %s/job.log)' % (job_dir, job_dir),
              file=sys.stderr)
        return 1
    ack_read, ack_write = os.pipe()
    pid = os.fork()
    if pid:
        os.close(ack_write)
        os.close(log)   # the wrapper holds the lock now
        return report_spawn(job_dir, handle, pid, ack_read)
    os.close(ack_read)
    run_wrapper(job_dir, handle, message, log, ack_write)


def report_spawn(job_dir, handle, pid, ack_read):
    """In the spawn caller: wait for the wrapper's ack, and print what happened."""
    # A slow launch (each ACP handshake call has its own limit) is never reported undelivered
    # while its wrapper runs; this cap only ends a wrapper stuck past every limit.
    ready = select.select([ack_read], [], [], 600)[0]
    ack = (os.read(ack_read, 64) if ready else b'').strip()
    if ack not in (b'', b'0'):
        delivery = wires.PROVIDERS[handle['provider']].delivery
        print(one_line({'status': 'spawned', 'pid': pid, 'dir': job_dir, 'expect': int(ack),
                        'delivery': delivery, 'note': NOTE}))
        return 0
    envelope = read_json(os.path.join(job_dir, 'envelope.json')) if ack == b'0' else None
    if envelope:   # the launch failed, or was refused, before any turn
        print('seat.py: %s %s: %s' % (envelope.get('status'), envelope.get('reason'),
                                      envelope.get('reset')), file=sys.stderr)
        print(printed(envelope))
        return 2 if envelope.get('status') == 'refused' else 1
    print('seat.py: the wrapper never delivered the message (see %s/job.log)' % job_dir,
          file=sys.stderr)
    if not wrappers(job_dir):   # it died, so its envelope is still owed
        write_envelope(job_dir, 'error', handle['provider'], handle.get('model'), 1, 0, '', None,
                       handle.get('effort'), reason='wrapper-failed')
    return 1


def run_wrapper(job_dir, handle, message, log, ack_fd):
    """In the forked child: become the detached wrapper and run the seat. Never returns."""
    os.setsid()   # outlive the caller's process group (a killed tool call, a closed terminal)
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_IGN)   # as nohup does; the reaper keeps it for the CLI
    # HANDS_IDLE: the seconds a seat whose turns have all ended waits for the next send. Only the
    # tests set it, to keep one CLI across a recording's turns; it is not part of the interface.
    idle_grace = seconds(os.environ.pop('HANDS_IDLE', ''))
    # Names no seat process may see. The driver's session stays out (pushes read DIR/driver).
    # An opencode seat runs on the user's own login: OPENCODE_API_KEY would reach every tool's
    # shell, and unlock paid models.
    for name in push.DRIVER_NAMES + ['OPENCODE_API_KEY']:
        os.environ.pop(name, None)
    null = os.open(os.devnull, os.O_RDONLY)
    for source, target in ((null, 0), (log, 1), (log, 2)):   # its output goes to job.log
        os.dup2(source, target)
    try:
        Wrapper(job_dir, handle, message, ack_fd, idle_grace).run()
    except BaseException:
        traceback.print_exc()
    finally:
        os._exit(0)


def send(job_dir, handle, message_file):
    """Deliver a message to the seat's live wrapper and print the ack -> 0; 3 when no wrapper is
    alive (hands.py then relaunches the seat); 1 when the wrapper never reads."""
    with open(message_file) as f:
        message = f.read()
    push.save_driver_address(job_dir, live_seats)
    provider = handle['provider']
    delivery = wires.PROVIDERS[provider].delivery
    for _ in range(150):   # a wrapper with no reader is starting or closing: wait for either
        with delivery_lock(job_dir):
            if not wrappers(job_dir):
                try:
                    os.utime(job_dir)   # a resume counts as use: the 30-day sweep keeps the dir
                except OSError:
                    pass
                return 3
            try:
                fd = os.open(os.path.join(job_dir, 'in.fifo'), os.O_WRONLY | os.O_NONBLOCK)
            except OSError:   # no reader yet, or none any more
                fd = None
            if fd is not None:
                os.set_blocking(fd, True)
                state = SeatState(job_dir).read()
                expect = state.next_turn(delivery, True)
                try:
                    deliver(job_dir, provider, fd, message, expect, state.turn)
                except OSError:   # the reader died mid-write: its wrapper types that turn
                    expect = None
                os.close(fd)
                if expect is not None:
                    print(one_line({'status': 'sent', 'dir': job_dir, 'expect': expect,
                                    'delivery': delivery}))
                    return 0
        time.sleep(0.2)
    print('seat.py: %s has a live wrapper that is not reading (it is starting or closing) -- '
          'send again shortly' % job_dir, file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------------------------
# The wrapper


class Wrapper:
    """Runs one seat: launches its CLI, delivers its messages, and records its turns.

    It runs while the seat has a turn to run: once every delivered turn has ended and its idle
    grace (0 seconds unless a test sets HANDS_IDLE) passes with no send, it closes the CLI as an
    idle stop does and exits; `send` relaunches the seat on its recorded session. It outlives the
    CLI, so it can always close the last turn and write its envelope."""

    def __init__(self, job_dir, handle, first_message, ack_fd, idle_grace=0):
        self.job_dir = job_dir
        self.handle = handle
        self.first_message = first_message
        self.ack_fd = ack_fd                # tells the spawn caller the first message is in
        self.provider = handle['provider']
        self.delivery = wires.PROVIDERS[self.provider].delivery
        self.state = SeatState(job_dir)     # events.jsonl, read on where the last read stopped
        self.stopping = False               # `hands.sh stop` asked the wrapper to end
        self.past_deadline = False
        self.started_at = time.time()
        self.stderr_offset = 0              # stderr.log before this launch types no failure
        self.first_turn = 0
        self.first_sent = False
        self.outbox_offset = file_size(self.path('outbox.jsonl'))
        self.inbox_size = -1                # inbox.jsonl's size when last read
        self.next_mail_check = 0
        self.fifo_reader = None
        self.fifo_writer = None             # held open, so in.fifo never reads end of file
        self.fifo_partial = b''             # the start of an in.fifo line not yet ended
        self.waiting = []                   # delivered records not yet handed to the CLI
        self.pushes_due = True              # send the pending pushes on the next pass: the last
                                            # try found another sender, or the launch has not
                                            # tried yet
        self.idle_grace = idle_grace        # seconds an idle seat waits for a send
        self.idle_since = None              # when the seat last became idle
        self.left = False                   # the loop ended because the seat was idle

    # -- the loop

    def run(self):
        handle = self.handle
        signal.signal(signal.SIGTERM, lambda *args: setattr(self, 'stopping', True))
        self.stderr_offset = file_size(self.path('stderr.log'))
        # A wrapper killed before its close-out left turns delivered and never answered. They
        # close now, so this launch numbers its turns after them.
        state = self.state.read()
        orphans = range(state.ended + 1, state.expected + 1)
        self.close_turns([closed_turn(n, 'incomplete', session=handle.get('session'))
                          for n in orphans], None)
        self.first_turn = state.read().next_turn(self.delivery, False)
        ends_at = self.deadline()

        # The wrapper ends the CLI itself, at the deadline as at a stop: stdin's end first, and
        # TERM only after a grace. opencode releases a turn's claim only on such a close, and
        # codex's app-server ends its commands only at stdin's end. The reaper is a backstop,
        # 10 s past the deadline.
        def under_reaper(argv):
            return reaper_argv(ends_at + 10 - time.time(), argv)

        cli, failure = wires.launch(handle, under_reaper, self.path('stderr.log'),
                                    self.path('stream.jsonl'), self.first_turn - 1, self)
        if failure:
            return self.launch_failed(*failure, cli=cli)
        self.open_fifo()
        while not self.stopping and cli.proc.poll() is None and time.time() < ends_at:
            self.tick()
            self.hand_on(cli)
            if self.leave_when_idle(cli):
                break
            readable, writable = cli.fds()
            readable, writable, _ = select.select(readable + [self.fifo_reader], writable, [], 0.5)
            cli.on_ready(readable, writable)
            if self.fifo_reader in readable:
                self.read_fifo()
        # Set before the close: the turns it ends read it.
        self.past_deadline = cli.proc.poll() is None and time.time() >= ends_at
        if not self.left:
            cli.interrupt()
        # A stop that came before the first message was delivered marks it now, so the close-out
        # types its turn.
        self.send_first_message(5)
        self.release_fifo()
        rc = self.closing_rc(exit_code(cli.close(2)))
        self.push_outbox()
        self.close_out(cli, rc)
        self.send_pushes(wait=True)   # also the one retry of a push that failed a moment ago

    def tick(self):
        """Each pass of the loop: the first message, then once a second the outbox and inbox."""
        if not self.send_first_message(0):
            return
        if time.time() >= self.next_mail_check:
            self.next_mail_check = time.time() + 1
            if self.pushes_due:
                self.send_pushes()
            self.push_outbox()
            self.deliver_inbox()

    def hand_on(self, cli):
        """Give the CLI the records waiting for it, as its client takes them."""
        if self.waiting:
            cli.hand_over(self.waiting)

    def deadline(self):
        """When the launch must end: --timeout seconds after it began, else never."""
        timeout = self.handle.get('timeout')
        return self.started_at + int(timeout) if timeout else NO_LIMIT

    # -- messages in

    def open_fifo(self):
        fifo = self.path('in.fifo')
        if not (os.path.exists(fifo) and stat.S_ISFIFO(os.stat(fifo).st_mode)):
            if os.path.lexists(fifo):
                os.remove(fifo)
            os.mkfifo(fifo, 0o600)
        self.fifo_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)   # first: neither blocks
        self.fifo_writer = os.open(fifo, os.O_WRONLY)

    def read_fifo(self):
        while self.fifo_reader is not None:
            try:
                chunk = os.read(self.fifo_reader, 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            *lines, self.fifo_partial = (self.fifo_partial + chunk).split(b'\n')
            self.waiting.extend(whole_records(self.job_dir, lines, self.state))

    def try_delivery_lock(self):
        """The delivery lock if it is free now, else None.

        The wrapper never waits for it: a sender that holds it may be blocked writing to
        in.fifo, which only this loop reads. What senders delivered before is read first."""
        f = open(self.path('events.jsonl'), 'ab')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        self.read_fifo()
        return f

    def queue_own(self, text, expect, turn, **mail):
        """The wrapper's own delivery (the first message, the inbox), under the lock."""
        mark_delivery(self.job_dir, text, expect, turn, **mail)
        self.waiting.append(fifo_record(self.provider, text))

    def send_first_message(self, wait):
        """Deliver the first message once the lock is free, trying for `wait` s -> whether sent."""
        tried = time.time()
        while not self.first_sent:
            lock = self.try_delivery_lock()
            if lock is not None:
                with lock:
                    self.queue_own(self.first_message, self.first_turn, self.first_turn - 1)
                self.first_sent = True
                self.ack(self.first_turn)
            elif time.time() - tried >= wait:
                return False
            else:
                time.sleep(0.05)
        return True

    def ack(self, turn):
        """Tell the spawn caller the turn its message opens (0: the launch failed first)."""
        if self.ack_fd is None:
            return
        try:
            os.write(self.ack_fd, b'%d\n' % turn)
        except OSError:   # the caller is gone; the seat runs on
            pass
        os.close(self.ack_fd)
        self.ack_fd = None

    def deliver_inbox(self):
        """Deliver each inbox line not delivered yet, in order, once."""
        inbox = self.path('inbox.jsonl')
        if file_size(inbox) == self.inbox_size:
            return
        lock = self.try_delivery_lock()
        if lock is None:
            return
        with lock:
            self.inbox_size = file_size(inbox)
            try:   # never through a link the seat planted
                with os.fdopen(open_regular(inbox, os.O_RDONLY), 'rb') as f:
                    lines = f.read(self.inbox_size).split(b'\n')[:-1]
            except OSError:
                return
            state = self.state.read()
            for number, line in enumerate(lines, 1):
                mail = parse_json(line.decode('utf-8', 'replace'))
                if number <= state.mailed or not isinstance(mail, dict):
                    continue
                expect = state.next_turn(self.delivery, True)
                self.queue_own(mail_text(mail), expect, state.turn, inbox=number)
                state.read()

    def expected(self):
        """The latest turn a delivery expects."""
        return self.state.read().expected

    # -- pushes to the driver

    def push_outbox(self):
        """Push each new outbox line (the seat's mail) to the driver."""
        text, self.outbox_offset = read_lines(self.path('outbox.jsonl'), self.outbox_offset)
        turn = self.state.read().turn if text else 0
        for line in text.split('\n')[:-1]:
            if not line.strip():
                continue
            mail = parse_json(line)
            body = mail.get('text') if isinstance(mail, dict) else mail
            self.push(turn, 'mail', 'mail to driver', str(body))

    def push(self, turn, kind, what, text):
        logged_at = push.log_push(self.job_dir, turn, kind, what, text)
        if logged_at is not None:
            self.send_pushes(logged_at=logged_at)

    def send_pushes(self, logged_at=None, wait=False):
        # When another sender holds the log, the next loop pass tries again.
        self.pushes_due = push.send_pushes(self.job_dir, self.handle, logged_at, wait) == 'busy'

    # -- turns

    def record_event(self, line):
        append_json(self.path('events.jsonl'), line)
        if line['kind'] == 'turn_start':
            self.keep_session(line.get('session'))

    def keep_session(self, session):
        """Save the first session a turn shows in the handle: it is what a relaunch resumes."""
        if session and not self.handle.get('session'):
            self.handle['session'] = session
            write_atomic(self.path('session'), json.dumps(self.handle) + '\n')

    def close_turns(self, turns, rc):
        """Each closed turn -> its status, result.txt, envelope, usage and turn_end events, and a
        push to the driver. Every provider's turns end here."""
        handle = self.handle
        for turn in turns:
            status = turn['status']
            text = turn['text']
            reason = None
            reset = ''
            self.keep_session(turn['session'])
            if turn['session'] and turn['session'] != handle.get('session'):
                status, reason, text = 'error', 'session-changed', ''   # a resume started fresh
            elif status == 'ok' and not text.strip():
                status, reason, text = 'error', 'empty', ''
            elif status != 'ok':
                text = ''
                if self.stopping:   # whatever the agent called it (interrupted, cancelled)
                    status = 'incomplete'
                else:
                    stderr = ''
                    if rc is not None:
                        stderr = '\n' + read_tail(self.path('stderr.log'), self.stderr_offset)
                    status, reset = failure_status(status, turn['error_text'] + stderr, rc)
            # A turn its CLI answered ok reads rc 0, whatever ended the CLI after it.
            turn_rc = 0 if status == 'ok' else rc if rc not in (None, 0) else 1
            number = turn['turn']
            session = handle.get('session') or turn['session']
            usage = turn['usage']
            cost = turn['extra'].get('cost_usd')
            if usage and type(cost) in (int, float):
                usage = dict(usage, cost_usd=cost)
            os.makedirs(self.path('turn-%d' % number), exist_ok=True)
            write_atomic(self.path('turn-%d' % number, 'result.txt'), text)
            started = turn['started'] or self.started_at
            if number == self.first_turn:
                started = self.started_at
            write_envelope(self.job_dir, status, self.provider, handle.get('model'), turn_rc,
                           int(time.time() - started), reset, usage, handle.get('effort'),
                           session=session, turn=number, delivery=self.delivery, reason=reason)
            if status == 'ok':
                self.link_result(number)
            if turn['usage'] or turn['extra']:
                self.record_event(event('usage', number, usage=turn['usage'], **turn['extra']))
            self.record_event(event('turn_end', number, status=status, text=text, session=session,
                                    usage=turn['usage'], final=turn['final'], rc=turn_rc,
                                    reason=reason))
            self.push(number, 'turn_end', status, text or reason or reset)

    def link_result(self, number):
        """Point result.txt at the latest ok turn's answer."""
        link = self.path('.result.lnk')
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(os.path.join('turn-%d' % number, 'result.txt'), link)
        os.replace(link, self.path('result.txt'))

    # -- the close

    def idle(self, cli):
        """Whether the seat has nothing to do: its first message in, every delivered turn ended,
        no record waiting for the CLI, and no turn open."""
        if not self.first_sent or self.waiting or cli.busy():
            return False
        state = self.state.read()
        return state.expected > 0 and state.ended >= state.expected

    def leave_when_idle(self, cli):
        """Idle for the idle grace (at least cli.linger(), while the CLI may hold a message it
        queued) -> close in.fifo under the delivery lock, after one last read of it, and say so:
        the loop ends, and the CLI is closed as an idle stop closes it. A send that got its
        message in first is taken; one that comes after finds no reader, then no wrapper, and
        relaunches the seat. A mail that landed meanwhile is delivered first."""
        if not self.idle(cli):
            self.idle_since = None
            return False
        if self.idle_since is None:
            self.idle_since = time.time()
        grace = max(self.idle_grace, cli.linger())
        if time.time() - self.idle_since < grace:
            return False
        lock = self.try_delivery_lock()   # it reads in.fifo
        if lock is None:
            return False
        with lock:
            if self.waiting or file_size(self.path('inbox.jsonl')) != self.inbox_size:
                return False
            self.close_fifo()
            self.left = True
        return True

    def release_fifo(self):
        """The CLI is done: close in.fifo, so a send finds the seat gone (unless the idle leave
        closed it already). It closes under the lock, between deliveries."""
        if self.fifo_writer is None:
            return
        with delivery_lock(self.job_dir):
            self.close_fifo()

    def close_fifo(self):
        for name in ('fifo_reader', 'fifo_writer'):
            if getattr(self, name) is not None:
                os.close(getattr(self, name))
                setattr(self, name, None)

    def closing_rc(self, rc):
        """The rc of a turn the wrapper's own close ended: 142 at the deadline and 143 at a stop
        (what the reaper's limit and a TERM read), else the CLI's own."""
        if self.past_deadline:
            return DEADLINE_RC
        if self.stopping:
            return STOP_RC
        return rc

    def close_out(self, cli, rc):
        """The CLI is gone: close the open turn, then each turn delivered and never answered."""
        cut = cli.cut()
        if cut:
            self.close_turns([cut], rc)
        state = self.state.read()
        session = cli.session or self.handle.get('session')
        orphans = range(state.ended + 1, state.expected + 1)
        self.close_turns([closed_turn(n, 'incomplete', session=session) for n in orphans], rc)
        last_turn = state.turn if cli.latest_turn is None else cli.latest_turn
        self.record_event(event('progress', last_turn, text='exit rc=%s' % rc,
                                **({'idle': True} if self.left else {})))

    def launch_failed(self, status, reason, why, cli=None):
        """A launch that failed or was refused before any turn: its envelope, and no turn files.
        The spawn caller prints it (rc 2 for a refusal)."""
        rc = 2
        reset = why
        if status != 'refused':   # typed like any failure: the error, the CLI's stderr, its rc
            rc = exit_code(cli.close(2)) if cli else 1
            stderr = read_tail(self.path('stderr.log'), self.stderr_offset)
            status, reset = failure_status(status, why + '\n' + stderr, rc)
            reset = reset or clip(why)
            if self.stopping:   # a stop during the launch ended it, as it ends any turn
                status, rc, reset = 'incomplete', STOP_RC, ''
        self.record_event(event('error', self.first_turn, text=clip('%s: %s' % (reason, why))))
        write_envelope(self.job_dir, status, self.provider, self.handle.get('model'), rc,
                       int(time.time() - self.started_at), reset, None, self.handle.get('effort'),
                       session=self.handle.get('session'), delivery=self.delivery, reason=reason)
        self.ack(0)
        if cli and status == 'refused':   # a failed CLI was closed above, for its rc
            cli.close(2)

    def forget_session(self, session, turn):
        """Delete the stored session of a turn whose agent went away mid-turn (a stop, a crash,
        a kill), so nothing resumes it later: opencode's background service would. It runs
        before the turn's envelope is written, so the one-shot run's stop never races it.
        A turn the deadline ended keeps its session."""
        if self.past_deadline:
            return
        argv = wires.forget_command(self.provider, session, self.handle.get('cli'))
        if not argv:
            return
        with open(self.path('stderr.log'), 'ab') as stderr:
            returncode = subprocess.call(reaper_argv(60, argv), stdin=subprocess.DEVNULL,
                                         stdout=stderr, stderr=stderr, cwd=self.handle['cwd'])
        self.stderr_offset = file_size(self.path('stderr.log'))   # its output types no turn
        self.record_event(event('progress', turn, text='session %s deleted (rc %s): its turn was '
                                'abandoned, so nothing may resume it'
                                % (session, exit_code(returncode))))

    def path(self, *names):
        return os.path.join(self.job_dir, *names)


# ---------------------------------------------------------------------------------------------
# Deliveries. Each message into a seat is marked in events.jsonl before it is sent, under the
# delivery lock, with the turn expected to answer it.


class SeatState:
    """Turn counters and other delivery state, read incrementally from events.jsonl.

    For example, after turn 1 starts, turn and expected are 1 and ended is 0. A queued
    message raises expected to 2. When turn 1 ends, ended becomes 1. When turn 2 starts,
    turn becomes 2. Ending turn 2 brings all three to 2. Deliveries record expected as
    the expect field.
    """

    def __init__(self, job_dir):
        self.path = os.path.join(job_dir, 'events.jsonl')
        self.offset = 0
        self.turn = 0              # the latest turn that started
        self.ended = 0             # the latest turn that ended
        self.expected = 0          # the latest turn a delivery expects
        self.last_event = None     # the time of the latest event
        self.open_calls = set()
        self.mailed = 0            # the latest inbox line delivered: a restart skips it

    def read(self):
        text, self.offset = read_lines(self.path, self.offset)
        for line in map(parse_json, text.split('\n')):   # not splitlines(): it splits at U+2028
            if not isinstance(line, dict):
                continue
            kind = line.get('kind')
            number = line.get('turn') or 0
            self.last_event = line.get('t') or self.last_event
            self.expected = max(self.expected, line.get('expect') or 0)
            self.mailed = max(self.mailed, line.get('inbox') or 0)
            if kind == 'turn_start':   # a turn opens for an input, so it is expected too
                self.turn = max(self.turn, number)
                self.expected = max(self.expected, number)
            elif kind == 'turn_end':
                self.ended = max(self.ended, number)
                self.open_calls = set()
            elif kind == 'tool_call':
                self.open_calls.add(line.get('call'))
            elif kind == 'tool_result':
                self.open_calls.discard(line.get('call'))
        return self

    def next_turn(self, delivery, alive):
        """The turn that will answer a message delivered now.

        Seat          Delivery   Turn
        Relaunch      either     One after the latest started or ended.
        Live          queue      One after the latest started, ended, or expected.
        Live          steer      The latest started or expected if it has yet to end,
                                 otherwise one after the latest ended.
        """
        latest = max(self.turn, self.ended)
        if not alive or delivery == 'queue':
            return max(self.expected if alive else 0, latest) + 1
        running = max(self.expected, self.turn)   # steer: a running turn takes the message
        return running if running > self.ended else self.ended + 1


def delivery_lock(job_dir):
    """Take the delivery lock (on events.jsonl), waiting for it."""
    f = open(os.path.join(job_dir, 'events.jsonl'), 'ab')
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def mark_delivery(job_dir, text, expect, turn, **mail):
    """Log a delivery before its message is sent, so one that dies part way is still typed.

    mail: inbox=N when it delivers line N of the inbox."""
    preview = clip(' '.join(text.split()), 200)
    append_json(os.path.join(job_dir, 'events.jsonl'),
                event('progress', turn, expect=expect, bytes=len(text.encode()),
                      text='input: ' + preview, **mail))


def fifo_record(provider, text):
    """A message as one in.fifo line."""
    return json.dumps(wires.input_record(provider, text), ensure_ascii=False).encode()


def deliver(job_dir, provider, fd, text, expect, turn):
    """A send: mark it, then write it to in.fifo, framed as newline, record, newline. The torn
    bytes of a sender killed part way end at the next record's first newline."""
    mark_delivery(job_dir, text, expect, turn)
    data = b'\n' + fifo_record(provider, text) + b'\n'
    while data:
        data = data[os.write(fd, data):]


def mail_text(line):
    """An inbox line (the driver's `hands.sh mail`) as the seat reads it."""
    return 'MAIL from driver:\n%s' % line.get('text')


def whole_records(job_dir, lines, state):
    """in.fifo lines -> the whole records among them. A torn line is dropped and logged, under
    the latest turn that started (state: the seat's SeatState)."""
    records = []
    for line in lines:
        if not line.strip():
            continue
        if isinstance(parse_json(line.decode('utf-8', 'replace')), dict):
            records.append(line)
            continue
        torn = clip(line.decode('utf-8', 'replace'), 120)
        append_json(os.path.join(job_dir, 'events.jsonl'),
                    event('progress', state.read().turn,
                          text='torn input dropped: ' + torn))
    return records


# ---------------------------------------------------------------------------------------------
# Failure types. They come from a provider's error fields and the CLI's stderr, never from what
# the model wrote: a model that quotes an error message does not fail its turn.

def failure_status(status, text, rc):
    """A failed turn or launch -> (status, reset). The deadline's rc is a timeout."""
    found, reset = ('timeout', '') if rc == DEADLINE_RC else classify(text)
    return (status if found == 'error' else found), reset


# ---------------------------------------------------------------------------------------------
# Envelopes, and what the commands print


def one_line(value):
    """The compact one-line JSON every command prints."""
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def printed(envelope):
    """An envelope as a command prints it, its usage cut to PRINTED_USAGE."""
    usage = envelope.get('usage')
    if isinstance(usage, dict):
        envelope = dict(envelope, usage={k: usage[k] for k in PRINTED_USAGE if k in usage})
    return one_line(envelope)


def write_envelope(job_dir, status, provider, model, rc, seconds, reset, usage, effort, **more):
    """Write DIR/envelope.json, and turn-N/envelope.json too when it is a turn's."""
    envelope = {'status': status, 'provider': provider, 'model': model or None,
                'effort': effort or None, 'rc': rc, 'seconds': seconds, 'reset': reset or None,
                'dir': job_dir, 'usage': usage}
    envelope.update((key, value) for key, value in more.items() if value is not None)
    text = json.dumps(envelope, separators=(',', ':'), ensure_ascii=False) + '\n'
    if envelope.get('turn'):
        write_atomic(os.path.join(job_dir, 'turn-%d' % envelope['turn'], 'envelope.json'), text)
    write_atomic(os.path.join(job_dir, 'envelope.json'), text)


# ---------------------------------------------------------------------------------------------
# Processes. A wrapper is found by its command line (seat.py spawn DIR), and a seat's CLI by
# the process groups below its wrapper.


def process_table():
    """Every process -> [(pid, parent pid, group id, command line)]."""
    out = subprocess.run(['ps', '-A', '-ww', '-o', 'pid=,ppid=,pgid=,command='],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            rows.append((int(fields[0]), int(fields[1]), int(fields[2]), fields[3]))
    return rows


def wrappers(job_dir, rows=None):
    """The live wrapper processes of a job dir (normally one)."""
    key = ' spawn %s ' % job_dir
    return [row for row in rows or process_table()
            if 'seat.py' in row[3] and key in row[3] + ' ' and row[0] != os.getpid()]


def live_seats():
    """The job dir of every live seat on this machine, found by its wrapper's command line
    (seat.py spawn DIR FILE), so a seat started with --out is found too."""
    found = set()
    for command in (row[3] for row in process_table()):
        if 'seat.py' not in command or ' spawn ' not in command:
            continue
        words = command.split(' spawn ', 1)[1].split(' ')
        for count in range(1, len(words)):   # the dir may hold spaces; the message file follows
            job_dir = ' '.join(words[:count])
            if os.path.isfile(os.path.join(job_dir, 'session')):
                found.add(job_dir)
                break
    return sorted(found)


def groups_under(roots, rows=None):
    """Every process group below the root processes, except their own groups and the caller's."""
    rows = rows or process_table()
    children = {}
    skip = {os.getpgid(0), 0, 1}
    for row in rows:
        children.setdefault(row[1], []).append(row)
        if row[0] in roots:
            skip.add(row[2])
    groups = set()
    todo = list(roots)
    while todo:
        for row in children.get(todo.pop(), []):
            todo.append(row[0])
            if row[2] not in skip:
                groups.add(row[2])
    return groups


def signal_groups(groups, sig=signal.SIGTERM):
    for group in groups:
        try:
            os.killpg(group, sig)
        except OSError:
            pass


def seconds(text):
    """A count of seconds -> that number; 0 when it is empty, not a number, not finite or
    negative."""
    try:
        value = float(text)
    except ValueError:
        return 0
    return value if math.isfinite(value) and value >= 0 else 0


def reap(seconds, argv):
    """Run argv as the leader of its own process group, for at most `seconds` (0: no limit).

    However argv ends (by itself, at the limit, or because this process got INT, TERM or HUP),
    the rest of its group is ended after it: TERM, a short grace, then KILL. So a CLI that runs
    its real binary as a child, or leaves a helper running, leaves nothing behind. Exits 142 at
    the limit, 128 + N when signal N ended argv or this process, else with argv's status."""
    try:
        child = subprocess.Popen(argv, preexec_fn=os.setpgrp)   # Popen also restores SIGPIPE
    except OSError:
        return 127
    timed_out = False

    def end_group(grace):
        signal_groups([child.pid])
        give_up = time.time() + grace
        while time.time() < give_up:
            child.poll()   # an exited leader counts as in its group until it is waited for
            try:
                os.killpg(child.pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        signal_groups([child.pid], signal.SIGKILL)

    def on_signal(sig, frame):
        end_group(2)
        sys.exit(128 + sig)

    def on_alarm(sig, frame):   # the limit: TERM the group, then KILL it every 5 s until it ends
        nonlocal timed_out
        signal_groups([child.pid], signal.SIGKILL if timed_out else signal.SIGTERM)
        timed_out = True
        signal.alarm(5)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) != signal.SIG_IGN:   # a signal the caller ignores (nohup's HUP)
            signal.signal(sig, on_signal)             # stays ignored
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    status = child.wait()
    signal.alarm(0)
    end_group(4 if timed_out else 2)
    if timed_out:
        return DEADLINE_RC
    return 128 - status if status < 0 else status


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
