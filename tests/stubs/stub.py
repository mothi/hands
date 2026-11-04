"""What the stub CLIs share: their settings and their log.

The stubs stand in for the real claude, codex, grok and opencode CLIs. Each one is set up by
STUB_* environment variables, and each run appends JSON lines to the file named by $STUB_LOG:

  launch   argv, cwd, pid, the names of every environment variable, the values of the few in SHOWN
  in       one line the stub read on its stdin
  eof      its stdin closed
  term     it got a TERM
  child    the pid of a process it started
  parked   its turn has started and will not end by itself

tests/harness.py reads the log back (StubLog).
"""
import json
import os
import sys

NAME = os.path.basename(sys.argv[0])
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'fixtures')

# Values the tests check. Every other variable is logged by name only.
SHOWN = (
    'XDG_DATA_HOME',
    'GROK_DISABLE_AUTOUPDATER',
    'GROK_CLAUDE_HOOKS_ENABLED',
    'GROK_CURSOR_HOOKS_ENABLED',
    'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS',
    'CLAUDE_CODE_DISABLE_AUTO_MEMORY',
)


def setting(name, default=None):
    """STUB_<name>, or default when it is unset or empty."""
    return os.environ.get('STUB_' + name) or default


def log(event, **fields):
    path = os.environ.get('STUB_LOG')
    if not path:
        return
    record = dict(cli=NAME, pid=os.getpid(), event=event, **fields)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record) + '\n').encode())   # one write: lines never interleave
    finally:
        os.close(fd)


def log_launch():
    values = {name: os.environ[name] for name in SHOWN if name in os.environ}
    log('launch', argv=sys.argv[1:], cwd=os.getcwd(), env=sorted(os.environ), values=values)


def launch_number():
    """How many times this CLI has been launched under this log, this run included."""
    path = os.environ.get('STUB_LOG')
    if not path or not os.path.exists(path):
        return 1
    with open(path) as f:
        records = [json.loads(line) for line in f if line.strip()]
    return sum(r['event'] == 'launch' and r['cli'] == NAME for r in records)


def write_line(message):
    """One line on stdout: a dict as JSON, a string as it is."""
    text = message if isinstance(message, str) else json.dumps(message)
    sys.stdout.write(text + '\n')
    sys.stdout.flush()
