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

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import Gio, GLib

from btkey import evdev, session as session_module

from test_grab import RecordingDevice, keyboard_factory
from test_keys import (calls_in, capture_timers, make_session, source_of)


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
        # The shipped wait is a second, most of it there to give another
        # program time to claim the keyboard.  What these check is the
        # debouncing, which is the same at any length, so shorten it
        # rather than spend ten seconds proving it.  ShippedSettleTest
        # below holds the real value to its reasons.
        self.addCleanup(setattr, session_module, "DEVICE_SETTLE_MS",
                        session_module.DEVICE_SETTLE_MS)
        session_module.DEVICE_SETTLE_MS = 60
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

    def test_a_loopback_appearing_late_still_lands_in_the_same_look(self):
        """The case the wait is really for.

        Something else grabs the keyboard and publishes what it does not
        want through uinput; that loopback is what btkey should hold.  It
        is created after the grab, so it arrives partway through the
        wait, and restarting the wait at every arrival is what makes one
        look see both of them.
        """
        session = self.session()
        session.watch_for_devices()
        self.touch("event99")                       # the keyboard
        run_loop(session_module.DEVICE_SETTLE_MS // 2)
        self.assertEqual(self.looks, [])            # still waiting
        self.touch("event100")                      # its uinput loopback
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(len(self.looks), 1, self.looks)

    def test_arrivals_keep_pushing_the_look_back(self):
        """Restarted at every one, not run once from the first.

        With a fixed delay from the first arrival, a stream of them
        lasting longer than the wait gets a look part way through and
        another after, each seeing a half-built picture.  Restarting
        means one look, once everything has stopped moving.
        """
        session = self.session()
        session.watch_for_devices()
        settle = session_module.DEVICE_SETTLE_MS
        for index in range(6):
            self.touch("event9%d" % index)
            run_loop(max(1, settle // 3))
        self.assertEqual(self.looks, [], "looked while nodes were arriving")
        run_loop(settle + 300)
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

    def test_a_change_forgets_the_keyboards_we_were_not_holding(self):
        """What was decided about them was decided about another machine.

        A keyboard btkey could not grab is remembered so it can be tried
        again cheaply and complained about once.  A node appearing or
        going says the arrangement has changed - something started or
        stopped, and it may be the very thing that was holding it - so
        the memory is dropped and they are looked at afresh.

        The one being held is not touched: it is not a decision, it is a
        keyboard.
        """
        refused = RecordingDevice("/refused", grabbable=False)
        ours = RecordingDevice("/ours")
        session = self.session()
        session.set_foreground(True)
        session.keyboards.devices.update({"/refused": refused,
                                          "/ours": ours})
        session.keyboards.grab_all()
        self.assertTrue(ours.grabbed)

        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertNotIn("/refused", session.keyboards.devices)
        self.assertIn("/ours", session.keyboards.devices)

    def test_a_keyboard_arriving_while_away_is_not_watched(self):
        """Nothing is read from it until the screen comes back.

        The set closes what discovery opened while it is asleep, which
        test_grab covers; what matters here is that no watch is left on a
        descriptor that is about to be closed.
        """
        session = self.session()
        session.set_foreground(False)
        arrival = RecordingDevice("/new")
        session.keyboards.refresh = lambda: ([arrival], [])
        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertNotIn("/new", session.watches)

    def test_a_keyboard_arriving_while_we_have_the_screen_is_watched(self):
        session = self.session()
        session.foreground = False
        session.set_foreground(True)
        arrival = RecordingDevice("/new")

        def refresh():
            # What the real one does: the set is where it lands, which
            # is where grab_all and the watch both look for it.
            session.keyboards.devices["/new"] = arrival
            return [arrival], []

        session.keyboards.refresh = refresh
        session.watch_for_devices()
        self.touch("event99")
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertTrue(arrival.grabbed)
        self.assertFalse(arrival.closed)
        self.assertIn("/new", session.watches)

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

    def test_dropping_the_watch_gives_the_monitor_up(self):
        """Cancelled, not merely forgotten.

        A GFileMonitor that is only dropped keeps its inotify watch and
        its signal connection until the garbage collector gets round to
        it, and goes on waking the process meanwhile.
        """
        session = self.session()
        session.watch_for_devices()
        monitor = session.device_monitor
        session.unwatch_for_devices()
        self.assertIsNone(session.device_monitor)
        self.assertTrue(monitor.is_cancelled())

    def test_watching_twice_keeps_one_monitor(self):
        # Startup settles the foreground, which installs it; a switch
        # away and back must not leave the first one behind.
        session = self.session()
        session.watch_for_devices()
        monitor = session.device_monitor
        session.watch_for_devices()
        self.assertIs(session.device_monitor, monitor)

    def test_a_change_still_settling_is_dropped_with_the_watch(self):
        # It would fire on a console we no longer have, and rescan for
        # keyboards we have just given back.
        session = self.session()
        session.watch_for_devices()
        self.touch("event99")
        run_loop(max(1, session_module.DEVICE_SETTLE_MS // 4))
        self.assertIsNotNone(session.device_settle)
        session.unwatch_for_devices()
        self.assertIsNone(session.device_settle)
        run_loop(session_module.DEVICE_SETTLE_MS + 300)
        self.assertEqual(self.looks, [])

    def test_the_fallback_timer_is_stopped_too(self):
        session = self.session()
        session.consoles.watch_fd = None
        timers = []
        real = session_module.GLib.timeout_add
        removed = []
        real_remove = session_module.GLib.source_remove
        session_module.GLib.timeout_add = (
            lambda ms, fn, *a: timers.append((ms, fn)) or 77)
        session_module.GLib.source_remove = removed.append
        try:
            session.device_monitor = None
            session.watch_for_devices = (
                lambda: setattr(session, "device_timer", 77))
            session.watch_for_devices()
            session.unwatch_for_devices()
        finally:
            session_module.GLib.timeout_add = real
            session_module.GLib.source_remove = real_remove
        self.assertIn(77, removed)
        self.assertIsNone(session.device_timer)

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
        logged = []
        session.log = logged.append

        def refuse(*args, **kwargs):
            raise GLib.Error("too many open files")

        real = session_module.Gio.File.new_for_path
        session_module.Gio.File.new_for_path = refuse
        try:
            timers = capture_timers(session.watch_for_devices)
        finally:
            session_module.Gio.File.new_for_path = real
        self.assertEqual([ms for ms, _, _ in timers],
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


class WatchOrderTest(unittest.TestCase):
    """When the watch goes on and comes off, relative to everything else.

    A keyboard plugged in between the scan and the watch being
    established would fall through the gap: too late for the scan, too
    early for a watch that did not exist yet, and unnoticed until the
    next switch.  So the watch goes on first, and comes off last.
    """

    def trace(self):
        session = make_session(keyboards=keyboard_factory())
        session.foreground = False
        order = []

        def record(name, real):
            def wrapper(*args, **kwargs):
                order.append(name)
                return real(*args, **kwargs)
            return wrapper

        for name in ("watch_for_devices", "unwatch_for_devices",
                     "rescan_devices", "wake_devices", "sleep_devices"):
            setattr(session, name, record(name, getattr(session, name)))
        return session, order

    def test_the_watch_goes_on_before_the_look(self):
        session, order = self.trace()
        session.set_foreground(True)
        self.assertLess(order.index("watch_for_devices"),
                        order.index("rescan_devices"))

    def test_the_watch_goes_on_before_the_devices_are_opened(self):
        session, order = self.trace()
        session.set_foreground(True)
        self.assertLess(order.index("watch_for_devices"),
                        order.index("wake_devices"))

    def test_the_watch_comes_off_before_the_devices_are_closed(self):
        """The same race the other way round.

        An arrival reported after we have let go would have us open and
        grab a keyboard on a console that is no longer ours.
        """
        session, order = self.trace()
        session.set_foreground(True)
        session.set_foreground(False)
        self.assertLess(order.index("unwatch_for_devices"),
                        order.index("sleep_devices"))

    def test_an_event_that_arrives_after_the_cancel_is_ignored(self):
        # GIO can have one queued already when the monitor is cancelled.
        session, order = self.trace()
        session.set_foreground(True)
        session.set_foreground(False)
        session.device_directory_changed(None, None, None,
                                         Gio.FileMonitorEvent.CREATED)
        self.assertIsNone(session.device_settle)


class ShippedSettleTest(unittest.TestCase):
    """How long the real wait is, and why it is that long.

    Three things have to have happened before the look is worth making,
    and the slowest is not btkey's at all: whatever else on this machine
    wants the keyboard should get it first, and publish what it does not
    want through uinput, because that loopback is the device btkey should
    be holding.  BRLTTY does exactly this.
    """

    def test_it_is_long_enough_for_another_program_to_claim_it(self):
        self.assertGreaterEqual(session_module.DEVICE_SETTLE_MS, 1000)

    def test_it_is_short_enough_to_go_unnoticed(self):
        # Against the act of plugging a keyboard in.
        self.assertLessEqual(session_module.DEVICE_SETTLE_MS, 3000)

    def test_each_arrival_restarts_it(self):
        """Which is what makes the wait cover the loopback at all.

        The loopback is created after the keyboard is grabbed, so it
        arrives during the wait and pushes the end of it out; btkey looks
        once everything has stopped moving.
        """
        source = source_of("session.py")
        self.assertIn("GLib.source_remove(self.device_settle)", source)


class HotplugWiringTest(unittest.TestCase):
    """That the watch follows the foreground, and nothing polls beside it."""

    def setUp(self):
        self.run_calls = calls_in("session.py", "run")
        self.switch_calls = calls_in("session.py", "set_foreground")

    def test_startup_settles_the_foreground(self):
        # Which is what puts the watch in place, our console being in
        # front at startup: btkey was just typed at it.
        self.assertIn("self.poll_foreground()", self.run_calls)

    def test_taking_the_screen_asks_to_be_told(self):
        self.assertIn("self.watch_for_devices()", self.switch_calls)

    def test_giving_it_up_stops_being_told(self):
        # A keyboard plugged in while another console has the screen is
        # that console's business, and we look afresh on the way back.
        self.assertIn("self.unwatch_for_devices()", self.switch_calls)

    def test_nothing_starts_a_rescan_of_its_own(self):
        for calls in (self.run_calls, self.switch_calls):
            self.assertFalse([call for call in calls
                              if "DEVICE_RESCAN_MS" in call], calls)


if __name__ == "__main__":
    unittest.main()
