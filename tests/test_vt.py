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
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import vt


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


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
