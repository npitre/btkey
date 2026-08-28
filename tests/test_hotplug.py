#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Being told a keyboard arrived.

btkey asks the kernel to report changes to /dev/input.  What is worth
testing is not that inotify works but that the session is wired to it:
the monitor is kept alive, the callback has the signature GIO calls it
with, the burst a single keyboard arrives as becomes one look, and the
look happens late enough that udev has finished setting the node's mode.

These run a real Gio.FileMonitor over a real directory and a real main
loop, with the session pointed at that directory in place of /dev/input.
"""

import ast
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib

from btkey import evdev, session as session_module

from test_grab import RecordingDevice, keyboard_factory
from test_keys import make_session


def run_loop(milliseconds):
    """Let GLib deliver its events for a while."""
    loop = GLib.MainLoop()
    GLib.timeout_add(milliseconds, lambda: (loop.quit(), False)[1])
    loop.run()


class HotplugNoticeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="btkey-hotplug-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.real_directory = evdev.DEVICE_DIRECTORY
        evdev.DEVICE_DIRECTORY = self.directory
        self.addCleanup(setattr, evdev, "DEVICE_DIRECTORY",
                        self.real_directory)
        self.looks = []

    def session(self, *devices):
        session = make_session(keyboards=keyboard_factory(*devices))
        real = session.rescan_devices

        def counted():
            self.looks.append(True)
            return real()

        session.rescan_devices = counted
        return session

    def touch(self, name, mode=0o660):
        # What udev does, in the order it does it: the node appears, and
        # is given its ownership and mode afterwards.
        path = os.path.join(self.directory, name)
        with open(path, "w"):
            pass
        os.chmod(path, mode)
        return path

    def test_a_node_appearing_is_noticed_without_being_polled_for(self):
        session = self.session()
        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(len(self.looks), 1, self.looks)

    def test_a_node_going_away_is_noticed_too(self):
        path = self.touch("event99")
        session = self.session()
        session.watch_for_devices()
        os.unlink(path)
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(len(self.looks), 1, self.looks)

    def test_the_burst_one_keyboard_arrives_as_becomes_one_look(self):
        """A keyboard is several nodes, each created and then chmodded.

        Looking at each event would rescan every device in the directory
        half a dozen times over, and the first of those looks would find
        nodes udev has not finished with.
        """
        session = self.session()
        session.watch_for_devices()
        for index in range(4):
            self.touch("event9%d" % index)
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(len(self.looks), 1, self.looks)

    def test_a_node_getting_its_mode_is_worth_another_look(self):
        """udev makes the node and gives it its ownership afterwards.

        When the two are far enough apart the first look finds something
        that cannot be opened yet, and without this the keyboard would be
        missed until the next one was plugged in.
        """
        session = self.session()
        session.watch_for_devices()
        path = self.touch("event99", mode=0o600)
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        del self.looks[:]

        os.chmod(path, 0o660)
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(len(self.looks), 1, self.looks)

    def test_nothing_is_looked_at_before_the_node_has_settled(self):
        session = self.session()
        session.watch_for_devices()
        self.touch("event99")
        run_loop(max(1, session_module.DEVICE_SETTLE_MS // 4))
        self.assertEqual(self.looks, [])

    def test_a_quiet_directory_is_never_looked_at(self):
        session = self.session()
        session.watch_for_devices()
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(self.looks, [])

    def test_a_keyboard_arriving_while_we_have_the_screen_is_grabbed(self):
        device = RecordingDevice("/a")
        session = self.session()
        session.set_foreground(True)
        session.keyboards.devices["/a"] = device
        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertTrue(device.grabbed)

    def test_a_keyboard_arriving_while_another_console_has_it_is_not(self):
        # Grabbing from the background would take the keys away from
        # whoever is using that console.
        device = RecordingDevice("/a")
        session = self.session()
        session.set_foreground(False)
        session.keyboards.devices["/a"] = device
        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertFalse(device.grabbed)

    def test_the_monitor_is_kept(self):
        # A GFileMonitor that nothing holds a reference to is collected,
        # and then nothing is ever reported again.
        session = self.session()
        session.watch_for_devices()
        self.assertIsNotNone(session.device_monitor)

    def test_being_refused_a_watch_falls_back_to_looking(self):
        """GIO can refuse: one inotify instance per watch, and a limit.

        Note that a monitor on a directory that does not exist is not a
        refusal - GIO hands one back that simply never reports anything -
        so this stubs the refusal rather than staging one.
        """
        session = self.session()
        logged, timers = [], []
        session.log = logged.append

        def refuse(*args, **kwargs):
            raise GLib.Error("too many open files")

        real_new, real_timeout = session_module.Gio.File.new_for_path, \
            GLib.timeout_add
        session_module.Gio.File.new_for_path = refuse
        GLib.timeout_add = lambda ms, fn, *a: timers.append((ms, fn)) or 1
        try:
            session.watch_for_devices()
        finally:
            session_module.Gio.File.new_for_path = real_new
            GLib.timeout_add = real_timeout
        self.assertEqual([ms for ms, _ in timers],
                         [session_module.DEVICE_RESCAN_MS])
        self.assertIs(timers[0][1], session.rescan_devices)
        self.assertIsNone(session.device_monitor)
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("too many open files", logged[0])


class ComingBackTest(unittest.TestCase):
    """A console switch is the other moment the device set can be stale.

    Whatever changed while another console had the screen has to be taken
    account of before the grab, not after it.
    """

    def test_coming_back_looks_before_grabbing(self):
        trace = []
        device = RecordingDevice("/a", trace=trace)
        session = make_session(keyboards=keyboard_factory(device))
        session.foreground = False
        real = session.rescan_devices
        session.rescan_devices = lambda: (trace.append("look"), real())[1]
        session.set_foreground(True)
        self.assertIn("look", trace)
        self.assertLess(trace.index("look"), trace.index("grab /a"))


class HotplugWiringTest(unittest.TestCase):
    """That startup asks to be told, and sets no rescan of its own going."""

    def setUp(self):
        source = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "btkey", "session.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        self.functions = {node.name: node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef)}

    def calls_in(self, name):
        return {ast.unparse(node.func)
                for node in ast.walk(self.functions[name])
                if isinstance(node, ast.Call)}

    def test_startup_asks_to_be_told(self):
        self.assertIn("self.watch_for_devices", self.calls_in("run"))

    def test_startup_starts_no_rescan_of_its_own(self):
        started = {ast.unparse(node)
                   for node in ast.walk(self.functions["run"])
                   if isinstance(node, ast.Call)
                   and ast.unparse(node.func) == "GLib.timeout_add"}
        self.assertFalse([call for call in started
                          if "DEVICE_RESCAN_MS" in call], started)


if __name__ == "__main__":
    unittest.main()
