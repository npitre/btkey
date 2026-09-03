#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Which console is in front, and switching between them.

The whole of btkey's forwarding rule is here: keystrokes go to the phone
exactly while our console is in the foreground.  Get the answer wrong in
one direction and the phone receives what was typed at a different
console; wrong in the other and the keyboard appears dead.

Which console is *ours* is a separate question with a separate answer,
and confusing the two grabs a keyboard somebody else is typing at.

The switch chords are reimplemented here too, because a grabbed keyboard
never reaches the kernel's own Alt+Fn handler.
"""

import errno
import os
import select
import shutil
import stat
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import session as session_module, vt

from test_keys import calls_in, capture_timers, make_session


class FakeKernel:
    """Stands in for the console: the attribute, and the switch ioctl."""

    def __init__(self, active=4, fail=None):
        self.active = active
        self.fail = fail            # an errno to raise instead
        self.says = None            # what the attribute holds, if not tty<n>
        self.activated = []
        self.read = []              # which descriptor was read, in order

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

    def pread(self, fd, size, offset):
        self.read.append(fd)
        if self.fail is not None:
            raise OSError(self.fail, os.strerror(self.fail))
        if self.says is not None:
            return self.says.encode()
        return ("tty%d\n" % self.active).encode()


def chardev(major, minor):
    """What fstat says about a character device."""
    class About:
        st_mode = stat.S_IFCHR | 0o600
        st_rdev = os.makedev(major, minor)
    return About()


def tty_nr(major, minor):
    """A device number the way /proc/pid/stat writes one."""
    return (minor & 0xFF) | (major << 8) | ((minor & ~0xFF) << 12)


class OwnConsoleTest(unittest.TestCase):
    """Which console btkey was started on.

    Not which one is in front.  Started from a console in the background
    the two differ, and over ssh there is no answer at all, where the
    foreground one would be somebody else's keyboard.
    """

    def setUp(self):
        self.proc = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.proc, ignore_errors=True)
        # Whatever the machine running the tests was started from.
        was = os.environ.pop("SUDO_TTY", None)
        if was is not None:
            self.addCleanup(os.environ.__setitem__, "SUDO_TTY", was)
        self.terminal = None        # what /dev/tty is, if anything
        self.stdio = {}             # descriptor -> what fstat says
        self.parent = 100
        self.saved = (vt.os.open, vt.os.close, vt.os.fstat, vt.os.getppid,
                      vt.PROC)
        vt.os.open = self.open
        vt.os.close = lambda handle: None
        vt.os.fstat = self.fstat
        vt.os.getppid = lambda: self.parent
        vt.PROC = self.proc
        self.addCleanup(self.restore)

    def restore(self):
        (vt.os.open, vt.os.close, vt.os.fstat,
         vt.os.getppid, vt.PROC) = self.saved

    def open(self, path, flags):
        if path == "/dev/tty" and self.terminal is not None:
            return 9
        raise OSError(errno.ENXIO, "no such device or address")

    def fstat(self, handle):
        if handle == 9 and self.terminal is not None:
            return self.terminal
        if handle in self.stdio:
            return self.stdio[handle]
        raise OSError(errno.EBADF, "bad file descriptor")

    def process(self, pid, parent, number, name="bash"):
        os.mkdir(os.path.join(self.proc, str(pid)))
        with open(os.path.join(self.proc, str(pid), "stat"), "w") as handle:
            handle.write("%d (%s) S %d 900 900 %d 0 and so on\n"
                         % (pid, name, parent, number))

    # -- from our own descriptors ----------------------------------------

    def test_a_controlling_terminal_that_is_a_console_answers(self):
        self.terminal = chardev(4, 3)
        self.assertEqual(vt.own_console(), 3)

    def test_a_pty_is_not_a_console(self):
        # Which is what sudo leaves us holding.
        self.terminal = chardev(136, 3)
        self.assertIsNone(vt.own_console())

    def test_a_serial_line_is_not_a_console(self):
        # /dev/ttyS0 is major 4 as well; the minors above MAX_VT are the
        # serial lines, and a phone typed at over a serial console is
        # not what this is for.
        self.terminal = chardev(4, 64)
        self.assertIsNone(vt.own_console())

    def test_stdin_answers_when_the_terminal_was_given_up(self):
        self.stdio = {0: chardev(4, 5)}
        self.assertEqual(vt.own_console(), 5)

    def test_stderr_will_do_when_the_rest_is_redirected(self):
        self.stdio = {2: chardev(4, 6)}
        self.assertEqual(vt.own_console(), 6)

    # -- from what sudo remembers -----------------------------------------

    def test_sudo_says_which_terminal_it_was_invoked_from(self):
        os.environ["SUDO_TTY"] = "/dev/tty2"
        self.addCleanup(os.environ.pop, "SUDO_TTY", None)
        self.assertEqual(vt.own_console(), 2)

    def test_our_own_terminal_beats_what_sudo_remembers(self):
        # An inherited SUDO_TTY outlives the sudo that set it.
        os.environ["SUDO_TTY"] = "/dev/tty9"
        self.addCleanup(os.environ.pop, "SUDO_TTY", None)
        self.terminal = chardev(4, 3)
        self.assertEqual(vt.own_console(), 3)

    def test_a_sudo_tty_that_is_no_console_is_ignored(self):
        for value in ("/dev/pts/3", "/dev/ttyS0", "/dev/tty", "/dev/tty99",
                      "", "nonsense"):
            os.environ["SUDO_TTY"] = value
            self.addCleanup(os.environ.pop, "SUDO_TTY", None)
            self.assertIsNone(vt.own_console(), value)

    # -- from the processes that started us -------------------------------

    def test_the_shell_that_ran_sudo_answers_through_the_pty(self):
        # btkey under sudo's monitor, under sudo, under the shell: the
        # pty is in the way and the console is three processes up.
        self.terminal = chardev(136, 4)
        self.process(100, 200, tty_nr(136, 4), name="sudo")
        self.process(200, 300, tty_nr(136, 4), name="sudo")
        self.process(300, 1, tty_nr(4, 3))
        self.assertEqual(vt.own_console(), 3)

    def test_a_console_in_double_figures_survives_the_encoding(self):
        # tty11 is not tty1: the minor is split across the number.
        self.process(100, 1, tty_nr(4, 11))
        self.assertEqual(vt.own_console(), 11)

    def test_a_serial_line_high_in_the_minors_is_not_mistaken(self):
        # /dev/ttyS236 is major 4, minor 300, and a minor that large does
        # not fit the byte the device number starts it in: the rest is
        # kept higher up.  Read as just that byte it comes out as 44,
        # which is a console, and somebody's serial line is grabbed.
        self.process(100, 1, tty_nr(4, 300))
        self.assertIsNone(vt.own_console())

    def test_a_command_name_with_spaces_and_brackets_is_parsed(self):
        # The fields are counted from the last bracket for this reason.
        self.process(100, 1, tty_nr(4, 7), name="a (funny) name")
        self.assertEqual(vt.own_console(), 7)

    def test_over_ssh_there_is_no_answer(self):
        # Nothing in the chain has a console, and the one in front
        # belongs to whoever is sitting at the machine.
        self.terminal = chardev(136, 0)
        self.process(100, 200, tty_nr(136, 0))
        self.process(200, 1, 0, name="sshd")
        self.assertIsNone(vt.own_console())

    def test_a_chain_that_goes_nowhere_ends(self):
        self.process(100, 100, 0)      # its own parent
        self.assertIsNone(vt.own_console())

    def test_it_does_not_climb_for_ever(self):
        for pid in range(100, 100 + vt.MAX_ANCESTORS + 4):
            self.process(pid, pid + 1, 0)
        self.process(100 + vt.MAX_ANCESTORS + 4, 1, tty_nr(4, 2))
        self.assertIsNone(vt.own_console())

    def test_nothing_anywhere_is_no_answer(self):
        self.assertIsNone(vt.own_console())


class RealOwnConsoleTest(unittest.TestCase):
    """The same question against the real /proc, which is the one shape
    of stat line nobody can get wrong on purpose."""

    def test_it_answers_with_a_console_or_with_nothing(self):
        found = vt.own_console()
        self.assertTrue(found is None or 1 <= found <= vt.MAX_VT,
                        "answered %r" % (found,))


class ConsolesTest(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.opened = []
        self.closed = []
        self.started_on = 4         # what own_console() answers
        self.searches = 0
        self.saved = (vt.fcntl.ioctl, vt.os.open, vt.os.close,
                      vt.os.pread, vt.own_console)
        vt.fcntl.ioctl = self.kernel.ioctl
        vt.os.open = self.open
        vt.os.close = self.closed.append
        vt.os.pread = self.kernel.pread
        vt.own_console = self.own_console
        self.addCleanup(self.restore)

    def restore(self):
        (vt.fcntl.ioctl, vt.os.open, vt.os.close,
         vt.os.pread, vt.own_console) = self.saved

    def own_console(self):
        self.searches += 1
        return self.started_on

    def open(self, path, flags):
        # A descriptor of its own each time, so closing the right one is
        # something the tests can tell.
        self.opened.append(path)
        return 7 + len(self.opened) - 1

    # -- which console is ours -------------------------------------------

    def test_it_adopts_the_console_it_was_started_on(self):
        # The one it was started on, which is not always the one in
        # front: started from a console in the background, the other
        # answer grabs a keyboard somebody else is typing at.
        self.started_on, self.kernel.active = 3, 5
        self.assertEqual(vt.Consoles().vt, 3)

    def test_an_explicit_console_is_taken_as_given(self):
        self.assertEqual(vt.Consoles(vt=9).vt, 9)

    def test_a_console_given_outright_is_not_searched_for(self):
        vt.Consoles(vt=9)
        self.assertEqual(self.searches, 0)

    def test_not_knowing_says_so_rather_than_guessing(self):
        # Over ssh, or from a terminal window: the foreground console
        # belongs to somebody else, and taking it is worse than saying
        # nothing can be done.
        self.started_on = None
        with self.assertRaises(vt.NoConsole) as caught:
            vt.Consoles()
        self.assertIn("--vt", str(caught.exception))
        self.assertEqual(self.opened, [])

    def test_it_holds_its_own_console_and_not_tty0(self):
        # Not /dev/tty0, which is root's alone, and not our own
        # terminal, which under sudo is a pty knowing nothing about VTs.
        self.started_on = 3
        vt.Consoles()
        self.assertEqual(self.opened, ["/dev/tty3"])

    def test_a_console_it_cannot_open_says_which_and_why(self):
        def refuse(path, flags):
            self.opened.append(path)
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))

        vt.os.open = refuse
        with self.assertRaises(vt.NoConsole) as caught:
            vt.Consoles()
        self.assertIn("/dev/tty4", str(caught.exception))
        self.assertIn("Permission denied", str(caught.exception))
        self.assertEqual(self.closed, [], "nothing was opened to leak")

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

    def test_the_refusal_opens_nothing_to_leak(self):
        # The range check comes before any of it, so there is nothing
        # open to give back.
        with self.assertRaises(vt.NoConsole):
            vt.Consoles(vt=0)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.closed, [])

    def test_close_gives_the_console_back_once(self):
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
        self.saved = (vt.fcntl.ioctl, vt.os.open, vt.os.close, vt.os.pread,
                      vt.own_console)
        vt.fcntl.ioctl = self.kernel.ioctl
        vt.os.open = self.open
        vt.os.close = self.closed.append
        vt.os.pread = self.pread
        # Whether the machine running the tests has a console of its own
        # is not what this class is about.
        vt.own_console = lambda: 4
        self.addCleanup(self.restore)

    def restore(self):
        (vt.fcntl.ioctl, vt.os.open, vt.os.close,
         vt.os.pread, vt.own_console) = self.saved

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

    def started(self):
        return vt.Consoles()

    def test_watching_reads_it_once_to_arm_it(self):
        """A sysfs attribute nobody has read is ready from the outset.

        Watched without this it fires the instant it is added, and again
        the instant it is rearmed, which is a spin rather than a watch.
        """
        consoles = self.started()
        consoles.watch()
        self.assertEqual(self.reads, [(11, 0)])

    def test_nothing_but_the_rearm_reads_the_watched_descriptor(self):
        """Reading is what clears the readiness, so a second reader of
        the same file swallows notifications.  Nothing else reads it:
        which console is in front is asked of the console instead.
        """
        consoles = vt.Consoles()
        consoles.watch()
        self.reads.clear()
        consoles.active()
        consoles.is_foreground()
        self.assertEqual(self.reads, [], "something read the attribute")

    def test_asking_twice_does_not_open_it_twice(self):
        consoles = vt.Consoles()
        self.assertEqual(consoles.watch(), consoles.watch())
        self.assertEqual(self.opened.count(vt.ACTIVE_ATTRIBUTE), 1)

    def test_every_read_starts_at_the_beginning(self):
        # A read carrying on from where the last one stopped is past the
        # end, returns nothing, and clears no readiness at all.
        consoles = self.started()
        consoles.watch()
        consoles.rearm()
        self.assertEqual(self.reads, [(11, 0), (11, 0)])

    def test_rearming_without_a_watch_does_nothing(self):
        consoles = self.started()
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

    def test_a_watch_that_cannot_be_opened_says_so(self):
        # The caller falls back to asking rather than losing the console.
        # Startup has the attribute open already, so this is the second
        # open failing, not a kernel without one: that is refused before
        # there is a Consoles at all.
        consoles = vt.Consoles()
        self.refuse = True
        self.assertIsNone(consoles.watch())

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
        # A console of one's own is a thing the test runner may not have,
        # so the watch is exercised without the rest of the class.
        consoles = vt.Consoles.__new__(vt.Consoles)
        consoles.fd = consoles.watch_fd = None
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
