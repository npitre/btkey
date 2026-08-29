# SPDX-License-Identifier: GPL-2.0-only
"""A forked helper that undoes our system-wide side effects if we die badly.

btkey leaves two things behind that the kernel will not clean up on its own:
the system bluetoothd has been stopped in favour of our private one, and
the console has a scrolling region reserving its bottom line.  The keyboard
grabs need no such help - EVIOCGRAB lives on the open file description, so
the kernel drops it when we die however we die - but a machine left with no
bluetoothd, or a console whose last line never scrolls again, would both be
genuinely unpleasant surprises.

The console has to be reached by device rather than through our own stdout,
because under sudo that stdout is a pty which dies with us; the scrolling
region belongs to the VT itself and outlives both.

The guardian also watches for the opposite failure.  Dying releases the
grabs; *hanging* does not, and a wedged btkey would leave the keyboard
inert with no way to type a rescue command - a bad place to put anyone,
and a worse one for someone who cannot read a frozen screen.  So the
parent sends a heartbeat, and if it goes quiet for too long the guardian
SIGKILLs it, which hands the keyboard straight back.

Normal exits and catchable signals are handled by the main process.  SIGKILL,
an OOM kill, and a segfault inside a C extension are not: by definition
nothing in the dying process gets to run.

So we fork a guardian before acquiring anything.  The parent holds the write
end of a pipe and describes each side effect as it happens.  If the parent
exits cleanly it says DONE; if it dies by any means at all, the pipe reaches
EOF and the guardian performs the accumulated cleanup.

The guardian calls setsid() and ignores the job-control signals, so a Ctrl+C
or a `kill` aimed at the process group cannot take it down with the parent.
The parent blocks until that has happened, because otherwise a kill arriving
early enough would win the race.
"""

import fcntl
import os
import select
import signal
import struct
import subprocess

from .evdev import EVIOCSREP

# Patched by the tests; the guardian must reach the VT by device, not
# through a stdout that has died along with the process it belonged to.
CONSOLE_DEVICE = "/dev/tty%d"


def spawn():
    """Fork the guardian.  Returns a Guardian for the parent to talk to."""
    read_fd, write_fd = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        os.close(ready_read)
        try:
            _guard(read_fd, ready_write)
        finally:
            os._exit(0)
    os.close(read_fd)
    os.close(ready_write)
    try:
        os.read(ready_read, 1)
    finally:
        os.close(ready_read)
    os.set_inheritable(write_fd, False)
    return Guardian(write_fd, pid)


class Guardian:
    def __init__(self, write_fd, pid):
        self.write_fd = write_fd
        self.pid = pid

    def _send(self, line):
        if self.write_fd is None:
            return
        try:
            os.write(self.write_fd, (line + "\n").encode())
        except OSError:
            self.write_fd = None

    def kill_on_death(self, pid, comm):
        """comm guards against PID reuse in the window after we die."""
        self._send("KILL %d %s" % (pid, comm))

    def watch_me(self, seconds):
        """Ask to be SIGKILLed if we stop sending heartbeats for `seconds`."""
        self._send("WATCHDOG %d" % seconds)

    def heartbeat(self):
        self._send("PING")

    def start_unit_on_death(self, unit):
        self._send("SYSTEMCTL %s" % unit)

    def reset_console_on_death(self, vt):
        """Undo the DECSTBM scrolling region on that VT."""
        self._send("RESETVT %d" % vt)

    def forget_repeat(self, path):
        """That keyboard's repeat needs no putting back after all.

        Either btkey has put it back itself, or the device has gone and
        whatever takes its place is not ours to configure.
        """
        self._send("NOREPEAT %s" % path)

    def restore_repeat_on_death(self, path, delay, period):
        """Put a keyboard's key repeat back if we never get to.

        btkey turns autorepeat off on the keyboards it holds, and that
        setting belongs to the device: killed before it can undo that,
        it would leave a keyboard that types one character however long
        you hold a key, with nothing to say why.
        """
        self._send("REPEAT %d %d %s" % (delay, period, path))

    def dismiss(self):
        """Clean exit: tell the guardian to stand down and reap it."""
        self._send("DONE")
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def _guard(read_fd, ready_fd):
    os.setsid()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
                   signal.SIGQUIT, signal.SIGTTOU, signal.SIGTTIN,
                   signal.SIGPIPE):
        signal.signal(signum, signal.SIG_IGN)
    # Only now is it safe for the parent to carry on.
    try:
        os.write(ready_fd, b"1")
        os.close(ready_fd)
    except OSError:
        pass

    kills, units, consoles, repeats = [], [], [], {}
    watchdog = None
    parent = os.getppid()
    buffer = b""
    while True:
        if watchdog is not None:
            ready, _, _ = select.select([read_fd], [], [], watchdog)
            if not ready:
                # The parent is alive but no longer running its main loop.
                # Kill it so the kernel releases the keyboard grabs.
                try:
                    os.kill(parent, signal.SIGKILL)
                except OSError:
                    pass
                break
        try:
            chunk = os.read(read_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            command = line.decode("utf-8", "replace").strip()
            if command == "DONE":
                return
            if command == "PING":
                continue
            if command.startswith("WATCHDOG "):
                seconds = int(command[9:])
                watchdog = seconds if seconds > 0 else None
                continue
            _record(command, kills, units, consoles, repeats)

    _cleanup(kills, units, consoles, repeats)


def _record(command, kills, units, consoles, repeats):
    """File one request from the parent against the day it dies.

    Separate from the loop above so it can be read back without a
    process to run it in: the loop's own business - the deadline, the
    heartbeats, DONE - stays there.
    """
    if command.startswith("KILL "):
        pid, _, comm = command[5:].partition(" ")
        kills.append((int(pid), comm))
    elif command.startswith("SYSTEMCTL "):
        units.append(command[10:])
    elif command.startswith("RESETVT "):
        consoles.append(int(command[8:]))
    elif command.startswith("NOREPEAT "):
        repeats.pop(command[9:], None)
    elif command.startswith("REPEAT "):
        # The path is last because it is the field that can hold a
        # space; taking only two splits leaves it whole.
        delay, period, path = command[7:].split(" ", 2)
        repeats[path] = (int(delay), int(period))


def _cleanup(kills, units, consoles, repeats=None):
    for path, (delay, period) in (repeats or {}).items():
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, EVIOCSREP, struct.pack("II", delay, period))
        except OSError:
            pass
        finally:
            os.close(fd)

    for number in consoles:
        try:
            fd = os.open(CONSOLE_DEVICE % number, os.O_WRONLY | os.O_NOCTTY)
        except OSError:
            continue
        try:
            os.write(fd, b"\033[r")     # scrolling region back to the full screen
        except OSError:
            pass
        finally:
            os.close(fd)

    for pid, comm in kills:
        # PR_SET_PDEATHSIG has almost certainly done this already; this is
        # the fallback.  Check what the PID actually is first, in case it
        # was recycled between our death and now.
        try:
            with open("/proc/%d/comm" % pid) as handle:
                if handle.read().strip() != comm:
                    continue
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for unit in units:
        try:
            subprocess.run(["systemctl", "start", unit],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
