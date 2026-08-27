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

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

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
