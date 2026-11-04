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
import datetime
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import wires
from seat import JOBS

POOLS = tuple(wires.PROVIDERS)
REFRESH = 'refresh: hands.sh --probe claude'


class Unknown(Exception):
    """A pool that cannot be read; the message says why."""


def iso_time(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def window(name, used, resets):
    """One usage window, or None when the reply has no percentage for it (never a made-up 0)."""
    if used is None:
        return None
    return {'name': name, 'used_pct': used, 'resets_at': resets}


def window_label(minutes):
    """A window's length as a label: 300 -> 5h, 1440 -> 1d, 10080 -> week."""
    if minutes == 10080:
        return 'week'
    if minutes < 1440:
        return '%dh' % (minutes // 60)
    return '%dd' % (minutes // 1440)


def parse_reply(body):
    """The reply as a JSON object, checked for an error."""
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise Unknown('the reply is not JSON')
    return raise_if_error(reply)


def raise_if_error(reply):
    """The reply, unless it is an error: then Unknown, with the provider's own message."""
    error = reply.get('error')
    if isinstance(error, dict):
        messages = (error.get('message'), error.get('type'))
        error = next((m for m in messages if isinstance(m, str) and m),
                     'the provider answered with an error')
    if error:
        raise Unknown(error if isinstance(error, str) else 'the provider answered with an error')
    return reply


def fetch(url, token, *headers):
    """GET url with a bearer token. The token goes to curl on stdin: ps shows argv to all."""
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


def read_login(path, find):
    """The token find() picks out of a CLI's own JSON login file."""
    try:
        with open(path) as f:
            value = find(json.load(f))
    except Exception:
        value = None
    if not isinstance(value, str) or not value:
        raise Unknown('no login in %s' % path)
    return value


# ---------------------------------------------------------------------------------------------
# One reader per provider


def read_claude():
    """The newest rate_limit_event in the claude job streams, with its age."""
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
            # Only a line's own top-level object counts: a seat that quotes an event is text.
            info = event.get('rate_limit_info') if isinstance(event, dict) else None
            if isinstance(info, dict) and isinstance(info.get('unifiedWindows'), dict):
                return claude_windows(info['unifiedWindows'], int(now - written), now)
    raise Unknown('no claude reading in the jobs root; ' + REFRESH)


def claude_windows(windows, age, now):
    """The reading's windows. One that has reset since the reading is dropped: its number is no
    longer true. five_hour and seven_day are the account's windows; any other key (fable's
    seven_day_overage_included) limits one model, and is listed apart as a model limit."""
    names = {'seven_day': 'week', 'five_hour': '5h'}
    reset = []
    found = []
    model_limits = []
    for key, value in windows.items():
        if not isinstance(value, dict):
            continue
        name = names.get(key, key[:40])
        resets = value.get('resetsAt')
        if isinstance(resets, (int, float)) and resets <= now:
            reset.append(name)
            continue
        used = value.get('utilization')
        used = None if used is None else round(used * 100, 1)
        resets = iso_time(resets) if isinstance(resets, (int, float)) else None
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

    stdin stays open until the answer: the app-server drops a read in flight when stdin ends.
    The CLI runs in its own process group, and one 20 s deadline, its exit included, kills
    the group."""
    try:
        cli = subprocess.Popen(['codex', '--no-daemon', 'app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    except OSError:
        raise Unknown('no codex CLI on PATH')
    deadline = threading.Timer(20, kill_group, (cli.pid,))
    deadline.daemon = True
    deadline.start()
    hello = {'clientInfo': {'name': 'hands-quota', 'version': '1'}}
    messages = ({'id': 1, 'method': 'initialize', 'params': hello},
                {'method': 'initialized'},
                {'id': 2, 'method': 'account/rateLimits/read'})
    try:
        for message in messages:
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
        if cli.returncode == -9:
            why = 'killed at the 20 s deadline'
        else:
            why = 'rc %s' % cli.returncode
        raise Unknown('codex app-server gave no answer (%s)' % why)
    reply = raise_if_error(answer.get('result', answer))
    found = []
    for key in ('primary', 'secondary'):   # their meanings have swapped: go by the length
        value = (reply.get('rateLimits') or {}).get(key)
        if not isinstance(value, dict):
            continue
        minutes = value.get('windowDurationMins')
        resets = value.get('resetsAt')
        name = window_label(minutes) if isinstance(minutes, int) else key
        resets = iso_time(resets) if isinstance(resets, (int, float)) else None
        length = minutes if isinstance(minutes, int) else 0
        found.append((length, window(name, value.get('usedPercent'), resets)))
    return {'windows': [w for _, w in sorted(found, key=lambda f: -f[0]) if w]}


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def read_grok():
    path = os.path.expanduser('~/.grok/auth.json')
    key = read_login(path, lambda auth: next(iter(auth.values()))['key'])
    reply = parse_reply(fetch('https://cli-chat-proxy.grok.com/v1/billing?format=credits', key,
                              'x-grok-client-mode: build'))
    config = reply.get('config') or {}
    used = config.get('creditUsagePercent')
    # The gateway leaves out a zero, so a new billing period has no creditUsagePercent at all.
    # That reads as 0 only while the period's end is there to show the reply is a real one.
    if used is None and config.get('billingPeriodEnd'):
        used = 0.0
    found = window('week', used, config.get('billingPeriodEnd'))
    return {'windows': [found] if found else []}


def read_opencode():
    data = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    path = os.path.join(data, 'opencode', 'auth.json')
    key = read_login(path, lambda auth: auth['opencode-go']['key'])
    usage = parse_reply(fetch('https://opencode.ai/zen/go/v1/usage', key)).get('usage') or {}
    found = []
    for field, name in (('monthly', 'month'), ('weekly', 'week'), ('rolling', 'rolling')):
        value = usage.get(field)
        if isinstance(value, dict):
            found.append(window(name, value.get('percent'), value.get('resetsAt')))
    return {'windows': [w for w in found if w]}


READERS = {
    'claude': read_claude,
    'codex': read_codex,
    'grok': read_grok,
    'opencode': read_opencode,
}


# ---------------------------------------------------------------------------------------------
# The board


def mask(text):
    """Hide anything that looks like a credential (sk-..., or a run of 40+ token characters)."""
    return re.sub(r'sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{40,}', '…', text)[:200]


def read(pool):
    """One pool's reading. Any failure, even a crash on an odd reply, makes only it unknown."""
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


def percent(value):
    return '%g%%' % value


def resets_in(resets_at):
    """' (resets in 3d)' for an ISO time in the future, else ''."""
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
    if seconds < 7200:
        return '%dm' % (seconds // 60)
    return '%dh' % (seconds // 3600)


def board_line(reading):
    """One board line: the pool, each window longest first, then how old the reading is."""
    if reading['status'] != 'ok':
        return '%-9s unknown: %s' % (reading['pool'], reading['why'])
    parts = ['%s %s%s' % (w['name'], percent(w['used_pct']), resets_in(w['resets_at']))
             for w in reading['windows']]
    parts += ['%s %s (one model)' % (m['name'], percent(m['used_pct']))
              for m in reading.get('model_limits', [])]
    text = '%-9s %s' % (reading['pool'], ', '.join(parts))
    if 'age_s' in reading:
        reset = reading.get('reset_since')
        before = ', before the %s reset' % '+'.join(reset) if reset else ''
        text += '; read %s ago%s; %s' % (ago(reading['age_s']), before, REFRESH)
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
        print(json.dumps({'read_at': iso_time(time.time()), 'pools': readings}, indent=1))
    else:
        for reading in readings:
            print(board_line(reading))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
