# SPDX-License-Identifier: GPL-2.0-only
"""Typing a probe at the phone, and saying how far it has got.

Learning a phone's layout means typing every key position at every level
and reading back what arrived, which takes about a minute of unbroken
typing.  For that minute the instruction is not to touch the keyboard, so
the one thing this has to do besides typing is say when it is over.

The bell is the part that matters.  BRLTTY monitors the console bell, so
the end arrives without having to watch for it; the running percentage in
the status line is reassurance in between, and it borrows the indicator
slot the lock keys usually have.

Kept apart from the main loop because it owns a stretch of time rather
than a moment: while a sweep is running there is a name, a queue length,
a start, and two counters read off the link, none of which anything else
looks at.
"""

import time

from gi.repository import GLib

from . import probe
from .typist import INTERVAL_MS as TYPE_INTERVAL_MS

# How often to recompute how far a sweep has got.  The display only
# repaints when the number actually changes, so a brisk poll costs
# nothing, and it stops itself the moment the queue empties.
PROGRESS_MS = 500


class Sweep:
    """One probe at a time, from the moment it is asked for to the bell."""

    def __init__(self, typist, link, display, log, announce):
        self.typist = typist
        self.link = link
        self.display = display
        self.log = log
        self.announce = announce
        self.name = None            # not None while a sweep is being typed
        self.queued = 0
        self.started = None
        self.reports = 0
        self.waiting = 0.0

    @property
    def running(self):
        return self.name is not None

    def learn_layout(self):
        """Type the first probe: every key position, at every level."""
        if not self._ready():
            return
        self.start("learning the keyboard layout", probe.capture_strokes())

    def learn_accents(self, specs):
        """Type the second probe: every candidate accent key, composed.

        A single-keystroke probe cannot see a composition, because a dead
        key followed by the space it types looks exactly like a literal
        accent.  So a second pass is unavoidable - and which keys it
        should try is decided by the *first* pass's results, which live on
        the phone.  The client works that out from the capture and sends
        the list, which is why this takes one rather than reading a file.
        """
        if not self._ready():
            return
        candidates = []
        for spec in specs:
            keycode, _, mods = spec.partition(":")
            try:
                keycode, mods = int(keycode), int(mods or 0)
            except ValueError:
                self.log("ignoring malformed accent key %r" % spec)
                continue
            # Straight off the control FIFO and into a HID report.
            if not 0 <= keycode <= 0xFFFF or not 0 <= mods <= 0xFF:
                self.log("ignoring out-of-range accent key %r" % spec)
                continue
            candidates.append((keycode, mods, ""))
        if not candidates:
            self.log("no accent keys given; run btkey --learn-accents "
                     "with the capture from --learn-layout")
            return
        self.start("learning accent keys", probe.compose_strokes(candidates))

    def _ready(self):
        if self.link.connected:
            return True
        self.log("not connected; nothing to learn from")
        return False

    def start(self, name, steps):
        """Type a labelled probe sequence, reporting how far it has got."""
        # Every keystroke here is a position, never text.  Going through
        # the console keymap would put the one mapping this exists to
        # measure in the middle of measuring it, and garble the capture on
        # any machine whose console does not match the phone.
        self.typist.enqueue(steps)

        self.name = name
        self.queued = len(self.typist.queue)
        self.started = time.monotonic()
        self.reports = self.link.sent_reports
        self.waiting = self.link.send_seconds
        self.announce("%s: about %d seconds; do not type until the bell"
                      % (name,
                         max(1, self.queued * TYPE_INTERVAL_MS // 1000)))
        GLib.timeout_add(PROGRESS_MS, self.poll)

    def poll(self):
        if not self.running:
            return False
        if not self.link.connected:
            # drain() empties the queue on a disconnect, which would
            # otherwise read as completion - bell and all - and send
            # someone off to mail a capture that stops halfway.
            self.typist.clear()
            self.finish("%s stopped: the phone disconnected" % self.name)
            return False
        remaining = len(self.typist.queue)
        if remaining:
            done = self.queued - remaining
            self.display.borrow_indicator(
                "%d%%" % (100 * done // max(self.queued, 1)))
            return True
        self.finish("%s: done. The text is on the phone; send it to "
                    "yourself." % self.name)
        return False

    def cancel(self):
        if not self.running:
            # Nothing to cancel is not the same as cancel everything: a
            # paste may well be draining.
            self.log("nothing to cancel")
            return
        dropped = self.typist.clear()
        self.finish("%s cancelled, %d keystrokes dropped"
                    % (self.name, dropped))

    def finish(self, message):
        self.log(self.timing())
        self.name = None
        self.queued = 0
        self.started = None
        self.display.return_indicator()
        self.display.bell()
        self.announce(message)

    def timing(self):
        """How long the probe took, and how much of it was the radio.

        A probe that runs slower than the estimate has two possible
        reasons and they call for different things.  If the time went into
        send(), the link is the limit: the socket blocks, so a phone that
        cannot absorb reports as fast as btkey produces them stops the
        main loop for as long as it takes, and sharing the link with A2DP
        audio is enough to do it.  If it did not, the limit is here.
        """
        if self.started is None:
            return "sweep finished"
        elapsed = time.monotonic() - self.started
        reports = self.link.sent_reports - self.reports
        waiting = self.link.send_seconds - self.waiting
        estimate = self.queued * TYPE_INTERVAL_MS / 1000.0
        return ("%d reports in %.1fs (estimated %.1fs); %.1fs of that "
                "waiting on the link" % (reports, elapsed, estimate, waiting))
