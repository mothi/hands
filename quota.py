#!/usr/bin/env python3
"""quota -- how much of each provider's quota is used, and when it resets.

    hands.sh quota [--json] [claude|codex|grok|opencode ...]

One line per provider (all four by default), or one JSON document with --json. It only reads:
it never spends quota, never starts a seat, and always exits 0. A provider it cannot read is
shown as unknown, with the reason.

Where each reading comes from:
  claude    the newest rate_limit_event in a claude seat's stream.jsonl in the jobs root. No
            credential, no endpoint. The line says how old the reading is; hands.sh --probe
            claude takes a fresh one.
  codex     the codex CLI's own app-server, asked account/rateLimits/read. No credential.
  grok      cli-chat-proxy.grok.com/v1/billing, with the token from ~/.grok/auth.json
  opencode  opencode.ai/zen/go/v1/usage (the Go plan), with the key from opencode's auth.json

The grok and opencode endpoints are undocumented and may change. Their token goes to curl on
stdin, never on its command line. Raw replies are never printed or stored: only the numbers
below leave this file, and a provider's error text is masked before it is shown.
"""
import datetime, glob, json, os, re, signal, subprocess, sys, threading, time

from seat import JOBS

POOLS = ('claude', 'codex', 'grok', 'opencode')
REFRESH = 'refresh: hands.sh --probe claude'


class Unknown(Exception):
    """A pool that cannot be read; the message says why."""


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def window(name, used, resets):
    """One usage window, or None when the reply has no percentage for it (never a made-up 0)."""
    if used is None:
        return None
    return {'name': name, 'used_pct': used, 'resets_at': resets}


def span(minutes):
    """A window's length as a label: 300 -> 5h, 10080 -> week, 1440 -> 1d."""
    if minutes == 10080:
        return 'week'
    return '%dh' % (minutes // 60) if minutes < 1440 else '%dd' % (minutes // 1440)


def parse(body):
    """The reply as a JSON object, checked."""
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise Unknown('the reply is not JSON')
    return checked(reply)


def checked(reply):
    """reply, unless it is an error: then Unknown, carrying the provider's own message."""
    error = reply.get('error')
    if isinstance(error, dict):
        error = next((v for v in (error.get('message'), error.get('type'))
                      if isinstance(v, str) and v), 'the provider answered with an error')
    if error:
        raise Unknown(error if isinstance(error, str) else 'the provider answered with an error')
    return reply


def curl(url, token, *headers):
    """GET url with a bearer token. The token goes to curl on stdin: ps shows argv to every user."""
    argv = ['curl', '-sS', '--max-time', '20', url, '-H', '@-']
    for header in headers:
        argv += ['-H', header]
    try:
        done = subprocess.run(argv, input='Authorization: Bearer %s\n' % token,
                              capture_output=True, text=True)
    except OSError:
        raise Unknown('no curl on PATH')
    if done.returncode:
        raise Unknown('the request failed (curl exit %d)' % done.returncode)
    return done.stdout


def login(path, find):
    """The token find() picks out of the CLI's own JSON login file at path."""
    try:
        with open(path) as f:
            value = find(json.load(f))
    except Exception:
        value = None
    if not isinstance(value, str) or not value:
        raise Unknown('no login in %s' % path)
    return value


def read_claude():
    """The newest rate_limit_event in the claude job streams, with its age.

    A window that has reset since the event was written is dropped: its number is no longer true.
    five_hour and seven_day are the account's windows. Any other key (fable streams carry
    seven_day_overage_included) limits one model, so it is listed apart as a model limit."""
    now = time.time()
    streams = []
    for path in glob.glob(os.path.join(JOBS, '*', 'stream.jsonl')):
        try:
            streams.append((os.stat(path).st_mtime, path))
        except OSError:
            pass
    for written, path in sorted(streams, reverse=True):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            continue
        if b'unifiedWindows' not in data:   # most streams hold no reading: skip the parse
            continue
        for line in reversed(data.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Only a line's own top-level object counts: a seat quoting an event is text
            info = event.get('rate_limit_info') if isinstance(event, dict) else None
            if isinstance(info, dict) and isinstance(info.get('unifiedWindows'), dict):
                return claude_windows(info['unifiedWindows'], int(now - written), now)
    raise Unknown('no claude reading in the jobs root; ' + REFRESH)


def claude_windows(windows, age, now):
    names = {'seven_day': 'week', 'five_hour': '5h'}
    reset, found, model_limits = [], [], []
    for key, w in windows.items():
        if not isinstance(w, dict):
            continue
        name = names.get(key, key[:40])
        resets = w.get('resetsAt')
        if isinstance(resets, (int, float)) and resets <= now:
            reset.append(name)
            continue
        used = w.get('utilization')
        used = None if used is None else round(used * 100, 1)
        resets = iso(resets) if isinstance(resets, (int, float)) else None
        if key in names:
            found.append(window(name, used, resets))
        elif used is not None:
            model_limits.append({'name': name, 'used_pct': used, 'resets_at': resets})
    found = sorted((w for w in found if w), key=lambda w: w['name'] == '5h')   # week first
    if not found and reset:
        raise Unknown('out of date: %s reset since the reading; %s' % (', '.join(reset), REFRESH))
    reading = {'windows': found, 'age_s': age}
    if reset:
        reading['reset_since'] = sorted(reset)
    if model_limits:
        reading['model_limits'] = model_limits
    return reading


def read_codex():
    """One account/rateLimits/read to the codex CLI's own app-server, which keeps its own login.

    stdin stays open until the answer (the app-server drops a read in flight at stdin EOF). The
    CLI runs in its own process group, and one 20 s deadline, its exit included, kills the
    group."""
    try:
        cli = subprocess.Popen(['codex', '--no-daemon', 'app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    except OSError:
        raise Unknown('no codex CLI on PATH')
    deadline = threading.Timer(20, kill_group, (cli.pid,))
    deadline.daemon = True
    deadline.start()
    try:
        for message in ({'id': 1, 'method': 'initialize',
                         'params': {'clientInfo': {'name': 'hands-quota', 'version': '1'}}},
                        {'method': 'initialized'},
                        {'id': 2, 'method': 'account/rateLimits/read'}):
            cli.stdin.write((json.dumps(message) + '\n').encode())
        cli.stdin.flush()
    except OSError:
        pass
    answer = {}
    for line in cli.stdout:
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get('id') == 2:
            break
        answer = {}
    try:
        cli.stdin.close()
    except OSError:
        pass
    cli.wait()
    deadline.cancel()
    if 'result' not in answer and not answer.get('error'):
        why = 'killed at the 20 s deadline' if cli.returncode == -9 else 'rc %s' % cli.returncode
        raise Unknown('codex app-server gave no answer (%s)' % why)
    reply = checked(answer.get('result', answer))
    found = []
    for key in ('primary', 'secondary'):   # these have swapped meanings: go by the length
        w = (reply.get('rateLimits') or {}).get(key)
        if isinstance(w, dict):
            minutes, resets = w.get('windowDurationMins'), w.get('resetsAt')
            name = span(minutes) if isinstance(minutes, int) else key
            resets = iso(resets) if isinstance(resets, (int, float)) else None
            found.append((minutes if isinstance(minutes, int) else 0,
                          window(name, w.get('usedPercent'), resets)))
    return {'windows': [w for _, w in sorted(found, key=lambda f: -f[0]) if w]}


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def read_grok():
    path = os.path.expanduser('~/.grok/auth.json')
    key = login(path, lambda auth: next(iter(auth.values()))['key'])
    reply = parse(curl('https://cli-chat-proxy.grok.com/v1/billing?format=credits', key,
                       'x-grok-client-mode: build'))
    config = reply.get('config') or {}
    used = config.get('creditUsagePercent')
    # The gateway leaves out a zero value: a new billing period has no creditUsagePercent at all.
    # That reads as 0 only while the period's end is there to show the reply is a real one.
    if used is None and config.get('billingPeriodEnd'):
        used = 0.0
    return {'windows': [w for w in [window('week', used, config.get('billingPeriodEnd'))] if w]}


def read_opencode():
    data = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    path = os.path.join(data, 'opencode', 'auth.json')
    key = login(path, lambda auth: auth['opencode-go']['key'])
    usage = parse(curl('https://opencode.ai/zen/go/v1/usage', key)).get('usage') or {}
    found = []
    for key, name in (('monthly', 'month'), ('weekly', 'week'), ('rolling', 'rolling')):
        w = usage.get(key)
        if isinstance(w, dict):
            found.append(window(name, w.get('percent'), w.get('resetsAt')))
    return {'windows': [w for w in found if w]}


READERS = {'claude': read_claude, 'codex': read_codex, 'grok': read_grok, 'opencode': read_opencode}


def mask(text):
    """Hide anything that looks like a credential (sk-..., or any run of 40+ token characters)."""
    return re.sub(r'sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{40,}', '…', text)[:200]


def read(pool):
    """One pool's reading. Any failure, a crash on an odd reply included, makes only it unknown."""
    try:
        reading = READERS[pool]()
        if not reading['windows']:
            raise Unknown('the reply has no usage windows')
        return dict({'pool': pool, 'status': 'ok', 'why': None}, **reading)
    except Unknown as e:
        why = mask(str(e))
    except Exception:
        why = 'the reply has an unexpected shape'
    return {'pool': pool, 'status': 'unknown', 'why': why, 'windows': []}


def number(pct):
    return '%g%%' % pct


def until(resets_at):
    """'resets in 3d' for an ISO time in the future, else ''."""
    try:
        when = datetime.datetime.fromisoformat(resets_at.replace('Z', '+00:00'))
        left = int(when.timestamp() - time.time())
    except (AttributeError, TypeError, ValueError):
        return ''
    if left <= 0:
        return ''
    if left >= 86400:
        return ' (resets in %dd)' % (left // 86400)
    if left >= 3600:
        return ' (resets in %dh)' % (left // 3600)
    return ' (resets in %dm)' % (left // 60)


def ago(seconds):
    if seconds < 120:
        return '%ds' % seconds
    return '%dm' % (seconds // 60) if seconds < 7200 else '%dh' % (seconds // 3600)


def line(reading):
    """One board line: the pool, each window longest first, then how old the reading is."""
    if reading['status'] != 'ok':
        return '%-9s unknown: %s' % (reading['pool'], reading['why'])
    parts = ['%s %s%s' % (w['name'], number(w['used_pct']), until(w['resets_at']))
             for w in reading['windows']]
    parts += ['%s %s (one model)' % (m['name'], number(m['used_pct']))
              for m in reading.get('model_limits', [])]
    text = '%-9s %s' % (reading['pool'], ', '.join(parts))
    if 'age_s' in reading:
        reset = reading.get('reset_since')
        text += '; read %s ago%s; %s' % (ago(reading['age_s']),
                                         ', before the %s reset' % '+'.join(reset) if reset else '',
                                         REFRESH)
    return text


def main(args):
    pools = [a for a in args if a in POOLS] or list(POOLS)
    for arg in args:
        if arg in ('-h', '--help'):
            print(__doc__.strip())
            return 0
        if arg != '--json' and arg not in POOLS:
            print('quota: unknown argument %s (ignored; see --help)' % arg, file=sys.stderr)
    readings = [read(pool) for pool in pools]
    if '--json' in args:
        print(json.dumps({'read_at': iso(time.time()), 'pools': readings}, indent=1))
    else:
        for reading in readings:
            print(line(reading))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
