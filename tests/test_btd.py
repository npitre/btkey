#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The private bluetoothd, and giving the system one back.

Nothing here starts a real daemon; what is checked is the bookkeeping
around it, because the failures are all of the same shape: the machine is
left with no bluetoothd at all.  That is worse than btkey not working -
it takes the braille display and every other Bluetooth device with it, and
the person it happens to is the one who can least easily go and look.

This module had no tests, and a commit removing something else took
_noplugin() away with it while leaving both calls in place.  Every run of
the private daemon died on an AttributeError from then on.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import btd


class FakeProcess:
    """A bluetoothd child that responds however the test wants it to."""

    def __init__(self, terminate_hangs=False, kill_hangs=False, alive=True):
        self.pid = 4242
        self.returncode = None if alive else 1
        self.terminate_hangs = terminate_hangs
        self.kill_hangs = kill_hangs
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.killed:
            if self.kill_hangs:
                raise subprocess.TimeoutExpired("bluetoothd", timeout)
            return 0
        if self.terminate_hangs:
            raise subprocess.TimeoutExpired("bluetoothd", timeout)
        return 0

    def poll(self):
        return self.returncode


class BtdTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.events = []
        self.saved = btd._systemctl
        btd._systemctl = self.systemctl
        self.addCleanup(self.restore)

    def restore(self):
        btd._systemctl = self.saved

    def systemctl(self, *args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    def make(self, audio=True, process=None, unit_was_active=True):
        daemon = btd.ManagedBluetoothd(0x000540, on_event=self.events.append,
                                       audio=audio)
        daemon.process = process
        daemon.unit_was_active = unit_was_active
        return daemon


class NoPluginTest(BtdTest):
    def test_the_input_plugin_is_always_out(self):
        # It owns the HID UUID and both PSMs; btkey cannot start with it.
        self.assertIn("input", self.make()._noplugin())

    def test_audio_plugins_stay_by_default(self):
        self.assertEqual(self.make()._noplugin(), "input")

    def test_no_audio_drops_them(self):
        names = self.make(audio=False)._noplugin().split(",")
        self.assertEqual(sorted(names), ["a2dp", "avrcp", "input"])

    def test_it_is_a_comma_separated_list(self):
        # bluetoothd takes one --noplugin= with the names joined.
        self.assertNotIn(" ", self.make(audio=False)._noplugin())


class StopTest(BtdTest):
    def test_a_well_behaved_daemon_is_asked_to_stop(self):
        process = FakeProcess()
        self.make(process=process).stop()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_one_that_ignores_that_is_killed(self):
        process = FakeProcess(terminate_hangs=True)
        self.make(process=process).stop()
        self.assertTrue(process.killed)

    def test_the_system_unit_comes_back(self):
        self.make(process=FakeProcess()).stop()
        self.assertIn(("start", btd.UNIT), self.calls)

    def test_it_comes_back_even_when_the_child_cannot_be_reaped(self):
        # Stuck in the kernel on a wedged controller.  Giving up here used
        # to skip the restart, which is the exact outcome this whole
        # arrangement exists to prevent - and PR_SET_PDEATHSIG will have
        # taken the private daemon down regardless.
        process = FakeProcess(terminate_hangs=True, kill_hangs=True)
        self.make(process=process).stop()
        self.assertIn(("start", btd.UNIT), self.calls)
        self.assertTrue(any("could not stop" in event
                            for event in self.events))

    def test_a_unit_that_was_not_running_is_not_started(self):
        self.make(process=FakeProcess(), unit_was_active=False).stop()
        self.assertNotIn(("start", btd.UNIT), self.calls)

    def test_stopping_twice_does_not_start_it_twice(self):
        daemon = self.make(process=FakeProcess())
        daemon.stop()
        daemon.stop()
        self.assertEqual(self.calls.count(("start", btd.UNIT)), 1)


class StartTest(BtdTest):
    def test_a_missing_bluetoothd_is_reported_as_such(self):
        saved, btd.BLUETOOTHD = btd.BLUETOOTHD, "/nonexistent/bluetoothd"
        try:
            with self.assertRaises(btd.BluetoothdError) as caught:
                self.make(unit_was_active=False).start()
        finally:
            btd.BLUETOOTHD = saved
        self.assertIn("/nonexistent/bluetoothd", str(caught.exception))
        # Nothing was taken away, so nothing has to be given back.
        self.assertEqual(self.calls, [])

    def test_a_daemon_that_exits_at_once_is_reported(self):
        daemon = self.make(process=FakeProcess(alive=False))
        with self.assertRaises(btd.BluetoothdError) as caught:
            daemon._check_alive()
        self.assertIn("exited immediately", str(caught.exception))

    def test_a_failed_start_gives_the_system_unit_back(self):
        # start() undoes itself; without that the machine is left with the
        # system unit stopped and nothing in its place.
        daemon = self.make(process=FakeProcess(), unit_was_active=False)

        def explode():
            daemon.unit_was_active = True     # as start() would have set it
            raise btd.BluetoothdError("no adapter")

        daemon._spawn = explode
        with self.assertRaises(btd.BluetoothdError):
            daemon.start()
        self.assertIn(("start", btd.UNIT), self.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
