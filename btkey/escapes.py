# SPDX-License-Identifier: GPL-2.0-only
"""Keys that reach a console as an escape sequence rather than a character.

The keymap answers "what does this key type", and for Home the answer is
nothing at all - so a key that produces no character cannot be looked up
there, and arrives instead as a short burst of ordinary bytes that only
look like text.

This is not a corner case here.  BRLTTY's braille keyboard delivers to the
console rather than through the input subsystem, so on a braille display
every arrow, Home, End and Delete comes in this way.  Undecoded, the escape
went nowhere and the rest of the sequence - `[3~` for Delete - was typed
onto the phone as literal characters.

Two spellings are in play and which one arrives is the terminal's business,
not ours, so both are read: the Linux console's, and the xterm forms a
different TERM produces.
"""

from . import keycodes

ESC = "\x1b"

KEY_HOME, KEY_UP, KEY_PAGEUP = 102, 103, 104
KEY_LEFT, KEY_RIGHT, KEY_END = 105, 106, 107
KEY_DOWN, KEY_PAGEDOWN, KEY_INSERT, KEY_DELETE = 108, 109, 110, 111
KEY_F1, KEY_F2, KEY_F3, KEY_F4, KEY_F5, KEY_F6 = 59, 60, 61, 62, 63, 64
KEY_F7, KEY_F8, KEY_F9, KEY_F10, KEY_F11, KEY_F12 = 65, 66, 67, 68, 87, 88
# The console gives Shift+F1 to Shift+F12 as F13 to F24, and has sequences
# for them; btkey has usages for them; nothing joined the two up.
KEY_F13, KEY_F14, KEY_F15, KEY_F16 = 183, 184, 185, 186
KEY_F17, KEY_F18, KEY_F19, KEY_F20 = 187, 188, 189, 190
KEY_TAB = 15

# CSI sequences ending in a letter, and SS3 sequences, which share it.
FINAL_LETTERS = {
    "A": KEY_UP, "B": KEY_DOWN, "C": KEY_RIGHT, "D": KEY_LEFT,
    "H": KEY_HOME, "F": KEY_END,
    "P": KEY_F1, "Q": KEY_F2, "R": KEY_F3, "S": KEY_F4,
    "Z": KEY_TAB,
}

# CSI Z is backtab, which is Shift+Tab: the sequence carries the modifier
# in its identity rather than in a parameter, so it has to be put back.
IMPLICIT_MODIFIERS = {"Z": keycodes.MOD_LEFTSHIFT}

# CSI sequences ending in ~, keyed by their first parameter.
FINAL_NUMBERS = {
    1: KEY_HOME, 2: KEY_INSERT, 3: KEY_DELETE, 4: KEY_END,
    5: KEY_PAGEUP, 6: KEY_PAGEDOWN, 7: KEY_HOME, 8: KEY_END,
    11: KEY_F1, 12: KEY_F2, 13: KEY_F3, 14: KEY_F4, 15: KEY_F5,
    17: KEY_F6, 18: KEY_F7, 19: KEY_F8, 20: KEY_F9, 21: KEY_F10,
    23: KEY_F11, 24: KEY_F12,
    25: KEY_F13, 26: KEY_F14, 28: KEY_F15, 29: KEY_F16,
    31: KEY_F17, 32: KEY_F18, 33: KEY_F19, 34: KEY_F20,
}

# The Linux console's F1 to F5 are the odd ones out: ESC [ [ A.
LINUX_FUNCTION = {"A": KEY_F1, "B": KEY_F2, "C": KEY_F3,
                  "D": KEY_F4, "E": KEY_F5}

# xterm's modifier parameter is 1 + a bitmask, in this order.
MODIFIER_BITS = (keycodes.MOD_LEFTSHIFT, keycodes.MOD_LEFTALT,
                 keycodes.MOD_LEFTCTRL, keycodes.MOD_LEFTMETA)

def modifiers_from(parameter):
    """Turn xterm's modifier parameter into HID modifier bits."""
    mask = max(0, parameter - 1)
    bits = 0
    for index, bit in enumerate(MODIFIER_BITS):
        if mask & (1 << index):
            bits |= bit
    return bits


def parameters(text):
    """The numeric parameters of a CSI sequence; empty ones count as 0."""
    if not text:
        return []
    return [int(part) if part.isdigit() else 0 for part in text.split(";")]


def match(text, start):
    """Read one escape sequence at `start`.

    Returns (steps, length), where steps is the keystrokes to send, or
    None for a sequence that is complete but stands for no key we can
    send.  A length of 0 means the sequence is still arriving and the
    caller should keep the tail for the next read.
    """
    rest = text[start:]
    if len(rest) < 2:
        return None, 0

    if rest[1] == "O":                          # SS3
        if len(rest) < 3:
            return None, 0
        key = FINAL_LETTERS.get(rest[2])
        if key is None:
            return None, 3
        return ((key, IMPLICIT_MODIFIERS.get(rest[2], 0)),), 3

    if rest[1] != "[":
        # ESC followed by an ordinary character.  A terminal means Alt by
        # that, but so does a person who pressed Escape and then typed, and
        # nothing here can tell the two apart - so it is read as Escape and
        # the character stands on its own.
        return None, 1

    if len(rest) < 3:
        return None, 0

    if rest[2] == "[":                          # the Linux console's F1..F5
        if len(rest) < 4:
            return None, 0
        key = LINUX_FUNCTION.get(rest[3])
        return (((key, 0),) if key else None), 4

    index = 2
    while index < len(rest) and (rest[index].isdigit() or rest[index] == ";"):
        index += 1
    if index >= len(rest):
        return None, 0                          # parameters still arriving

    final = rest[index]
    params = parameters(rest[2:index])
    if final == "~":
        key = FINAL_NUMBERS.get(params[0] if params else 0)
    else:
        key = FINAL_LETTERS.get(final)
    if key is None:
        return None, index + 1                  # whole sequence, no key
    modifiers = (modifiers_from(params[1] if len(params) > 1 else 1)
                 | IMPLICIT_MODIFIERS.get(final, 0))
    return ((key, modifiers),), index + 1


def decode(text):
    """Split text into items, plus any incomplete sequence at the end.

    An item is ("char", c) for something to look up in the keymap,
    ("steps", steps) for keystrokes to send as they are, or ("unknown",
    raw) for a complete sequence standing for no key we can send - which
    is reported rather than typed, since typing it puts `[3~` on the phone.

    The tail is the start of a sequence whose remainder has not arrived.
    It belongs at the front of the next call.
    """
    items, index = [], 0
    while index < len(text):
        char = text[index]
        if char != ESC:
            items.append(("char", char))
            index += 1
            continue
        steps, length = match(text, index)
        if length == 0:
            return items, text[index:]
        if length == 1:
            items.append(("char", ESC))         # a bare Escape
            index += 1
            continue
        if steps is None:
            items.append(("unknown", text[index:index + length]))
        else:
            items.append(("steps", steps))
        index += length
    return items, ""


def spell(raw):
    """A readable form of a sequence that could not be decoded."""
    return "ESC " + raw[1:] if raw.startswith(ESC) else raw
