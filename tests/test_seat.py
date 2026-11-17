#!/usr/bin/env python3
"""Reaping, atomic writes, driver isolation and provider failure text."""
import os
import signal
import subprocess
import sys
import time
import unittest

import harness
from harness import envelope, read_text, wait_until

import wires

CODEX = harness.SPECS['codex']
SB = None


def setUpModule():
    global SB
    SB = harness.SeatSandbox('seat')


def tearDownModule():
    SB.close()


class Reaper(unittest.TestCase):
    def test_a_timeout_kills_the_whole_process_group(self):
        shim = SB.path('shim-bin')
        os.makedirs(shim)
        pidfile = SB.path('grandchild.pid')
        # the real work is a child it waits on; both outlast every bound, so only a kill ends them
        path = SB.write('shim-bin/codex', '#!/bin/sh\n'
                        'sh -c \'echo $$ > "$T71_PID"; exec sleep 300\' &\n'
                        'cat >/dev/null\nsleep 300\n')
        os.chmod(path, 0o755)
        job, _ = SB.one_shot('reaper-timeout', CODEX, '--timeout', '1', T71_PID=pidfile,
                             PATH=shim + os.pathsep + SB.env['PATH'])
        grandchild = int(read_text(pidfile))
        try:
            self.assertEqual((envelope(job)['status'], envelope(job)['rc']), ('timeout', 142))
            self.assertTrue(wait_until(lambda: not harness.alive(grandchild), timeout=3))
        finally:
            harness.kill(grandchild)

    def reap(self, seconds, interrupt):
        """`seat.py reap` with SIGINT as `interrupt` (a foreground shell's default, or nohup's
        ignore), its child's pid, and the INT sent to the reaper."""
        pidfile = SB.path('reap-%s.pid' % interrupt)
        trampoline = ('import os, signal, sys; signal.signal(signal.SIGINT, signal.%s); '
                      'os.execvp(sys.argv[1], sys.argv[1:])' % interrupt)
        reaper = subprocess.Popen([sys.executable, '-c', trampoline, sys.executable,
                                   os.path.join(harness.ROOT, 'seat.py'), 'reap', str(seconds),
                                   'sh', '-c', 'echo $$ > "$1"; exec sleep 30', 'sh', pidfile],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_until(lambda: os.path.getsize(pidfile)))
        child = int(read_text(pidfile))
        os.kill(reaper.pid, signal.SIGINT)
        return reaper, child

    def test_an_interrupt_reaches_the_seats_group(self):
        reaper, child = self.reap(30, 'SIG_DFL')
        self.assertEqual(reaper.wait(timeout=10), 130)
        self.assertTrue(wait_until(lambda: not harness.alive(child), timeout=3))

    def test_an_interrupt_the_caller_ignored_never_reaches_the_seat(self):
        reaper, child = self.reap(4, 'SIG_IGN')
        time.sleep(0.5)
        self.assertTrue(harness.alive(child))   # nohup holds: only the seat's own alarm ends it
        self.assertEqual(reaper.wait(timeout=10), 142)


class AtomicWrite(unittest.TestCase):
    def test_a_link_planted_as_the_temporary_file_is_never_written_through(self):
        victim = SB.write('atomic-victim', 'keep')
        path = SB.path('atomic-target')
        os.symlink(victim, '%s.tmp%d' % (path, os.getpid()))
        wires.write_atomic(path, 'new')
        self.assertEqual((read_text(victim), read_text(path)), ('keep', 'new'))
        self.assertFalse(os.path.islink(path))


class Hermetic(unittest.TestCase):
    def test_every_captured_driver_is_one_this_suite_made(self):
        job, run = SB.one_shot('hermetic', harness.SPECS['claude'], CODEX_THREAD_ID='test-hermetic')
        self.assertEqual(run.rc, 0, run.err)
        self.assertTrue(os.path.isfile(os.path.join(job, 'driver')))
        SB.check_drivers()


class FailureText(unittest.TestCase):
    def classify(self, fixture):
        return wires.classify(read_text(os.path.join(harness.FIXTURES, fixture)))

    def test_usage_limits_are_provider_quota_dead_with_their_reset(self):
        for name in ('claude-session-limit', 'codex-usage-limit'):
            self.assertEqual(self.classify(name + '.txt')[0], 'provider-quota-dead', name)
        self.assertIn('resets 6:20am', self.classify('claude-session-limit.txt')[1].lower())
        self.assertIn('again at aug', self.classify('codex-usage-limit.txt')[1].lower())

    def test_api_key_billing_is_provider_quota_dead_with_no_reset(self):
        for name in ('claude-api-credit-low', 'codex-api-insufficient-quota',
                     'opencode-openrouter-402'):
            self.assertEqual(self.classify(name + '.txt'), ('provider-quota-dead', ''), name)

    def test_logins_are_auth_expired(self):
        for name in ('claude-oauth-expired', 'opencode-go-no-credential'):
            self.assertEqual(self.classify(name + '.txt')[0], 'auth-expired', name)

    def test_other_text_is_an_error(self):
        self.assertEqual(wires.classify('plain unrelated failure text')[0], 'error')


if __name__ == '__main__':
    harness.main('test_seat')
