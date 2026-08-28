#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Which console is in front, and switching between them.

The whole of btkey's forwarding rule is here: keystrokes go to the phone
exactly while our console is in the foreground.  Get the answer wrong in
one direction and the phone receives what was typed at a different
console; wrong in the other and the keyboard appears dead.

The switch chords are reimplemented here too, because a grabbed keyboard
never reaches the kernel's own Alt+Fn handler.
"""

import errno
import os
import select
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import session as session_module, vt

from test_keys import calls_in, capture_timers, make_session


class FakeKernel:
    """Stands in for the ioctls on /dev/tty0."""

    def __init__(self, active=4, fail=None):
        self.active = active
        self.fail = fail            # an errno to raise instead
        self.activated = []

    def ioctl(self, fd, request, arg=0):
        if self.fail is not None:
            raise OSError(self.fail, os.strerror(self.fail))
        if request == vt.VT_GETSTATE:
            arg[:6] = struct.pack("HHH", self.active, 0, 0)
            return 0
        if request == vt.VT_ACTIVATE:
            self.activated.append(arg)
            self.active = arg
            return 0
        raise AssertionError("unexpected ioctl %#x" % request)


class ConsolesTest(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.opened = []
        self.closed = []
        self.saved = (vt.fcntl.ioctl, vt.os.open, vt.os.close)
        vt.fcntl.ioctl = self.kernel.ioctl
        vt.os.open = self.open
        vt.os.close = self.closed.append
        self.addCleanup(self.restore)

    def restore(self):
        vt.fcntl.ioctl, vt.os.open, vt.os.close = self.saved

    def open(self, path, flags):
        self.opened.append(path)
        return 7

    # -- which console is ours -------------------------------------------

    def test_it_adopts_the_console_it_was_started_from(self):
        self.kernel.active = 3
        self.assertEqual(vt.Consoles().vt, 3)

    def test_an_explicit_console_is_taken_as_given(self):
        self.assertEqual(vt.Consoles(vt=9).vt, 9)

    def test_it_reads_tty0_not_our_own_terminal(self):
        # Under sudo stdin is a pty, which knows nothing about VTs.
        vt.Consoles()
        self.assertEqual(self.opened, ["/dev/tty0"])

    def test_no_vt_layer_says_so_rather_than_failing_obscurely(self):
        def refuse(path, flags):
            raise OSError(errno.ENOENT, "No such file or directory")

        vt.os.open = refuse
        with self.assertRaises(vt.NoConsole) as caught:
            vt.Consoles()
        self.assertIn("/dev/tty0", str(caught.exception))

    # -- foreground ------------------------------------------------------

    def test_ours_in_front_is_the_foreground(self):
        consoles = vt.Consoles(vt=4)
        self.kernel.active = 4
        self.assertTrue(consoles.is_foreground())

    def test_another_console_in_front_is_not(self):
        consoles = vt.Consoles(vt=4)
        self.kernel.active = 5
        self.assertFalse(consoles.is_foreground())

    def test_an_ioctl_that_fails_counts_as_not_in_front(self):
        # The safe answer: it costs a released grab, where the other way
        # round would forward a different console's keystrokes to a phone.
        consoles = vt.Consoles(vt=4)
        self.kernel.fail = errno.EIO
        self.assertFalse(consoles.is_foreground())

    # -- switching -------------------------------------------------------

    def test_switching_activates_the_console_asked_for(self):
        vt.Consoles(vt=4).switch_to(2)
        self.assertEqual(self.kernel.activated, [2])

    def test_a_console_out_of_range_is_refused_without_an_ioctl(self):
        consoles = vt.Consoles(vt=4)
        for target in (0, -1, vt.MAX_VT + 1):
            self.assertFalse(consoles.switch_to(target))
        self.assertEqual(self.kernel.activated, [])

    def test_a_switch_the_kernel_refuses_is_reported(self):
        consoles = vt.Consoles(vt=4)
        self.kernel.fail = errno.EPERM
        self.assertFalse(consoles.switch_to(2))

    # -- the option ------------------------------------------------------

    def test_an_out_of_range_vt_option_is_refused_at_startup(self):
        # Otherwise btkey runs forever on a console that cannot exist,
        # grabbing nothing and explaining nothing.
        for target in (0, vt.MAX_VT + 1):
            with self.assertRaises(vt.NoConsole):
                vt.Consoles(vt=target)

    def test_the_refusal_does_not_leak_the_console(self):
        with self.assertRaises(vt.NoConsole):
            vt.Consoles(vt=0)
        self.assertEqual(self.closed, [7])

    def test_close_gives_the_descriptor_back_once(self):
        consoles = vt.Consoles(vt=4)
        consoles.close()
        consoles.close()
        self.assertEqual(self.closed, [7])


class ConsoleWatchTest(unittest.TestCase):
    """Waiting for the console to change instead of asking whether it has.

    Asking costs a wakeup and an ioctl twenty-five times a second for as
    long as btkey runs.  The kernel notifies on a sysfs attribute at every
    switch, so the question can be waited on instead.
    """

    def setUp(self):
        self.kernel = FakeKernel()
        self.opened = []
        self.closed = []
        self.reads = []
        self.refuse = False
        self.saved = (vt.fcntl.ioctl, vt.os.open, vt.os.close, vt.os.pread)
        vt.fcntl.ioctl = self.kernel.ioctl
        vt.os.open = self.open
        vt.os.close = self.closed.append
        vt.os.pread = self.pread
        self.addCleanup(self.restore)

    def restore(self):
        (vt.fcntl.ioctl, vt.os.open, vt.os.close, vt.os.pread) = self.saved

    def open(self, path, flags):
        self.opened.append(path)
        if path != vt.ACTIVE_ATTRIBUTE:
            return 7
        if self.refuse:
            raise OSError(errno.ENOENT, "no such file")
        return 11

    def pread(self, fd, size, offset):
        self.reads.append((fd, offset))
        return b"tty4\n"

    def test_it_watches_the_attribute_the_kernel_notifies_on(self):
        consoles = vt.Consoles()
        self.assertEqual(consoles.watch(), 11)
        self.assertIn(vt.ACTIVE_ATTRIBUTE, self.opened)

    def test_watching_reads_it_once_to_arm_it(self):
        """A sysfs attribute nobody has read is ready from the outset.

        Watched without this it fires the instant it is added, and again
        the instant it is rearmed, which is a spin rather than a watch.
        """
        consoles = vt.Consoles()
        consoles.watch()
        self.assertEqual(self.reads, [(11, 0)])

    def test_asking_twice_does_not_open_it_twice(self):
        consoles = vt.Consoles()
        self.assertEqual(consoles.watch(), consoles.watch())
        self.assertEqual(self.opened.count(vt.ACTIVE_ATTRIBUTE), 1)

    def test_every_read_starts_at_the_beginning(self):
        # A read carrying on from where the last one stopped is past the
        # end, returns nothing, and clears no readiness at all.
        consoles = vt.Consoles()
        consoles.watch()
        consoles.rearm()
        self.assertEqual(self.reads, [(11, 0), (11, 0)])

    def test_rearming_without_a_watch_does_nothing(self):
        consoles = vt.Consoles()
        # False, because the caller reads it as the watch being finished
        # with; a watch that was never started has nothing to keep.
        self.assertFalse(consoles.rearm())
        self.assertEqual(self.reads, [])

    def test_a_rearm_that_cannot_read_says_so(self):
        def gone(fd, size, offset):
            raise OSError(errno.ENODEV, "no such device")

        consoles = vt.Consoles()
        consoles.watch()
        vt.os.pread = gone
        self.assertFalse(consoles.rearm())

    def test_a_rearm_that_reads_says_so(self):
        consoles = vt.Consoles()
        consoles.watch()
        self.assertTrue(consoles.rearm())

    def test_no_attribute_says_so_rather_than_failing(self):
        # A kernel without it, so the caller can fall back to asking.
        self.refuse = True
        self.assertIsNone(vt.Consoles().watch())

    def test_close_gives_the_watch_descriptor_back_too(self):
        consoles = vt.Consoles()
        consoles.watch()
        consoles.close()
        self.assertIn(11, self.closed)

    def test_close_gives_it_back_once(self):
        consoles = vt.Consoles()
        consoles.watch()
        consoles.close()
        consoles.close()
        self.assertEqual(self.closed.count(11), 1)


@unittest.skipIf(not os.path.exists(vt.ACTIVE_ATTRIBUTE),
                 "no %s on this kernel" % vt.ACTIVE_ATTRIBUTE)
class RealConsoleAttributeTest(unittest.TestCase):
    """Against the real sysfs attribute, the arming being the subtle part.

    Only the arming is checked.  That it fires on a switch cannot be
    tested without moving the console out from under whoever is running
    the tests.
    """

    def ready(self, fd):
        poller = select.poll()
        poller.register(fd, select.POLLPRI | select.POLLERR)
        return bool(poller.poll(200))

    def watcher(self):
        # /dev/tty0 needs root and the attribute does not, so the watch is
        # exercised without the rest of the class.
        consoles = vt.Consoles.__new__(vt.Consoles)
        consoles.watch_fd = None
        return consoles

    def test_unread_it_is_ready_at_once(self):
        # Which is the whole reason watch() reads it.
        fd = os.open(vt.ACTIVE_ATTRIBUTE, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertTrue(self.ready(fd))

    def test_being_ready_unread_says_nothing_about_being_notified(self):
        """Why there is no capability probe here.

        Every unread sysfs attribute reports itself ready, including ones
        nothing ever calls sysfs_notify on, so a probe would pass on a
        kernel that never says a word.  Presence is all there is to test,
        and waiting on this one has worked for as long as it has existed.
        """
        never_notified = "/sys/class/tty/tty0/dev"
        if not os.path.exists(never_notified):
            self.skipTest("no %s" % never_notified)
        fd = os.open(never_notified, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertTrue(self.ready(fd))

    def test_watch_hands_back_a_quiet_descriptor(self):
        consoles = self.watcher()
        fd = consoles.watch()
        self.addCleanup(os.close, fd)
        self.assertFalse(self.ready(fd))

    def test_it_stays_quiet_after_rearming(self):
        consoles = self.watcher()
        fd = consoles.watch()
        self.addCleanup(os.close, fd)
        consoles.rearm()
        self.assertFalse(self.ready(fd))

    def test_it_names_a_console(self):
        consoles = self.watcher()
        fd = consoles.watch()
        self.addCleanup(os.close, fd)
        os.lseek(fd, 0, os.SEEK_SET)
        self.assertRegex(os.read(fd, 64).decode(), r"^tty\d+")


class SessionConsoleWatchTest(unittest.TestCase):
    """That the session waits on it rather than asking."""

    NOTICE = session_module.GLib.IOCondition.PRI
    # kernfs reports the error bit alongside the event every time.
    ORDINARY = (session_module.GLib.IOCondition.PRI
                | session_module.GLib.IOCondition.ERR)
    BROKEN = session_module.GLib.IOCondition.ERR

    def test_a_change_rechecks_which_console_is_in_front(self):
        session = make_session()
        session.foreground = False
        session.console_changed(11, self.ORDINARY)
        self.assertTrue(session.foreground)

    def test_a_change_rearms_the_attribute(self):
        # Or it stays ready and the watch spins for the rest of the run.
        session = make_session()
        session.console_changed(11, self.ORDINARY)
        self.assertEqual(session.consoles.rearmed, 1)

    def test_the_watch_stays_on(self):
        session = make_session()
        self.assertTrue(session.console_changed(11, self.NOTICE))

    def test_an_error_without_the_event_ends_the_watch(self):
        """The failure BRLTTY had to fix twice in its own monitor.

        A descriptor in error is reported ready for ever, so a callback
        that says "carry on" turns the watch into a busy loop for the
        rest of the run.  Ending it and asking instead is the graceful
        way down.
        """
        session = make_session()
        timers = capture_timers(
            lambda: self.assertFalse(
                session.console_changed(11, self.BROKEN)))
        self.assertEqual([ms for ms, _, _ in timers],
                         [session_module.FOREGROUND_POLL_MS])

    def test_a_rearm_that_fails_ends_the_watch_too(self):
        session = make_session()
        session.consoles.rearm = lambda: False
        timers = capture_timers(
            lambda: self.assertFalse(
                session.console_changed(11, self.ORDINARY)))
        self.assertEqual([ms for ms, _, _ in timers],
                         [session_module.FOREGROUND_POLL_MS])

    def test_a_watch_that_fails_says_so(self):
        session = make_session()
        logged = []
        session.log = logged.append
        capture_timers(
            lambda: session.console_changed(11, self.BROKEN))
        self.assertEqual(len(logged), 1, logged)

    def test_the_console_is_still_checked_on_the_way_down(self):
        # The switch that was being reported must not be lost with it.
        session = make_session()
        session.foreground = False
        capture_timers(
            lambda: session.console_changed(11, self.BROKEN))
        self.assertTrue(session.foreground)

    def test_a_kernel_without_the_attribute_falls_back_to_asking(self):
        session = make_session()
        session.consoles.watch_fd = None       # what watch() hands back
        logged = []
        session.log = logged.append
        timers = capture_timers(session.watch_foreground)
        self.assertEqual([ms for ms, _, _ in timers],
                         [session_module.FOREGROUND_POLL_MS])
        # Bound methods are made fresh on each access, so compare
        # rather than identify.
        self.assertEqual(timers[0][1], session.poll_foreground)
        self.assertEqual(len(logged), 1, logged)

    def test_startup_waits_rather_than_asking(self):
        """The wiring, which is what the twenty-five ioctls a second were.

        watch_foreground can be perfect and cost nothing if run() still
        arms the timer beside it.
        """
        calls = calls_in("session.py", "run")
        self.assertIn("self.watch_foreground()", calls)
        self.assertFalse([call for call in calls
                          if "FOREGROUND_POLL_MS" in call], calls)

    def test_a_kernel_with_it_sets_no_timer(self):
        session = make_session()
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        session.consoles.watch_fd = read
        self.assertEqual(capture_timers(session.watch_foreground), [])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
