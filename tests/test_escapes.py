#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Keys that arrive as escape sequences.

BRLTTY's braille keyboard delivers to the console, not through the input
subsystem - measured, with tools/btkey-trace-input - so on a braille
display every arrow, Home, End and Delete comes in this way.  Undecoded,
the escape went nowhere and the rest was typed onto the phone as literal
text: pressing Delete put "[3~" in the message.

Which spelling arrives is the terminal's business, so both are read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import escapes, keycodes


def steps(text):
    """The keystrokes one sequence decodes to, or None."""
    items, tail = escapes.decode(text)
    if tail or len(items) != 1 or items[0][0] != "steps":
        return None
    return items[0][1]


def one(text):
    """(keycode, modifiers) for a sequence expected to be a single key."""
    got = steps(text)
    return got[0] if got and len(got) == 1 else None


class LinuxConsoleTest(unittest.TestCase):
    """What the console BRLTTY writes to actually sends."""

    def test_the_arrows(self):
        self.assertEqual(one("\x1b[A"), (escapes.KEY_UP, 0))
        self.assertEqual(one("\x1b[B"), (escapes.KEY_DOWN, 0))
        self.assertEqual(one("\x1b[C"), (escapes.KEY_RIGHT, 0))
        self.assertEqual(one("\x1b[D"), (escapes.KEY_LEFT, 0))

    def test_delete(self):
        # The one that used to put "[3~" on the phone.
        self.assertEqual(one("\x1b[3~"), (escapes.KEY_DELETE, 0))

    def test_home_end_insert_and_the_pages(self):
        self.assertEqual(one("\x1b[1~"), (escapes.KEY_HOME, 0))
        self.assertEqual(one("\x1b[2~"), (escapes.KEY_INSERT, 0))
        self.assertEqual(one("\x1b[4~"), (escapes.KEY_END, 0))
        self.assertEqual(one("\x1b[5~"), (escapes.KEY_PAGEUP, 0))
        self.assertEqual(one("\x1b[6~"), (escapes.KEY_PAGEDOWN, 0))

    def test_the_consoles_own_function_keys(self):
        self.assertEqual(one("\x1b[[A"), (escapes.KEY_F1, 0))
        self.assertEqual(one("\x1b[[E"), (escapes.KEY_F5, 0))
        self.assertEqual(one("\x1b[17~"), (escapes.KEY_F6, 0))


class HighFunctionKeyTest(unittest.TestCase):
    """F13 to F20, which the console gives for Shift+F1 to Shift+F8.

    btkey has had HID usages for these all along and no way to recognise
    them arriving; Fn+F11 on one keyboard turned out to produce ESC [ 26 ~
    alongside its volume key, which is F14.
    """

    # The Linux keycodes for F13 to F20, written out rather than taken
    # from the module: comparing its constants against themselves would
    # pass with any two of them equal.
    EXPECTED = {25: 183, 26: 184, 28: 185, 29: 186,
                31: 187, 32: 188, 33: 189, 34: 190}

    def test_each_one_reaches_its_own_key(self):
        for number, keycode in self.EXPECTED.items():
            self.assertEqual(one("\x1b[%d~" % number), (keycode, 0), number)

    def test_no_two_of_them_are_the_same_key(self):
        got = [one("\x1b[%d~" % number)[0] for number in self.EXPECTED]
        self.assertEqual(len(set(got)), len(got), got)

    def test_the_gaps_in_the_numbering_are_gaps(self):
        # 27 and 30 are not function keys in that table, and inventing
        # keys for them would send the phone something nobody pressed.
        for number in (27, 30):
            items, _ = escapes.decode("\x1b[%d~" % number)
            self.assertEqual(items[0][0], "unknown", number)

    def test_they_carry_modifiers_like_the_rest(self):
        self.assertEqual(one("\x1b[26;5~"), (184, keycodes.MOD_LEFTCTRL))


class XtermTest(unittest.TestCase):
    """The other spelling, for a console that is not the Linux one."""

    def test_ss3_arrows(self):
        self.assertEqual(one("\x1bOA"), (escapes.KEY_UP, 0))
        self.assertEqual(one("\x1bOD"), (escapes.KEY_LEFT, 0))

    def test_ss3_home_and_end(self):
        self.assertEqual(one("\x1bOH"), (escapes.KEY_HOME, 0))
        self.assertEqual(one("\x1bOF"), (escapes.KEY_END, 0))

    def test_csi_home_and_end(self):
        self.assertEqual(one("\x1b[H"), (escapes.KEY_HOME, 0))
        self.assertEqual(one("\x1b[F"), (escapes.KEY_END, 0))


class ModifierTest(unittest.TestCase):
    """Ctrl+Left is a word, which is how anyone navigates text."""

    def test_ctrl_left(self):
        self.assertEqual(one("\x1b[1;5D"),
                         (escapes.KEY_LEFT, keycodes.MOD_LEFTCTRL))

    def test_shift_right_selects(self):
        self.assertEqual(one("\x1b[1;2C"),
                         (escapes.KEY_RIGHT, keycodes.MOD_LEFTSHIFT))

    def test_ctrl_shift_left(self):
        self.assertEqual(one("\x1b[1;6D"),
                         (escapes.KEY_LEFT,
                          keycodes.MOD_LEFTSHIFT | keycodes.MOD_LEFTCTRL))

    def test_a_modified_tilde_sequence(self):
        self.assertEqual(one("\x1b[3;5~"),
                         (escapes.KEY_DELETE, keycodes.MOD_LEFTCTRL))

    def test_no_modifier_parameter_means_none(self):
        self.assertEqual(one("\x1b[1;1D"), (escapes.KEY_LEFT, 0))


class BacktabTest(unittest.TestCase):
    """Shift+Tab, which is how you go backwards through anything."""

    def test_backtab_is_shift_tab(self):
        # CSI Z carries the modifier in the sequence's identity rather
        # than in a parameter, so it has to be put back by hand.
        self.assertEqual(one("\x1b[Z"),
                         (escapes.KEY_TAB, keycodes.MOD_LEFTSHIFT))

    def test_the_ss3_spelling_too(self):
        self.assertEqual(one("\x1bOZ"),
                         (escapes.KEY_TAB, keycodes.MOD_LEFTSHIFT))

    def test_a_parameter_adds_to_the_implicit_modifier(self):
        # Ctrl+Shift+Tab, which is a key combination in its own right.
        self.assertEqual(one("\x1b[1;5Z"),
                         (escapes.KEY_TAB,
                          keycodes.MOD_LEFTSHIFT | keycodes.MOD_LEFTCTRL))

    def test_plain_tab_is_still_a_character(self):
        # It arrives as \t and goes through the keymap, not through here.
        items, _ = escapes.decode("\t")
        self.assertEqual(items, [("char", "\t")])


class SplitReadTest(unittest.TestCase):
    """A sequence arriving in pieces, which a slow writer will do."""

    def test_an_incomplete_sequence_is_held(self):
        items, tail = escapes.decode("\x1b[")
        self.assertEqual(items, [])
        self.assertEqual(tail, "\x1b[")

    def test_a_lone_escape_is_held_rather_than_guessed_at(self):
        items, tail = escapes.decode("\x1b")
        self.assertEqual(tail, "\x1b")

    def test_the_pieces_join_up(self):
        first, tail = escapes.decode("ab\x1b[3")
        rest, over = escapes.decode(tail + "~")
        self.assertEqual([kind for kind, _ in first], ["char", "char"])
        self.assertEqual(rest, [("steps", ((escapes.KEY_DELETE, 0),))])
        self.assertEqual(over, "")

    def test_parameters_still_arriving_are_held(self):
        items, tail = escapes.decode("\x1b[1;5")
        self.assertEqual(items, [])
        self.assertEqual(tail, "\x1b[1;5")


class PassThroughTest(unittest.TestCase):
    def test_ordinary_text_is_untouched(self):
        items, tail = escapes.decode("hello")
        self.assertEqual(items, [("char", c) for c in "hello"])
        self.assertEqual(tail, "")

    def test_text_around_a_sequence_survives(self):
        items, _ = escapes.decode("a\x1b[Cb")
        self.assertEqual(items[0], ("char", "a"))
        self.assertEqual(items[2], ("char", "b"))

    def test_escape_then_a_letter_is_escape_then_a_letter(self):
        # A terminal means Alt by that; so does someone who pressed Escape
        # and then typed, and nothing here can tell them apart.
        items, _ = escapes.decode("\x1bx")
        self.assertEqual(items, [("char", "\x1b"), ("char", "x")])

    def test_an_undecodable_sequence_is_swallowed_whole(self):
        # Not typed.  Letting the tail through is what put "[3~" on the
        # phone in the first place.
        items, _ = escapes.decode("\x1b[200~")
        self.assertEqual(items, [("unknown", "\x1b[200~")])

    def test_an_unknown_sequence_is_named_readably(self):
        self.assertEqual(escapes.spell("\x1b[200~"), "ESC [200~")


class ReachableTest(unittest.TestCase):
    def test_every_key_in_the_tables_can_be_sent(self):
        """A keycode with no HID usage is dropped further downstream."""
        tables = (list(escapes.FINAL_LETTERS.values())
                  + list(escapes.FINAL_NUMBERS.values())
                  + list(escapes.LINUX_FUNCTION.values()))
        for keycode in tables:
            self.assertIn(keycode, keycodes.KEYBOARD,
                          "keycode %d has no HID usage" % keycode)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
