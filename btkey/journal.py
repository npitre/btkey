# SPDX-License-Identifier: GPL-2.0-only
"""A file copy of everything btkey says, and stderr folded into it.

Two problems, one answer.

The console output scrolls and cannot be scrolled back to, which makes
diagnosing anything a matter of catching it live - and with a status line
that repaints itself, worse than usual.  A file does not have that problem
and can be read afterwards, or from another machine, which is how the
audio investigation was finally settled.

Separately, anything written straight to fd 2 lands wherever the cursor
happens to be, and the cursor is parked on the status line.  One warning
from GLib or dbus overwrites the one line meant to be readable, with
nothing to indicate that is what happened - which is exactly how a stray
SIGQUIT registration once left the status line reading half a traceback.
So fd 2 is piped back through the normal log path instead.
"""

import os
import time

from gi.repository import GLib

from . import fifo


class Journal:
    def __init__(self, path, on_error=None):
        self.path = path
        self.on_error = on_error or (lambda message: None)
        self.handle = None
        self.saved_stderr = None
        self.stderr_fd = None
        self.buffer = b""

    # -- the file ---------------------------------------------------------

    def open(self, banner=""):
        if not self.path:
            return
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, mode=0o755, exist_ok=True)
            # 0600: a pairing passkey is announced, and announcements
            # are recorded here.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o600)
            try:
                os.fchmod(fd, 0o600)   # an existing file, made before this
                self.handle = os.fdopen(fd, "a", buffering=1)
            except OSError:
                os.close(fd)
                raise
        except OSError as exc:
            self.on_error("cannot write %s: %s" % (self.path, exc.strerror))
            return
        if banner:
            self.record(banner)

    def record(self, message):
        if self.handle is None:
            return
        try:
            self.handle.write("%s %s\n"
                              % (time.strftime("%H:%M:%S"), message))
        except (OSError, ValueError) as exc:
            handle, self.handle = self.handle, None
            try:
                handle.close()
            except (OSError, ValueError):
                pass
            self.on_error("stopped writing %s: %s"
                          % (self.path, getattr(exc, "strerror", None) or exc))

    def close(self, banner=""):
        if self.handle is None:
            return
        if banner:
            self.record(banner)
        try:
            self.handle.close()
        except OSError:
            pass
        self.handle = None

    # -- stderr -----------------------------------------------------------

    def capture_stderr(self, on_line):
        """Redirect fd 2 into a pipe we read and hand to `on_line`."""
        read_fd, write_fd = os.pipe()
        try:
            self.saved_stderr = os.dup(2)
            os.dup2(write_fd, 2)
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            self.saved_stderr = None
            return
        os.close(write_fd)
        self.stderr_fd = read_fd
        GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, read_fd,
                              GLib.IOCondition.IN, self._on_stderr, on_line)

    def _on_stderr(self, fd, condition, on_line):
        try:
            data = os.read(fd, 4096)
        except OSError as exc:
            return fifo.keep_watching(exc)
        if not data:
            return False
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                on_line(text)
        return True

    def release_stderr(self):
        if self.saved_stderr is None:
            return
        try:
            os.dup2(self.saved_stderr, 2)
            os.close(self.saved_stderr)
        except OSError:
            pass
        self.saved_stderr = None
        if self.stderr_fd is not None:
            try:
                os.close(self.stderr_fd)
            except OSError:
                pass
            self.stderr_fd = None
