# SPDX-License-Identifier: GPL-2.0-only
"""Inverting the console keymap: character -> key position.

btkey normally never needs this.  Keys arrive as positions and leave as
positions, and the phone applies the layout - which is the whole reason
the design is layout-agnostic.

Pasted text is the exception.  BRLTTY delivers a paste as UTF-8 bytes
pushed into the console's input queue with TIOCSTI, not as key events, so
there are no positions to forward.  To type that text on the phone we have
to work out which key produces each character, which means reading the
kernel's own keymap with KDGKBENT and inverting it.

Keysym encoding, from <linux/keyboard.h>:

    K(t, v) = (t << 8) | v

with the standard types stored biased by 0xf0 in the high byte, so an
internal value below 0xf000 is a bare Unicode code point and at or above
it the type is KTYP(x) - 0xf0.  Only KT_LATIN and KT_LETTER carry a
character; letters are KT_LETTER rather than KT_LATIN so that CapsLock can
act on them.

The trap is that KDGKBENT does not hand back the internal value.  It
returns U(x), which vt_do_kdsk_ioctl defines as x ^ 0xf000, so the bias
has to be put back before any of the above applies.  Skip that and every
KT_LATIN key still decodes correctly by coincidence - 0xf031 ^ 0xf000 is
just 0x31 - while every letter turns into nonsense: 'a' is stored as
0xfb61, arrives as 0x0b61, and reads as U+0B61, an Oriya vowel.

Accented characters mostly are not on a key at all.  On the cf keymap, é
is, but è, à and ç are dead-key compositions: a diacritic key followed by
the base letter.  The kernel holds that composition table too, reachable
with KDGKBDIACRUC, so those characters become a two-keystroke sequence
rather than a hole.  Sending the dead key as a *position* is right for the
same reason everything else here is: the phone's own layout has a dead key
in that position and will compose it exactly as the console would have.
"""

import fcntl
import struct

KDGKBENT = 0x4B46
KDGKBDIACRUC = 0x4BFA

KT_LATIN = 0
KT_DEAD = 4
KT_LETTER = 11
KT_DEAD2 = 13

# Character each dead-key index composes with, from ret_diacr[] in
# drivers/tty/vt/keyboard.c.  KT_DEAD stores an index into this; KT_DEAD2
# stores the character itself.
RET_DIACR = (
    "`", "'", "^", "~", '"', ",", "_", "U", ".", "*", "=", "c", "k", "i",
    "#", "o", "!", "?", "+", "-", ")", "(", ":", "n", ";", "$", "@",
)

# struct kbdiacrsuc: a count, then that many {diacr, base, result} triples
# of unsigned int.
DIACR_MAX = 256
DIACR_STRUCT_SIZE = 4 + DIACR_MAX * 12

# Bit positions within the keymap table index, from <linux/keyboard.h>.
KG_SHIFT = 0
KG_ALTGR = 1

# HID modifier bits those correspond to.  AltGr goes out as Right Alt,
# which is what a host with a third-level layout expects.
MOD_LEFTSHIFT = 0x02
MOD_RIGHTALT = 0x40

# Tables worth scanning, cheapest first so that a character reachable in
# more than one way is recorded with the fewest modifiers.
TABLES = (
    (0, 0),
    (1 << KG_SHIFT, MOD_LEFTSHIFT),
    (1 << KG_ALTGR, MOD_RIGHTALT),
    ((1 << KG_SHIFT) | (1 << KG_ALTGR), MOD_LEFTSHIFT | MOD_RIGHTALT),
)

MAX_KEYCODE = 128

# Keys that produce whitespace rather than a keymap character.
KEY_ENTER = 28
KEY_TAB = 15
KEY_SPACE = 57
KEY_BACKSPACE = 14
KEY_ESC = 1


def keysym_to_char(value):
    """Return the character a KDGKBENT keysym produces, or None."""
    keysym = value ^ 0xF000            # undo U(), see the module docstring
    kind = keysym >> 8
    if kind < 0xF0:
        char = chr(keysym)             # bare Unicode code point
    elif kind - 0xF0 in (KT_LATIN, KT_LETTER):
        char = chr(keysym & 0xFF)      # Latin-1 in the low byte
    else:
        return None                    # function key, dead key, console switch
    # Drops NUL, the control-table entries, and anything else with no glyph.
    return char if char.isprintable() else None


def keysym_to_diacritic(value):
    """Return the diacritic a dead key composes with, or None."""
    keysym = value ^ 0xF000
    kind = keysym >> 8
    if kind < 0xF0:
        return None
    kind -= 0xF0
    if kind == KT_DEAD:
        index = keysym & 0xFF
        return RET_DIACR[index] if index < len(RET_DIACR) else None
    if kind == KT_DEAD2:
        return chr(keysym & 0xFF)
    return None


def compose(singles, deads, diacritics):
    """Work out which characters a dead key plus a base key can reach.

    Only fills gaps: a character already on a key of its own keeps that,
    since one keystroke beats two.
    """
    composed = {}
    for diacritic, base, result in diacritics:
        if result in singles or result in composed:
            continue
        if diacritic not in deads or base not in singles:
            continue
        composed[result] = (deads[diacritic], singles[base])
    return composed


def read_diacritics(fd):
    """Read the kernel's composition table as (diacritic, base, result)."""
    buf = bytearray(DIACR_STRUCT_SIZE)
    try:
        fcntl.ioctl(fd, KDGKBDIACRUC, buf)
    except OSError:
        return []
    count = min(struct.unpack("I", buf[:4])[0], DIACR_MAX)
    table = []
    for index in range(count):
        start = 4 + index * 12
        diacritic, base, result = struct.unpack("III", buf[start:start + 12])
        try:
            table.append((chr(diacritic), chr(base), chr(result)))
        except ValueError:
            continue
    return table


def read_entry(fd, table, keycode):
    entry = struct.pack("BBH", table, keycode, 0)
    try:
        entry = fcntl.ioctl(fd, KDGKBENT, entry)
    except OSError:
        return None
    return struct.unpack("BBH", entry)[2]


def whitespace(shift_newline=True):
    """The characters the keymap has no entry for, because they are keys.

    The console keymap maps keys to characters, so the keys whose meaning
    *is* the character - Enter, Tab, Space, Backspace - come back from it
    as control codes with no printable form, and inverting it leaves them
    out.  They have to be put back by hand or they arrive as nothing at
    all, which is what a terminal sends when Backspace is pressed.

    Newline is the interesting one.  Plain Enter is what the key means, but
    it is the wrong thing to *paste*: in a messaging app Enter sends, so a
    two-line paste would fire off the first line as a message before the
    second arrived.  Shift+Enter inserts a line break instead, which is
    what every such app expects and what plain text fields treat as an
    ordinary newline anyway.

    Somewhere like an SSH client the opposite is true - Enter is meant to
    run the line - so this is switchable, but it defaults to the choice
    whose failure mode is merely nothing happening rather than a
    half-finished message going out.
    """
    enter = MOD_LEFTSHIFT if shift_newline else 0
    return {
        "\n": ((KEY_ENTER, enter),),
        "\r": ((KEY_ENTER, enter),),
        "\t": ((KEY_TAB, 0),),
        " ": ((KEY_SPACE, 0),),
        # What a terminal sends for Backspace.  Which of the two depends on
        # the terminal, so both.
        "\x7f": ((KEY_BACKSPACE, 0),),
        "\x08": ((KEY_BACKSPACE, 0),),
        "\x1b": ((KEY_ESC, 0),),
    }


def load_layout(path):
    """Read a swept phone layout: {character: ((keycode, modifiers),)}.

    This is the whole of what btkey knows about the phone's keyboard once
    it exists.  The console keymap is a stand-in used only until it does:
    sweeping the phone showed the two disagreeing at nine of ninety-six
    plain and shifted positions as well as across the entire Option level,
    so a character present in one and not the other is not a gap to be
    filled from the other side.
    """
    layout = {}
    for number, fields in _layout_lines(path):
        if fields[0] == "dead":
            continue
        char, rest = fields[0], fields[1:]
        if len(rest) < 2 or len(rest) % 2:
            raise ValueError("%s:%d: expected pairs of keycode and modifiers"
                             % (path, number))
        if char.upper().startswith("U+"):
            char = chr(int(char[2:], 16))
        elif len(char) != 1:
            raise ValueError("%s:%d: %r is not one character"
                             % (path, number, char))
        steps = []
        for index in range(0, len(rest), 2):
            keycode, modifiers = int(rest[index], 0), int(rest[index + 1], 0)
            # These end up in a HID report; anything outside a byte raises
            # from bytes() deep inside a timer callback, where the failure
            # is a dead paste queue rather than a message.
            if not 0 <= keycode <= 0xFFFF or not 0 <= modifiers <= 0xFF:
                raise ValueError("%s:%d: keycode %d modifiers %d out of range"
                                 % (path, number, keycode, modifiers))
            steps.append((keycode, modifiers))
        layout[char] = tuple(steps)
    return layout


def load_dead_keys(path):
    """[(keycode, modifiers), ...] the sweep flagged as candidate dead keys.

    Kept in the same file as the layout because they come from the same
    sweep and describe the same keyboard; the compose sweep reads them to
    know what is worth probing.
    """
    deads = []
    for number, fields in _layout_lines(path):
        if fields[0] != "dead":
            continue
        if len(fields) != 3:
            raise ValueError("%s:%d: dead wants a keycode and modifiers"
                             % (path, number))
        deads.append((int(fields[1], 0), int(fields[2], 0)))
    return deads


def _layout_lines(path):
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.split("#", 1)[0].strip()
            if line:
                yield number, line.split()


def build(fd, shift_newline=True):
    """Read the loaded keymap through fd and invert it.

    Returns {character: ((keycode, hid_modifiers), ...)}, one step per
    keystroke needed - two for anything reached through a dead key.
    """
    singles, deads = {}, {}
    for table, modifiers in TABLES:
        for keycode in range(1, MAX_KEYCODE):
            value = read_entry(fd, table, keycode)
            if value is None:
                continue
            char = keysym_to_char(value)
            if char is not None:
                singles.setdefault(char, (keycode, modifiers))
                continue
            diacritic = keysym_to_diacritic(value)
            if diacritic is not None:
                deads.setdefault(diacritic, (keycode, modifiers))

    layout = {char: (step,) for char, step in singles.items()}
    layout.update(compose(singles, deads, read_diacritics(fd)))

    for char, steps in whitespace(shift_newline).items():
        layout.setdefault(char, steps)
    return layout
