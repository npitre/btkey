# SPDX-License-Identifier: GPL-2.0-only
"""Which virtual terminal we belong to, and switching between them.

Keystrokes come from evdev; the VT decides *when* to forward them, since
they go to the phone exactly while our console is in the foreground.  So
this needs to know which VT is ours, notice when it stops being in front,
and implement the switch chords itself - a grabbed keyboard never reaches
the kernel's own handler.

/dev/tty0 is the current foreground console whatever that happens to be,
so it answers both questions without needing our stdin to be a tty at all.
That matters: under Fedora's sudoers, stdin is a pty.
"""

import fcntl
import os
import struct

# <linux/vt.h>
VT_GETSTATE = 0x5603
VT_ACTIVATE = 0x5606

# The VT layer calls sysfs_notify on this attribute at every console
# change, so a poll for POLLPRI on it waits for the next switch instead of
# asking over and over whether one has happened.  It holds the console in
# front, as "tty2".
#
# Its presence is all there is to check.  Waiting on it has worked for as
# long as it has existed, and there is nothing a probe could add: an
# unread sysfs attribute reports itself ready whether or not anything ever
# notifies on it, so one would pass on a kernel that never says a word.
ACTIVE_ATTRIBUTE = "/sys/class/tty/tty0/active"

MAX_VT = 63


class NoConsole(Exception):
    """Raised when the virtual terminal layer is not reachable."""


class Consoles:
    def __init__(self, vt=None):
        try:
            self.fd = os.open("/dev/tty0", os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise NoConsole(
                "cannot open /dev/tty0 (%s) - btkey needs the Linux virtual "
                "terminal layer, so it cannot run over ssh or in a "
                "container without it" % exc.strerror)
        if vt is not None and not 1 <= vt <= MAX_VT:
            os.close(self.fd)
            raise NoConsole("VT %d is out of range; consoles are 1 to %d"
                            % (vt, MAX_VT))
        # Whatever console is in front when we start is the one the user
        # launched us from, which is the one they will switch back to.
        self.vt = vt if vt is not None else self.active()
        self.watch_fd = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.watch_fd is not None:
            os.close(self.watch_fd)
            self.watch_fd = None

    def watch(self):
        """A descriptor that goes POLLPRI-ready when the console changes.

        None where the attribute is not there, leaving the caller to fall
        back on asking.  The read is what arms it: a sysfs attribute that
        has not been read is ready from the outset, which would fire the
        moment it is watched and again immediately after.
        """
        if self.watch_fd is not None:
            return self.watch_fd
        try:
            self.watch_fd = os.open(ACTIVE_ATTRIBUTE, os.O_RDONLY)
        except OSError:
            return None
        self.rearm()
        return self.watch_fd

    def rearm(self):
        """Read the attribute back, which is what clears the readiness.

        Without this the descriptor stays ready and the watch spins.  It
        has to be read from the beginning: a read that starts where the
        last one stopped is past the end, returns nothing, and leaves the
        readiness where it was.  Hence pread rather than a seek and a
        read, which is the same thing in one call.

        False if it could not be read, which the caller must treat as the
        watch being finished with.
        """
        if self.watch_fd is None:
            return False
        try:
            os.pread(self.watch_fd, 64, 0)
        except OSError:
            return False
        return True

    def active(self):
        buf = bytearray(6)
        fcntl.ioctl(self.fd, VT_GETSTATE, buf)
        return struct.unpack("HHH", buf)[0]

    def is_foreground(self):
        try:
            return self.active() == self.vt
        except OSError:
            return False

    def switch_to(self, vt):
        if not 1 <= vt <= MAX_VT:
            return False
        try:
            fcntl.ioctl(self.fd, VT_ACTIVATE, vt)
            return True
        except OSError:
            return False
