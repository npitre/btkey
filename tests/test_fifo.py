# SPDX-License-Identifier: GPL-2.0-only
"""Creating the FIFOs, and the two ways that goes wrong.

Two things go wrong here.  A FIFO btkey creates is root-owned, which locks
out the person who started it.  And a path that already exists need not be
a FIFO: an ordinary file is always ready to read, so the watch fires, gets
end-of-file and removes itself - a dead channel that has logged itself as
working.
"""

import os
import shutil
import stat
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import fifo, single


class MakeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "sub", "control")
        self.logged = []

    def make(self):
        fd = fifo.make(self.path, self.logged.append)
        if fd is not None:
            self.addCleanup(os.close, fd)
        return fd

    def is_fifo(self):
        return stat.S_ISFIFO(os.lstat(self.path).st_mode)

    def test_it_creates_the_directory_and_the_fifo(self):
        self.assertIsNotNone(self.make())
        self.assertTrue(self.is_fifo())

    def test_an_existing_fifo_is_reused_quietly(self):
        self.make()
        self.logged.clear()
        self.assertIsNotNone(self.make())
        self.assertEqual(self.logged, [])

    def test_an_ordinary_file_in_the_way_is_replaced(self):
        """`echo sweep > path` before btkey ever ran leaves a real file."""
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("sweep\n")
        self.assertIsNotNone(self.make())
        self.assertTrue(self.is_fifo())
        self.assertTrue(any("not a FIFO" in line for line in self.logged))

    def test_a_dangling_symlink_in_the_way_is_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        os.symlink(os.path.join(self.directory, "gone"), self.path)
        self.assertIsNotNone(self.make())
        self.assertTrue(self.is_fifo())

    def test_an_impossible_path_reports_and_returns_none(self):
        self.path = "/proc/nowhere/control"
        self.assertIsNone(self.make())
        self.assertEqual(len(self.logged), 1)

    def test_reading_does_not_stop_at_end_of_file(self):
        """O_RDWR: otherwise the watch removes itself after one command."""
        fd = self.make()
        os.write(fd, b"sweep\n")
        self.assertEqual(os.read(fd, 64), b"sweep\n")


class PrivacyTest(unittest.TestCase):
    """Anything that can write here types into somebody's phone.

    So the mode is set every time rather than only on FIFOs we create, and
    the result is checked rather than assumed - a FIFO left behind by an
    earlier run keeps whatever mode that run left it with.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "control")
        self.logged = []

    def make(self):
        fd = fifo.make(self.path, self.logged.append)
        if fd is not None:
            self.addCleanup(os.close, fd)
        return fd

    def mode(self):
        return stat.S_IMODE(os.lstat(self.path).st_mode)

    def test_a_new_fifo_is_private(self):
        self.make()
        self.assertEqual(self.mode(), 0o600)

    def test_a_wide_open_one_left_behind_is_tightened(self):
        os.mkfifo(self.path, 0o666)
        os.chmod(self.path, 0o666)          # mkfifo honours the umask
        self.assertIsNotNone(self.make())
        self.assertEqual(self.mode(), 0o600)

    def test_a_group_readable_one_is_tightened(self):
        os.mkfifo(self.path)
        os.chmod(self.path, 0o640)
        self.make()
        self.assertEqual(self.mode(), 0o600)

    def test_privacy_is_checked_not_assumed(self):
        """The check has to reject what the chmod would have fixed."""
        os.mkfifo(self.path, 0o600)
        handle = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, handle)
        self.assertTrue(fifo._is_private(handle, None))
        os.chmod(self.path, 0o606)
        self.assertFalse(fifo._is_private(handle, None))

    def test_a_fifo_owned_by_someone_else_is_refused(self):
        os.mkfifo(self.path, 0o600)
        handle = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, handle)
        # Cannot chown without root, so ask whether it belongs to a user it
        # plainly does not.
        self.assertFalse(fifo._is_private(handle, (os.getuid() + 1, 0)))

    def test_the_owner_defaults_to_us_not_to_root(self):
        """btkey need not have got here through sudo."""
        os.mkfifo(self.path, 0o600)
        handle = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, handle)
        self.assertTrue(fifo._is_private(handle, None))


class InvokingUserTest(unittest.TestCase):
    def test_it_reads_the_sudo_environment(self):
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "1000", "1000"
        self.addCleanup(os.environ.pop, "SUDO_UID", None)
        self.addCleanup(os.environ.pop, "SUDO_GID", None)
        self.assertEqual(fifo.invoking_user(), (1000, 1000))

    def test_no_sudo_means_nobody_to_hand_it_to(self):
        os.environ.pop("SUDO_UID", None)
        self.assertIsNone(fifo.invoking_user())

    def test_nonsense_in_the_environment_is_not_fatal(self):
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "root", "root"
        self.addCleanup(os.environ.pop, "SUDO_UID", None)
        self.addCleanup(os.environ.pop, "SUDO_GID", None)
        self.assertIsNone(fifo.invoking_user())


class SingleInstanceTest(unittest.TestCase):
    """One btkey at a time.

    A second one does not merely fail.  Its private bluetoothd cannot
    start, since the first already owns org.bluez; failing to start it,
    btd.start() undoes itself, and undoing itself starts
    bluetooth.service underneath the instance still running.  So the
    second has to be stopped before it reaches any of that.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.path = os.path.join(self.directory, "lock")
        self.held = []

    def hold(self, who="nico"):
        handle, other = single.hold(who, self.path)
        if handle is not None:
            self.held.append(handle)
            self.addCleanup(os.close, handle)
        return handle, other

    def test_the_first_one_gets_it(self):
        handle, other = self.hold()
        self.assertIsNotNone(handle)
        self.assertIsNone(other)

    def test_the_second_one_does_not(self):
        self.hold()
        handle, other = self.hold("someone")
        self.assertIsNone(handle)

    def test_the_second_one_is_told_who_has_it(self):
        # "already running" without saying which leaves someone hunting
        # through ps for a process they cannot tell apart from the shell
        # they typed it in.
        self.hold("nico")
        _, other = self.hold("someone")
        self.assertIn("pid %d" % os.getpid(), other)
        self.assertIn("started by nico", other)

    def test_letting_go_lets_the_next_one_in(self):
        # Taken without the cleanup the helper adds, since this one closes
        # the descriptor itself and closing it twice is an error.
        handle, _ = single.hold("first", self.path)
        os.close(handle)
        again, _ = single.hold("next", self.path)
        self.addCleanup(os.close, again)
        self.assertIsNotNone(again)

    def test_the_lock_survives_being_dropped_by_a_crash(self):
        """The kernel releases it however the holder dies.

        Which is the reason for a lock rather than a pid file: nothing is
        left behind to be stale, and no pid has to be checked for having
        been recycled.
        """
        pid = os.fork()
        if pid == 0:                       # the child takes it and dies
            single.hold("child", self.path)
            os._exit(0)
        os.waitpid(pid, 0)
        handle, other = self.hold()
        self.assertIsNotNone(handle, "a dead holder kept the lock")

    def test_a_directory_that_does_not_exist_yet_is_made(self):
        path = os.path.join(self.directory, "run", "btkey", "lock")
        handle, _ = single.hold("nico", path)
        self.addCleanup(os.close, handle)
        self.assertTrue(os.path.exists(path))

    def test_it_says_nothing_about_a_holder_that_wrote_nothing(self):
        open(self.path, "w").close()
        handle = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, handle)
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        taken, other = single.hold("someone", self.path)
        self.assertIsNone(taken)
        self.assertEqual(other, "")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
