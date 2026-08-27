# SPDX-License-Identifier: GPL-2.0-only
"""Keysym decoding and dead-key composition.

Getting this wrong is silent: a mis-decoded keymap does not crash, it just
types the wrong characters into someone's phone.

Every value here is what KDGKBENT actually returns, not what the kernel
stores internally.  The two differ by the U() macro's ^ 0xf000, and
conflating them is the exact bug this file exists to prevent: it leaves
KT_LATIN keys working by coincidence while every letter silently decodes
to a character from another script.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import kbmap


class KeysymTest(unittest.TestCase):
    def test_letters_are_kt_letter_not_bare_unicode(self):
        """The regression: 0x0b61 is 'a', not U+0B61 (an Oriya vowel).

        Letters are KT_LETTER so CapsLock can act on them, so this path
        covers the entire alphabet - the symptom was that digits and
        punctuation pasted fine while no letter did.
        """
        self.assertEqual(kbmap.keysym_to_char(0x0B61), "a")
        self.assertEqual(kbmap.keysym_to_char(0x0B41), "A")
        self.assertEqual(kbmap.keysym_to_char(0x0B7A), "z")

    def test_kt_latin_carries_a_latin1_character(self):
        self.assertEqual(kbmap.keysym_to_char(0x0031), "1")
        self.assertEqual(kbmap.keysym_to_char(0x002C), ",")
        self.assertEqual(kbmap.keysym_to_char(0xF0B1), "±")

    def test_bare_values_are_unicode_code_points(self):
        self.assertEqual(kbmap.keysym_to_char(0xD0AC), "€")   # U+20AC

    def test_non_character_types_are_rejected(self):
        self.assertIsNone(kbmap.keysym_to_char(0x0100))   # KT_FN, F1
        self.assertIsNone(kbmap.keysym_to_char(0x0201))   # KT_SPEC, Enter
        self.assertIsNone(kbmap.keysym_to_char(0x0400))   # KT_DEAD, grave

    def test_control_characters_are_not_typable(self):
        self.assertIsNone(kbmap.keysym_to_char(0x0000))   # unassigned
        self.assertIsNone(kbmap.keysym_to_char(0x0001))   # Ctrl+A


class DiacriticTest(unittest.TestCase):
    def test_dead_keys_resolve_to_their_diacritic(self):
        self.assertEqual(kbmap.keysym_to_diacritic(0x0400), "`")
        self.assertEqual(kbmap.keysym_to_diacritic(0x0402), "^")
        self.assertEqual(kbmap.keysym_to_diacritic(0x0405), ",")

    def test_ordinary_keys_are_not_dead_keys(self):
        self.assertIsNone(kbmap.keysym_to_diacritic(0x0B61))   # 'a'
        self.assertIsNone(kbmap.keysym_to_diacritic(0x0031))   # '1'

    def test_dead_key_index_out_of_range(self):
        self.assertIsNone(kbmap.keysym_to_diacritic(0x04FF))


class ComposeTest(unittest.TestCase):
    SINGLES = {"a": (30, 0), "e": (18, 0), "c": (46, 0), "é": (53, 0)}
    DEADS = {"`": (40, 0), ",": (27, 0)}
    TABLE = [("`", "a", "à"), ("`", "e", "è"), (",", "c", "ç"),
             ("'", "e", "é"), ("~", "n", "ñ")]

    def compose(self):
        return kbmap.compose(self.SINGLES, self.DEADS, self.TABLE)

    def test_composes_two_keystroke_sequences(self):
        composed = self.compose()
        self.assertEqual(composed["à"], ((40, 0), (30, 0)))
        self.assertEqual(composed["ç"], ((27, 0), (46, 0)))

    def test_a_single_keystroke_wins(self):
        """é is on a key of its own here, so it must not become a sequence."""
        self.assertNotIn("é", self.compose())

    def test_unreachable_diacritics_are_skipped(self):
        """No tilde dead key on this keymap, so ñ simply is not available."""
        self.assertNotIn("ñ", self.compose())


class WhitespaceTest(unittest.TestCase):
    """Pasted whitespace, which the keymap has no entries for.

    Newline is the one that matters.  Enter sends in most chat apps, so
    pasting two lines with plain Enter fires the first off as a message
    before the second arrives - destructive, and not undoable.  Shift+Enter
    inserts a line break there and behaves as an ordinary newline in a
    plain text field, so it is the safer default in both.
    """

    def test_newline_pastes_as_shift_enter_by_default(self):
        self.assertEqual(kbmap.whitespace()["\n"],
                         ((kbmap.KEY_ENTER, kbmap.MOD_LEFTSHIFT),))

    def test_plain_enter_is_available_for_a_terminal(self):
        self.assertEqual(kbmap.whitespace(False)["\n"],
                         ((kbmap.KEY_ENTER, 0),))

    def test_tab_and_space_are_unmodified_either_way(self):
        for shift in (True, False):
            table = kbmap.whitespace(shift)
            self.assertEqual(table["\t"], ((kbmap.KEY_TAB, 0),))
            self.assertEqual(table[" "], ((kbmap.KEY_SPACE, 0),))


class ControlKeyTest(unittest.TestCase):
    """Keys whose meaning is a control code, not a printable character.

    The console keymap maps keys to characters; inverting it gives back
    only the keys that produce something printable.  Enter, Tab, Space and
    Backspace fall out, and have to be put back by hand - or a Backspace
    arriving as text goes nowhere, silently, since there is nothing to
    print in the complaint either.
    """

    def table(self, shift_newline=True):
        return kbmap.whitespace(shift_newline)

    def test_backspace_arrives_as_delete(self):
        # What a terminal in raw mode sends for the Backspace key.
        self.assertEqual(self.table()["\x7f"], ((kbmap.KEY_BACKSPACE, 0),))

    def test_backspace_arrives_as_control_h(self):
        # What the other sort of terminal sends for it.
        self.assertEqual(self.table()["\x08"], ((kbmap.KEY_BACKSPACE, 0),))

    def test_tab_is_a_key_not_a_character(self):
        self.assertEqual(self.table()["\t"], ((kbmap.KEY_TAB, 0),))

    def test_escape_is_a_key(self):
        self.assertEqual(self.table()["\x1b"], ((kbmap.KEY_ESC, 0),))

    def test_carriage_return_goes_where_newline_goes(self):
        table = self.table()
        self.assertEqual(table["\r"], table["\n"])

    def test_newline_pastes_as_shift_enter_by_default(self):
        self.assertEqual(self.table()["\n"],
                         ((kbmap.KEY_ENTER, kbmap.MOD_LEFTSHIFT),))

    def test_and_as_plain_enter_when_asked(self):
        self.assertEqual(self.table(shift_newline=False)["\n"],
                         ((kbmap.KEY_ENTER, 0),))

    def test_every_one_of_them_can_actually_be_sent(self):
        # A keycode with no HID usage behind it is dropped by strokes_for
        # without a word, which puts the character right back where it
        # started - unsendable, and silently so.
        from btkey import keycodes
        for char, steps in self.table().items():
            for keycode, _ in steps:
                # assertIn would print the whole keycode table as the
                # message, which buries the one number that is wrong.
                self.assertTrue(
                    keycode in keycodes.KEYBOARD,
                    "%r maps to keycode %d, which has no HID usage"
                    % (char, keycode))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
