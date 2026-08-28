# SPDX-License-Identifier: GPL-2.0-only
"""Typing pasted text, which is not the same problem as forwarding keys.

BRLTTY's Linux screen driver picks how to inject a paste from the console's
keyboard mode: raw modes go through uinput, but K_XLATE and K_UNICODE -
what a normal console is in - go through TIOCSTI, pushing UTF-8 straight
into the tty's input queue.  That never touches the input layer, so the
evdev grab cannot see it, and there are no keycodes to forward.

So those bytes are read from the tty instead - stdin, where sudo's pty
relay delivers them - and turned back into key positions by inverting the
console keymap.  It is the one place btkey is not layout-agnostic.

stdin is the only way in.  A FIFO would be more convenient, but anything
running as the same user could write to it, and that is a keystroke
injector into someone's phone with no way to tell where the text came
from.  Reading only from the console btkey was started on means the text
has to come from someone sitting at it.

The link is shared with the key path, so a paste borrows it and hands the
physical key state back when the queue drains.

Note that a *pasted* newline is not the same keystroke as a pressed one:
see kbmap.whitespace.  Pressing Enter goes through the ordinary key path
and is untouched by any of this.
"""

import codecs
import collections
import os
import termios

from gi.repository import GLib

from . import escapes, fifo, kbmap, keycodes

# One HID report per tick.  Most characters cost one report - see
# _stroke - so this is upwards of a hundred a second, fast enough not to
# feel like waiting and slow enough that the host does not drop keys.
INTERVAL_MS = 8
QUEUE_LIMIT = 20000

# How long to hold an escape sequence that has not finished arriving.  A
# terminal sends one in a single write, so this is only ever waiting on a
# key that was pressed alone: Escape itself, which has to reach the phone
# rather than sit here forever waiting to become an arrow.
ESCAPE_WAIT_MS = 60


# Names for the characters that have no printable form, so that a dropped
# one can be reported.  Reporting it as itself would print nothing at all,
# which is how a Backspace arriving from a braille display and going
# nowhere looked exactly like a Backspace that was never pressed.
CONTROL_NAMES = {
    "\x00": "NUL", "\x08": "Backspace", "\t": "Tab", "\n": "Enter",
    "\r": "Return", "\x1b": "Escape", "\x7f": "Backspace",
}


def describe(char):
    """A name for a character, for saying that it could not be typed."""
    if char in CONTROL_NAMES:
        return CONTROL_NAMES[char]
    if char.isprintable():
        return char
    return "U+%04X" % ord(char)


class Typist:
    def __init__(self, link, log, is_foreground, on_idle,
                 shift_newline=True, layout_path=None):
        self.link = link
        self.log = log
        self.is_foreground = is_foreground
        self.on_idle = on_idle
        self.shift_newline = shift_newline
        self.layout_path = layout_path
        self.keymap = {}
        self.queue = collections.deque()
        self.timer = None
        # A half-arrived escape sequence, and the timer that decides it was
        # never one.
        self.pending = ""
        self.escape_timer = None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.saved_stdin = None

    # -- the keymap -------------------------------------------------------

    def load_keymap(self, fd):
        """The phone's own layout where there is one, the console's if not.

        btkey sends key positions and the phone decides what they type, so
        the console keymap is a guess about the phone and nothing more.
        Where a measured layout exists it is not merely the better of two
        answers, it is the only one that can be right: a character the
        console can type and the measured layout does not list is a
        character no position on the phone produces, so falling back to the
        console's answer does not type it, it types something else.  Saying
        the character cannot be sent is the better failure by a distance.

        What comes from the console side regardless are the keys whose
        meaning is the key and not a character: Enter, Tab, Space,
        Backspace, Escape.  Those are positions on any keyboard, and a
        measured layout has nothing to say about them.
        """
        console = kbmap.build(fd, self.shift_newline)
        self.log("console keymap: %d characters typeable%s"
                 % (len(console),
                    "; newline pastes as Shift+Enter"
                    if self.shift_newline else "; newline pastes as Enter"))
        if not self.layout_path:
            self.keymap = console
            return
        try:
            layout = kbmap.load_layout(self.layout_path)
        except (OSError, ValueError) as exc:
            self.keymap = console
            self.log("ignoring %s: %s" % (self.layout_path, exc))
            return

        self.keymap = dict(kbmap.whitespace(self.shift_newline))
        self.keymap.update(layout)

        added = sum(1 for char in layout if char not in console)
        changed = sum(1 for char, steps in layout.items()
                      if char in console and console[char] != steps)
        self.log("phone layout %s: %d entries, %d new, %d corrected"
                 % (os.path.basename(self.layout_path), len(layout),
                    added, changed))

        dropped = sorted(char for char in console if char not in self.keymap)
        if dropped:
            shown = " ".join(describe(char) for char in dropped[:24])
            if len(dropped) > 24:
                shown += " ..."
            self.log("%d the console has and the phone does not, so they "
                     "cannot be sent: %s" % (len(dropped), shown))

    def strokes_for(self, char):
        """[(modifiers, usage), ...] to type char, or None if unreachable."""
        steps = self.keymap.get(char)
        if not steps:
            return None
        strokes = []
        for keycode, modifiers in steps:
            usage = keycodes.KEYBOARD.get(keycode)
            if usage is None:
                return None
            strokes.append((modifiers, usage))
        return strokes

    # -- queueing and draining --------------------------------------------

    def type_text(self, text):
        """Queue text to be typed on the phone, as key positions.

        Characters are looked up in the console keymap, so what arrives is
        whatever that key would have produced locally - which lines up on
        the phone as long as its hardware layout matches this console's.
        Accented characters may need two keystrokes, a dead key and then
        the base letter; the keymap says which.

        A key that produces no character - an arrow, Home, Delete - has no
        keymap entry to find, and arrives as an escape sequence instead.
        Those are decoded into key positions before any of the above.
        """
        if not self.link.connected:
            self.log("not connected; dropped %d characters" % len(text))
            return
        if len(self.queue) > QUEUE_LIMIT:
            self.log("type queue full; dropped %d characters" % len(text))
            return

        items, self.pending = escapes.decode(self.pending + text)
        self._wait_for_the_rest()

        unknown = []
        for kind, value in items:
            if kind == "steps":
                for keycode, modifiers in value:
                    usage = keycodes.KEYBOARD.get(keycode)
                    if usage is None:
                        unknown.append(keycodes.key_name(keycode))
                    else:
                        self._stroke(modifiers, usage)
                continue
            if kind == "unknown":
                label = escapes.spell(value)
                if label not in unknown:
                    unknown.append(label)
                continue
            strokes = self.strokes_for(value)
            if strokes is None:
                label = describe(value)
                if label not in unknown:
                    unknown.append(label)
                continue
            for modifiers, usage in strokes:
                self._stroke(modifiers, usage)
        if unknown:
            self.log("nothing to send for: %s" % " ".join(unknown))
        if self.timer is None and self.queue:
            self.timer = GLib.timeout_add(INTERVAL_MS, self.drain)

    def _wait_for_the_rest(self):
        """Hold a half-arrived escape sequence, but not indefinitely.

        Escape pressed on its own is indistinguishable from the start of an
        arrow key until either the rest turns up or it does not.
        """
        if self.escape_timer is not None:
            GLib.source_remove(self.escape_timer)
            self.escape_timer = None
        if self.pending:
            self.escape_timer = GLib.timeout_add(ESCAPE_WAIT_MS,
                                                 self._give_up_waiting)

    def _give_up_waiting(self):
        """No more of it arrived, so it was never a sequence."""
        self.escape_timer = None
        held, self.pending = self.pending, ""
        for char in held:
            strokes = self.strokes_for(char)
            if strokes is None:
                self.log("nothing to send for: %s" % describe(char))
                continue
            for modifiers, usage in strokes:
                self._stroke(modifiers, usage)
        if self.timer is None and self.queue:
            self.timer = GLib.timeout_add(INTERVAL_MS, self.drain)
        return False

    def _stroke(self, modifiers, usage):
        """Queue one keystroke, releasing the last one only when needed.

        A HID keyboard reports its whole state each time, so going from
        Shift+H straight to Shift+E in a single report is what a real
        keyboard does when a typist rolls from one key to the next: the
        host sees H released and E pressed at once.  An empty report
        between every character doubles the traffic and holds no key any
        longer.

        Two cases still need the gap.  The same key twice running is
        invisible without one, because nothing in the report changes and
        the host sees a held key rather than a second press.  And a change
        of modifiers gets one too: releasing Shift and pressing the next
        key in the same report is legal, but hosts vary in whether they
        apply the old modifiers or the new ones to it, and a wrong
        character is worse than a slow one.  The gap keeps whatever the
        two have in common, so a run of capitals holds Shift throughout.
        """
        if self.queue:
            previous, keys = self.queue[-1]
            if (keys and keys[0] == usage) or previous != modifiers:
                self.queue.append((previous & modifiers, []))
        self.queue.append((modifiers, [usage]))

    def enqueue(self, strokes):
        """Queue raw (modifiers, usage) keystrokes, bypassing the keymap.

        The probe needs to press positions the keymap has no character
        for, which is the whole point of running it.  Deliberately not
        coalesced the way typed text is: a probe is measuring the host's
        behaviour, so it should present each key as plainly as possible
        rather than as fast as possible.
        """
        for modifiers, usage in strokes:
            self.queue.append((modifiers, [usage]))
            self.queue.append((0, []))
        if self.timer is None and self.queue:
            self.timer = GLib.timeout_add(INTERVAL_MS, self.drain)

    def clear(self):
        """Abandon whatever is queued, and stop.

        The queue holds a press and its release as separate reports, so
        dropping it wholesale can land between the two and leave the host
        holding that key down - autorepeating it until the next physical
        keystroke.  Handing the physical state back is what drain() does
        when it empties, and it has to happen here too.
        """
        dropped = len(self.queue)
        self.queue.clear()
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        # A half-arrived sequence is abandoned with the rest; leaving its
        # timer armed would type the tail of it after the cancel.
        self.pending = ""
        if self.escape_timer is not None:
            GLib.source_remove(self.escape_timer)
            self.escape_timer = None
        if dropped:
            self.on_idle()
        return dropped

    def drain(self):
        if not self.link.connected:
            self.queue.clear()
        if not self.queue:
            self.timer = None
            # Put the physical key state back; the paste borrowed the link.
            self.on_idle()
            return False
        modifiers, keys = self.queue.popleft()
        self.link.send_keyboard(modifiers, keys)
        return True

    # -- where the text comes from ----------------------------------------

    def on_text_input(self, fd, condition):
        try:
            data = os.read(fd, 4096)
        except OSError as exc:
            return fifo.keep_watching(exc)
        if not data:
            return False
        # Only while our console is in front: text arriving on a
        # backgrounded console is not addressed to the phone.
        if not self.is_foreground():
            return True
        text = self.decoder.decode(data)
        if text:
            self.type_text(text)
        return True

    def watch_stdin(self):
        if not os.isatty(0):
            try:
                os.fstat(0)
            except OSError:
                return
        else:
            # Stop the console echoing pasted bytes back at us, and take
            # them a byte at a time rather than a line at a time.
            try:
                self.saved_stdin = termios.tcgetattr(0)
                attrs = termios.tcgetattr(0)
                attrs[3] &= ~(termios.ECHO | termios.ICANON)
                attrs[6][termios.VMIN] = 1
                attrs[6][termios.VTIME] = 0
                termios.tcsetattr(0, termios.TCSANOW, attrs)
            except termios.error:
                self.saved_stdin = None
        GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, 0,
                              GLib.IOCondition.IN, self.on_text_input)

    def close(self):
        if self.escape_timer is not None:
            GLib.source_remove(self.escape_timer)
            self.escape_timer = None
        if self.saved_stdin is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, self.saved_stdin)
            except (termios.error, OSError):
                pass
            self.saved_stdin = None
