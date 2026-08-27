# SPDX-License-Identifier: GPL-2.0-only
"""Verify the guardian really does survive a SIGKILL of its parent.

The claim btkey makes is that the machine is not left without a bluetoothd
no matter how btkey dies.  That is only worth anything if it is tested
against the one signal that cannot be caught, so these tests kill the
parent with SIGKILL and watch what the guardian does afterwards.

A `sleep` process stands in for the private bluetoothd: the guardian is
asked to kill it, which is observable from here without touching anything
system-wide.  Liveness is checked with Popen.poll() rather than kill(pid, 0),
since a signalled child stays visible as a zombie until it is reaped.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import guardian


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class GuardianTest(unittest.TestCase):
    def victim(self):
        """A process the guardian can be asked to kill."""
        process = subprocess.Popen(["sleep", "300"])
        self.addCleanup(self._reap, process)
        return process

    def _reap(self, process):
        if process.poll() is None:
            process.kill()
        process.wait()

    def _run_child(self, body):
        pid = os.fork()
        if pid == 0:
            try:
                body()
            finally:
                os._exit(1)
        os.waitpid(pid, 0)

    def test_kills_the_child_after_sigkill(self):
        victim = self.victim()

        def child():
            keeper = guardian.spawn()
            keeper.kill_on_death(victim.pid, "sleep")
            os.kill(os.getpid(), signal.SIGKILL)

        self._run_child(child)
        self.assertTrue(
            wait_for(lambda: victim.poll() is not None),
            "guardian did not clean up after the parent was killed")

    def test_dismissed_guardian_does_nothing(self):
        victim = self.victim()

        def child():
            keeper = guardian.spawn()
            keeper.kill_on_death(victim.pid, "sleep")
            keeper.dismiss()
            os._exit(0)

        self._run_child(child)
        time.sleep(0.5)
        self.assertIsNone(victim.poll(),
                          "a dismissed guardian must not act")

    def test_pid_reuse_is_not_mistaken_for_our_child(self):
        """A recycled PID must not be killed just because it now exists."""
        victim = self.victim()

        def child():
            keeper = guardian.spawn()
            keeper.kill_on_death(victim.pid, "bluetoothd")   # wrong comm
            os.kill(os.getpid(), signal.SIGKILL)

        self._run_child(child)
        time.sleep(0.5)
        self.assertIsNone(victim.poll(),
                          "guardian killed a PID whose comm did not match")

    def test_survives_a_kill_of_the_process_group(self):
        victim = self.victim()

        def child():
            os.setpgid(0, 0)
            keeper = guardian.spawn()
            keeper.kill_on_death(victim.pid, "sleep")
            os.killpg(os.getpgrp(), signal.SIGKILL)

        self._run_child(child)
        self.assertTrue(
            wait_for(lambda: victim.poll() is not None),
            "guardian died along with its process group")



class WatchdogTest(unittest.TestCase):
    """A hung btkey holds its keyboard grabs; only killing it lets go."""

    def test_silent_parent_is_killed(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)

        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                keeper = guardian.spawn()
                keeper.watch_me(1)
                os.write(write_fd, b"up\n")
                time.sleep(60)          # wedge, exactly as a deadlock would
            finally:
                os._exit(0)
        os.close(write_fd)
        self.assertEqual(os.read(read_fd, 3), b"up\n")

        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            self.fail("guardian did not kill the wedged parent")
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)

    def test_heartbeats_keep_the_parent_alive(self):
        pid = os.fork()
        if pid == 0:
            try:
                keeper = guardian.spawn()
                keeper.watch_me(1)
                for _ in range(20):     # 2s of healthy beating
                    keeper.heartbeat()
                    time.sleep(0.1)
                keeper.dismiss()
            finally:
                os._exit(7)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status), "watchdog fired spuriously")
        self.assertEqual(os.WEXITSTATUS(status), 7)


class ConsoleResetTest(unittest.TestCase):
    """The scrolling region belongs to the VT and outlives us.

    Nothing in the kernel undoes DECSTBM, so a console left with a reserved
    bottom line stays that way after btkey is gone - and the pty our stdout
    pointed at is gone too, which is why this goes via the device.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.original = guardian.CONSOLE_DEVICE
        guardian.CONSOLE_DEVICE = os.path.join(self.directory, "tty%d")
        self.addCleanup(self.restore)
        self.path = guardian.CONSOLE_DEVICE % 4
        open(self.path, "w").close()

    def restore(self):
        guardian.CONSOLE_DEVICE = self.original

    def test_resets_the_scrolling_region(self):
        guardian._cleanup([], [], [4])
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"\033[r")

    def test_an_unreachable_console_does_not_stop_the_rest(self):
        # The guardian runs once, on the way out, with nobody left to
        # notice if it gives up half way: one console it cannot open must
        # not cost the others their scrolling region.
        guardian._cleanup([], [], [99, 4])    # 99: no such device
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"\033[r")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
