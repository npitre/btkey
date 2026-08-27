# SPDX-License-Identifier: GPL-2.0-only
"""The log file, and stderr folded into it.

record() must touch only the file handle.  Three stray assignments in it
once reset the stderr-capture state on every log line, so fd 2 could never
be restored and partial lines were dropped, all without failing visibly.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey.journal import Journal


class _Broken:
    """A file handle whose every write fails, as a full disk would."""

    def __init__(self):
        self.closed = False

    def write(self, text):
        raise OSError(28, "No space left on device")

    def close(self):
        # Fails too, the way a buffered close on a full disk does: the
        # handle still has to be let go of.
        self.closed = True
        raise OSError(28, "No space left on device")


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "sub", "log")

    def read(self):
        with open(self.path) as handle:
            return handle.read()

    def test_it_creates_the_directory_and_writes(self):
        journal = Journal(self.path)
        journal.open("starting")
        journal.record("a line")
        journal.close("stopping")
        self.assertIn("starting", self.read())
        self.assertIn("a line", self.read())
        self.assertIn("stopping", self.read())

    def test_lines_are_timestamped(self):
        journal = Journal(self.path)
        journal.open()
        journal.record("a line")
        journal.close()
        first = self.read().splitlines()[0]
        self.assertRegex(first, r"^\d\d:\d\d:\d\d a line$")

    def test_no_path_means_no_file(self):
        journal = Journal("")
        journal.open("starting")
        journal.record("a line")
        journal.close()
        self.assertIsNone(journal.handle)
        self.assertEqual(os.listdir(self.directory), [])

    def test_the_file_is_private_to_its_owner(self):
        # A displayed pairing passkey is announced, and announcements are
        # recorded here.
        journal = Journal(self.path)
        journal.open("starting")
        journal.close()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_file_left_readable_by_an_earlier_run_is_tightened(self):
        os.makedirs(os.path.dirname(self.path))
        open(self.path, "w").close()
        os.chmod(self.path, 0o644)
        journal = Journal(self.path)
        journal.open("starting")
        journal.close()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_write_that_fails_says_so_and_lets_the_file_go(self):
        complaints = []
        journal = Journal(self.path, on_error=complaints.append)
        journal.open()
        broken = _Broken()
        journal.handle.close()
        journal.handle = broken
        journal.record("a line")
        self.assertIsNone(journal.handle)
        self.assertTrue(broken.closed)
        self.assertEqual(len(complaints), 1)
        journal.record("another")     # and nothing more is said about it
        self.assertEqual(len(complaints), 1)

    def test_an_unwritable_path_reports_once_and_carries_on(self):
        complaints = []
        journal = Journal("/proc/nonexistent/log",
                          on_error=complaints.append)
        journal.open()
        journal.record("a line")
        self.assertEqual(len(complaints), 1)

    def test_recording_leaves_the_stderr_capture_alone(self):
        """The regression: record() must touch only the file handle.

        Three stray assignments here reset saved_stderr, stderr_fd and the
        partial-line buffer on every single log line.
        """
        journal = Journal(self.path)
        journal.open()
        journal.saved_stderr, journal.stderr_fd = 99, 98
        journal.buffer = b"half a li"
        journal.record("a line")
        journal.record("another")
        self.assertEqual(journal.saved_stderr, 99)
        self.assertEqual(journal.stderr_fd, 98)
        self.assertEqual(journal.buffer, b"half a li")
        journal.saved_stderr = journal.stderr_fd = None   # nothing to close
        journal.close()

    def test_capture_and_release_restores_fd_2(self):
        journal = Journal(self.path)
        journal.open()
        self.addCleanup(journal.release_stderr)
        before = os.fstat(2)
        journal.capture_stderr(lambda text: None)
        self.assertIsNotNone(journal.saved_stderr)
        self.assertNotEqual(os.fstat(2).st_ino, before.st_ino)
        journal.release_stderr()
        self.assertIsNone(journal.saved_stderr)
        self.assertEqual(os.fstat(2).st_ino, before.st_ino)
        journal.close()


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
