# SPDX-License-Identifier: GPL-2.0-only
"""Console output split into a scrolling log and a fixed status line.

DECSTBM (`CSI top;bottom r`) confines scrolling to part of the screen; the
rows outside that region simply stay put.  apt's fancy progress uses this
to keep its progress bar pinned to the bottom line while package output
scrolls above it, and the Linux console has supported it since forever -
console_codes(4) lists it, along with DECSC/DECRC for parking and
recovering the cursor.

btkey wants the same shape for a different reason.  BRLTTY follows the
cursor, so leaving the cursor on the first character of the current
message is what puts the braille display on it - and the line has to be
one that cannot scroll, or the reader ends up on whatever moved underneath
once the log fills the screen.

The layout is rows 1..n-1 scrolling, row n fixed:

    ┌────────────────────────────┐
    │ btkey: ...                 │  scrolling region, the log
    │ btkey: ...                 │
    ├────────────────────────────┤
    │ btkey: connected to ...    │  row n, fixed, cursor parked at col 1
    └────────────────────────────┘

Every important message goes to both: the status line so it can be read
now, the log so it is still there afterwards.

The status line can also carry an indicator ahead of the message - the
phone's lock keys - which persists across messages and is repainted on its
own.  It goes first because the cursor is parked on the first character, so
that is what a braille display lands on.
"""

import fcntl
import struct
import sys
import termios

# Two scrolling lines plus a status line is the smallest layout that makes
# any sense; below that, fall back to plain output.
MIN_ROWS = 3
DEFAULT_COLUMNS = 80

SAVE_CURSOR = "\0337"
RESTORE_CURSOR = "\0338"
CLEAR_LINE = "\033[2K"
RESET_REGION = "\033[r"


class Display:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.rows, self.columns = self._size() or (0, DEFAULT_COLUMNS)
        self.split = self.rows >= MIN_ROWS
        self.started = False
        self.indicator = ""
        self.last_status = ""

    # -- geometry ---------------------------------------------------------

    def _size(self):
        """(rows, columns), or None if there is no terminal to measure.

        None has to be distinct from a zero row count: treating a failed
        ioctl as "the screen is now 0 rows tall" would tear the layout down
        on a transient error rather than leaving it alone.
        """
        try:
            packed = fcntl.ioctl(self.stream.fileno(), termios.TIOCGWINSZ,
                                 b"\0" * 8)
        except Exception:
            return None
        rows, columns = struct.unpack("HHHH", packed)[:2]
        if not rows:
            return None
        return rows, columns or DEFAULT_COLUMNS

    def _resized(self):
        size = self._size()
        if size is None or size == (self.rows, self.columns):
            return False
        self.rows, self.columns = size
        self.split = self.rows >= MIN_ROWS
        return True

    # -- primitives -------------------------------------------------------

    def _emit(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _park(self):
        """Cursor to the first character of the status line."""
        return "\033[%d;1H" % self.rows

    def _goto_log(self):
        """Back to wherever the log left off, inside the scrolling region."""
        return RESTORE_CURSOR

    # -- lifecycle --------------------------------------------------------

    def start(self):
        if not self.split or self.started:
            return
        self.started = True
        # DECSTBM homes the cursor, so place it deliberately afterwards
        # rather than relying on where it lands.
        self._emit("\033[1;%dr" % (self.rows - 1)
                   + "\033[%d;1H" % (self.rows - 1)
                   + SAVE_CURSOR
                   + self._park() + CLEAR_LINE)

    def close(self):
        if not self.started:
            return
        self.started = False
        # Wipe the status line, give the whole screen back, then leave the
        # cursor below the last log line so the shell prompt has somewhere
        # to go that is not on top of a message.
        self._emit(self._park() + CLEAR_LINE
                   + RESET_REGION
                   + "\033[%d;1H\n" % (self.rows - 1))

    # -- output -----------------------------------------------------------

    def log(self, text):
        """Routine progress.  Scrolls; nothing has to read it."""
        if not self.started:
            self._emit(text + "\n")
            return
        self._emit(self._goto_log() + text + "\n" + SAVE_CURSOR + self._park())

    def status(self, text):
        """Something worth reading: onto the fixed line, under the cursor.

        Also logged, so the history stays complete - the status line only
        ever holds the latest one.
        """
        self.last_status = text
        if self._resized():
            if self.started and not self.split:
                # Shrunk below a usable split.  Take the scrolling region
                # down here: start() will not put one back, and close()
                # leaves it alone once started is false - so the bottom
                # line would stay frozen for the rest of the session, and
                # after btkey exits.
                self._emit(RESET_REGION)
            self.started = False
            self.start()
        if not self.started:
            self._emit(text + "\n")
            return
        self._emit(self._goto_log() + text + "\n" + SAVE_CURSOR)
        self._repaint_status()

    def set_indicator(self, text):
        """Standing indicator shown ahead of the message, or "" for none.

        Repainted without disturbing the message or the log, so a lock key
        changing does not cost the reader whatever the line was saying.
        """
        if text == self.indicator:
            return
        self.indicator = text
        self._repaint_status()

    def _status_line(self):
        parts = [part for part in (self.indicator, self.last_status) if part]
        return " ".join(parts)[:self.columns]

    def _repaint_status(self):
        """Redraw the reserved line only.  The cursor is already on it."""
        if not self.started:
            return
        self._emit(self._park() + CLEAR_LINE + self._status_line()
                   + self._park())

    def bell(self):
        """BRLTTY monitors the console bell; used for time-critical prompts."""
        self._emit("\a")
