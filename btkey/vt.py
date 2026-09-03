# SPDX-License-Identifier: GPL-2.0-only
"""Which virtual terminal we belong to, and switching between them.

Keystrokes come from evdev; the VT decides *when* to forward them, since
they go to the phone exactly while our console is in the foreground.  So
this needs to know which VT is ours, notice when it stops being in front,
and implement the switch chords itself - a grabbed keyboard never reaches
the kernel's own handler.

Both questions are ioctls on a console, and any console will do:
VT_GETSTATE answers with whichever one is in front, not with the one it
was asked on.  So btkey holds its own, /dev/ttyN, which belongs to
whoever is logged in there.  /dev/tty0 would answer the same and is
root's alone.  Our own terminal is no use either: under Fedora's
sudoers it is a pty, which knows nothing about VTs.

Which console is ours is a different question, and not one either
ioctl answers: btkey can be started from a console that is not in
front, and over ssh from no console at all.  Answering with whichever
one is in front would grab a keyboard somebody else is typing at.  It
is our own descriptors that know, or, when sudo has put a pty between
us and the terminal, the shell that ran sudo.
"""

import fcntl
import os
import stat
import struct

# <linux/vt.h>
VT_GETSTATE = 0x5603
VT_ACTIVATE = 0x5606

CONSOLE_PREFIX = "/dev/tty"
CONSOLE_DEVICE = CONSOLE_PREFIX + "%d"
TTY_MAJOR = 4

# Where to look for the processes that started us, and how far up to
# follow them: a shell, sudo, and sudo's monitor is three.
PROC = "/proc"
MAX_ANCESTORS = 16

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


def _console_of_descriptor(handle):
    """The console a descriptor is open on, or None for anything else.

    Anything else includes /dev/ttyS0, which shares the major and is not
    a virtual terminal: the minors above MAX_VT are the serial lines.
    """
    try:
        about = os.fstat(handle)
    except OSError:
        return None
    if not stat.S_ISCHR(about.st_mode) \
            or os.major(about.st_rdev) != TTY_MAJOR:
        return None
    minor = os.minor(about.st_rdev)
    return minor if 1 <= minor <= MAX_VT else None


def _console_named(path):
    """The console a device path names, or None for anything else.

    /dev/ttyS0 is a serial line and /dev/tty is whichever one is ours,
    so the rest of the name has to be a number, and in range.
    """
    if not path.startswith(CONSOLE_PREFIX):
        return None
    number = path[len(CONSOLE_PREFIX):]
    if not number.isdigit():
        return None
    return int(number) if 1 <= int(number) <= MAX_VT else None


def _console_of_process(pid):
    """(the console that process is on, the process that started it).

    Either may be None.  The line holds the command in brackets and the
    command may hold brackets and spaces of its own, so the fields are
    counted from the last bracket rather than from the start.
    """
    try:
        with open(os.path.join(PROC, str(pid), "stat")) as handle:
            fields = handle.read().rpartition(")")[2].split()
        number, parent = int(fields[4]), int(fields[1])
    except (OSError, IndexError, ValueError):
        return None, None
    # The device number as the kernel writes it here: the bottom of the
    # minor, then the major, then the rest of the minor.
    major = (number >> 8) & 0xFFF
    minor = (number & 0xFF) | ((number >> 12) & 0xFFF00)
    if major != TTY_MAJOR or not 1 <= minor <= MAX_VT:
        return None, parent
    return minor, parent


def own_console():
    """The console btkey was started on, or None if it was not.

    /dev/tty is the controlling terminal whatever the descriptors have
    been pointed at since, so it is asked first, and the descriptors
    after it in case the terminal was given up rather than redirected.

    Under sudo neither answers: it puts a pty between the program and
    the terminal.  Sudo says where it came from in SUDO_TTY, and where
    it does not (su, doas, a root login), the shell that started us
    still holds the real terminal, so the search carries on up the
    processes above us.

    SUDO_TTY is a hint rather than evidence, being an environment
    variable, but a forged one only names a console, which --vt names
    outright anyway.
    """
    try:
        handle = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        handle = None
    if handle is not None:
        try:
            found = _console_of_descriptor(handle)
        finally:
            os.close(handle)
        if found is not None:
            return found
    for handle in (0, 1, 2):
        found = _console_of_descriptor(handle)
        if found is not None:
            return found
    found = _console_named(os.environ.get("SUDO_TTY", ""))
    if found is not None:
        return found
    pid = os.getppid()
    for _ in range(MAX_ANCESTORS):
        if pid <= 1:
            break
        found, parent = _console_of_process(pid)
        if found is not None:
            return found
        if parent is None:
            break
        pid = parent
    return None


class Consoles:
    def __init__(self, vt=None):
        self.fd = None
        self.watch_fd = None
        if vt is not None and not 1 <= vt <= MAX_VT:
            raise NoConsole("VT %d is out of range; consoles are 1 to %d"
                            % (vt, MAX_VT))
        if vt is None:
            vt = own_console()
        if vt is None:
            raise NoConsole(
                "cannot tell which console btkey was started on - it needs "
                "a real one, not ssh, not tmux, not a terminal window; "
                "--vt N names one outright")
        self.vt = vt
        try:
            self.fd = os.open(CONSOLE_DEVICE % vt, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise NoConsole("cannot open %s (%s) - btkey needs a console of "
                            "its own, and that one is not ours to open"
                            % (CONSOLE_DEVICE % vt, exc.strerror))

    def close(self):
        for name in ("fd", "watch_fd"):
            handle = getattr(self, name)
            if handle is not None:
                os.close(handle)
                setattr(self, name, None)

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
        """Which console is in front, asked of the one we hold.

        VT_GETSTATE answers for the layer, not for the console it is
        asked on, so ours will do.  Asked rather than read from the
        attribute the switch is watched on: reading that is what arms
        it, and a read at the wrong moment swallows a notification.
        """
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
