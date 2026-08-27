# SPDX-License-Identifier: GPL-2.0-only
"""Reading a layout capture back.

Every recovery rule here comes from something a real iPhone did to a real
capture: it trimmed a trailing space, it swallowed one at a dead key, it
rewrote straight quotes as curly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import kbmap, keycodes, probe

SHIFT = keycodes.MOD_LEFTSHIFT
OPTION = keycodes.MOD_RIGHTALT


class RowCaptureTest(unittest.TestCase):
    """A row per line, with a space after each key.

    Probing a key at a time and labelling each cost about 1500 keystrokes;
    this costs 400, because the row says which key each result came from.
    The space after each key is what makes it readable back, and it does
    double duty: a dead key swallows it, which is how they are found.
    """

    def read(self, text, count):
        return probe.read_row(text, count, "1")

    def test_a_key_that_produced_whitespace_is_not_nothing(self):
        """iOS inserts a non-breaking space of its own after a guillemet,
        and a key really yielding one is a character worth having."""
        self.assertEqual(self.read("a 11\u00a0 11 11", 3),
                         [("a", False), ("\u00a0", False), (None, False)])

    def test_a_key_producing_a_space_is_not_nothing_either(self):
        self.assertEqual(self.read("a 11  11", 2),
                         [("a", False), (" ", False)])

    def test_a_row_of_ordinary_keys(self):
        self.assertEqual(self.read("a 11s 11d 11", 3),
                         [("a", False), ("s", False), ("d", False)])

    def test_a_key_that_produced_nothing_keeps_its_place(self):
        """Without the space every later key in the row would shift."""
        self.assertEqual(self.read("a 11 11d 11", 3),
                         [("a", False), (None, False), ("d", False)])

    def test_a_dead_key_swallows_the_space(self):
        self.assertEqual(self.read("a 11^11d 11", 3),
                         [("a", False), ("^", True), ("d", False)])

    def test_a_dead_key_followed_by_one_that_produced_nothing(self):
        """The case that needs the delimiter.  With only spaces this reads
        as a literal caret, and every key after it shifts by one: "^ X "
        fits both [dead, nothing, literal] and [literal, dead, nothing].
        """
        self.assertEqual(self.read("^11 11X 11", 3),
                         [("^", True), (None, False), ("X", False)])

    def test_a_result_equal_to_the_marker_is_not_a_delimiter(self):
        """The key that types the marker is itself probed, which is why the
        marker is doubled: no field can hold two in a row."""
        self.assertEqual(self.read("1 11a 11", 2),
                         [("1", False), ("a", False)])

    def test_a_dead_key_at_the_end_of_a_row(self):
        self.assertEqual(self.read("a 11s 11^11", 3),
                         [("a", False), ("s", False), ("^", True)])

    def test_a_row_that_ran_out_reports_nothing_not_nonsense(self):
        self.assertEqual(self.read("a 11", 3),
                         [("a", False), (None, False), (None, False)])

    def test_sentinels_bound_the_capture_and_name_the_marker(self):
        lines = ["From: nico", "", "11111111", "a 11", "11111111", "-- "]
        self.assertEqual(probe.capture_block(lines), (["a 11"], "1"))

    def test_the_marker_is_read_off_the_sentinel(self):
        """On AZERTY that key is an ampersand, and the reader finds out
        from the capture rather than needing to know the layout."""
        lines = ["&&&&&&&&", "a &&", "&&&&&&&&"]
        self.assertEqual(probe.capture_block(lines)[1], "&")

    def test_a_capture_with_no_sentinels_is_not_one(self):
        self.assertIsNone(probe.capture_block(["a 11"]))

    def test_the_sentinel_is_whatever_that_key_produces(self):
        """On AZERTY it is a row of ampersands, and still recognisable."""
        self.assertTrue(probe._is_sentinel("&&&&&&&&"))
        self.assertTrue(probe._is_sentinel("11111111"))
        self.assertFalse(probe._is_sentinel("12345678"))
        self.assertFalse(probe._is_sentinel("        "))


class RowRoundTripTest(unittest.TestCase):
    """Generate the probe, answer it as a phone would, read it back."""

    LAYOUT = {30: "a", 31: "s", 18: "e", 23: "i", 46: "c"}
    DEAD = {(26, 0): {"e": "ê", "E": "Ê", "i": "î", " ": "^"},
            (26, SHIFT): {"e": "ë", "E": "Ë", "i": "ï", " ": "¨"}}

    def answer(self, strokes):
        """What a phone with that layout would put on the screen."""
        lines, line, pending = [], "", None
        for modifiers, usage in strokes:
            if usage == keycodes.KEYBOARD[keycodes.KEY_ENTER]:
                lines.append(line)
                line = ""
                continue
            keycode = next((k for k, u in keycodes.KEYBOARD.items()
                            if u == usage), None)
            if pending is not None:
                char = " " if keycode == probe.SPACE_POSITION else \
                    self.LAYOUT.get(keycode, "?")
                if modifiers & SHIFT:
                    char = char.upper()
                line += self.DEAD[pending].get(char, char)
                pending = None
                continue
            if (keycode, modifiers) in self.DEAD:
                pending = (keycode, modifiers)
                continue
            if keycode == probe.SPACE_POSITION:
                line += " "
            elif keycode == probe.SENTINEL_POSITION:
                line += "1"
            else:
                line += self.LAYOUT.get(keycode, "") if modifiers == 0 else ""
        if line:
            lines.append(line)
        return "\n".join(lines)

    def test_the_capture_reads_back(self):
        text = self.answer(probe.capture_strokes())
        results, problems = probe.parse_capture(text)
        self.assertEqual(problems, [])
        found = {char: steps for steps, char, _ in results}
        self.assertEqual(found["a"], ((30, 0),))
        self.assertEqual(found["e"], ((18, 0),))

    def test_dead_keys_are_identified_by_measurement(self):
        text = self.answer(probe.capture_strokes())
        results, _ = probe.parse_capture(text)
        candidates = probe.dead_key_candidates(results)
        self.assertIn((26, 0, "^"), candidates)
        self.assertIn((26, SHIFT, "¨"), candidates)

    def test_the_compose_capture_reads_back(self):
        candidates = [(26, 0, "^"), (26, SHIFT, "¨")]
        text = self.answer(probe.compose_strokes(candidates))
        results, problems = probe.parse_compositions(text, candidates)
        self.assertEqual(problems, [])
        found = {char: steps for steps, char, _ in results}
        self.assertEqual(found["ê"], ((26, 0), (18, 0)))
        self.assertEqual(found["Ê"], ((26, 0), (18, SHIFT)))
        self.assertEqual(found["ï"], ((26, SHIFT), (23, 0)))

    def test_a_wrong_number_of_rows_is_reported(self):
        results, problems = probe.parse_capture(
            "11111111\na s 1\n11111111\n")
        self.assertEqual(results, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("16 rows", problems[0])


class LayoutTest(unittest.TestCase):
    def test_fewest_modifiers_wins(self):
        layout = probe.to_layout([(((3, SHIFT),), "@", []),
                                  (((3, OPTION),), "@", []),
                                  (((30, 0),), "a", [])])
        self.assertEqual(layout["@"][0], ((3, SHIFT),))
        self.assertEqual(layout["a"][0], ((30, 0),))

    def test_plain_beats_shift(self):
        layout = probe.to_layout([(((4, SHIFT),), "#", []),
                                  (((5, 0),), "#", [])])
        self.assertEqual(layout["#"], (((5, 0),), []))

    def test_whitespace_is_left_to_its_own_keys(self):
        """The probe covers the alphanumeric block; a key there that emits
        a space is a curiosity, not a way to type one."""
        layout = probe.to_layout([((((7, OPTION)),), " ", []),
                                  ((((30, 0)),), "a", [])])
        self.assertNotIn(" ", layout)
        self.assertIn("a", layout)

    def test_one_keystroke_beats_two(self):
        """A phone may offer an accented letter directly and by composing."""
        layout = probe.to_layout([(((26, 0), (18, 0)), "è", []),
                                  (((40, 0),), "è", [])])
        self.assertEqual(layout["è"][0], ((40, 0),))

    def test_a_dead_key_is_never_a_way_of_typing_its_own_accent(self):
        """Pressing it alone types nothing and eats the next character.

        The capture does show what it produced, so it is tempting - and
        cheaper - to record it as one keystroke.  That would make pasting
        "x^y" send x, a dead circumflex, and then lose the y to it.  The
        bare accent has to come from composing with a space.
        """
        layout = probe.to_layout([
            ((((26, 0)),), "^", ["dead"]),
            (((26, 0), (57, 0)), "^", [])])
        self.assertEqual(layout["^"][0], ((26, 0), (57, 0)))

    def test_a_dead_key_with_no_composition_is_simply_absent(self):
        """Better nothing than an entry that corrupts the next character."""
        layout = probe.to_layout([((((26, 0)),), "^", ["dead"])])
        self.assertNotIn("^", layout)

    def test_a_literal_accent_key_is_kept(self):
        """Not every accent-shaped key is dead; some really do type one."""
        layout = probe.to_layout([((((43, OPTION)),), "`", [])])
        self.assertEqual(layout["`"][0], ((43, OPTION),))

    def test_two_keystrokes_are_kept_when_that_is_the_only_way(self):
        layout = probe.to_layout([(((26, 0), (18, 0)), "ê", [])])
        self.assertEqual(layout["ê"][0], ((26, 0), (18, 0)))


class RenderTest(unittest.TestCase):
    def test_it_round_trips_through_the_loader(self):
        """What render writes, load_layout has to read back unchanged."""
        results = [((((30, 0)),), "a", []),
                   ((((30, OPTION)),), "æ", []),
                   ((((26, 0)),), "^", ["dead"]),
                   (((26, 0), (18, 0)), "ê", [])]
        layout = probe.to_layout(results)
        text = probe.render(layout, missing=["k22L3 produced nothing"],
                            deads=probe.dead_key_candidates(results))

        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        self.addCleanup(os.unlink, path)

        loaded = kbmap.load_layout(path)
        self.assertEqual(loaded,
                         {char: steps for char, (steps, _) in layout.items()})
        self.assertEqual(loaded["ê"], ((26, 0), (18, 0)))

    def test_unresolved_rows_are_recorded_in_the_file(self):
        text = probe.render({}, missing=["k22L3 produced nothing"])
        self.assertIn("# unresolved: k22L3 produced nothing", text)

    def test_dead_keys_are_written_for_the_compose_pass_to_read(self):
        import tempfile
        text = probe.render({}, deads=[(26, 0, "^"), (26, SHIFT, "¨")])
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(text)
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(kbmap.load_dead_keys(path), [(26, 0), (26, SHIFT)])
        self.assertEqual(kbmap.load_layout(path), {})


class QuoteAliasTest(unittest.TestCase):
    """A straight quote has to be typable somehow.

    With Smart Punctuation on, no key produces one - iOS rewrites them as
    it stores them - so without an alias "c'est" pastes as "cest", which
    is a misspelling rather than a substitution.
    """

    def test_the_apostrophe_is_aliased_onto_its_key(self):
        layout = probe.add_quote_aliases({"’": (((51, SHIFT),), ["SMART"])})
        self.assertEqual(layout["'"][0], ((51, SHIFT),))

    def test_the_alias_says_what_will_actually_arrive(self):
        layout = probe.add_quote_aliases({"’": (((51, SHIFT),), [])})
        self.assertIn("arrives as ’", layout["'"][1])

    def test_a_real_straight_quote_is_left_alone(self):
        """Nothing to substitute for if the keyboard has the real thing."""
        layout = probe.add_quote_aliases({"'": (((40, 0),), []),
                                          "’": (((51, SHIFT),), ["SMART"])})
        self.assertEqual(layout["'"][0], ((40, 0),))

    def test_the_cheapest_variant_is_chosen(self):
        layout = probe.add_quote_aliases({
            "“": (((25, OPTION),), []),
            "”": (((25, OPTION | SHIFT),), [])})
        self.assertEqual(layout['"'][0], ((25, OPTION),))

    def test_nothing_is_invented_when_there_is_no_curly_one_either(self):
        layout = probe.add_quote_aliases({"a": (((30, 0),), [])})
        self.assertNotIn("'", layout)
        self.assertNotIn('"', layout)

    def test_the_double_quote_is_aliased_too(self):
        layout = probe.add_quote_aliases({"”": (((25, OPTION),), ["SMART"])})
        self.assertEqual(layout['"'][0], ((25, OPTION),))


class SmartPunctuationTest(unittest.TestCase):
    """Which quote entries depend on Settings > Keyboard > Smart Punctuation.

    It depends on which way the setting was turned when the capture was
    taken, and marking the wrong ones is worse than marking none: with the
    setting off, a curly quote is a key that really types one, and the
    conditional entries are the straight quotes, which would start arriving
    curly if it were turned back on.
    """

    def results(self, *chars):
        return [(((30 + n, 0),), char, []) for n, char in enumerate(chars)]

    def annotate(self, *chars):
        found = self.results(*chars)
        probe.annotate_quotes(found)
        return {char: notes for _, char, notes in found}

    def test_a_straight_quote_anywhere_means_the_setting_was_off(self):
        self.assertFalse(probe.smart_punctuation(self.results("a", "'", "\u2019")))

    def test_no_straight_quote_means_it_was_on(self):
        self.assertTrue(probe.smart_punctuation(self.results("a", "\u2019")))

    def test_with_it_on_the_curly_ones_are_marked(self):
        notes = self.annotate("a", "\u2019", "\u201c")
        self.assertEqual(notes["\u2019"], ["SMART"])
        self.assertEqual(notes["\u201c"], ["SMART"])

    def test_with_it_off_the_curly_ones_are_not(self):
        # They are keys that really type a curly quote; nothing about them
        # is conditional, and saying otherwise sends someone re-measuring
        # for no reason.
        notes = self.annotate("'", "\u2019", "\u201c")
        self.assertEqual(notes["\u2019"], [])
        self.assertEqual(notes["\u201c"], [])

    def test_with_it_off_the_straight_ones_are(self):
        notes = self.annotate("'", '"', "\u2019")
        self.assertEqual(notes["'"], ["STRAIGHT"])
        self.assertEqual(notes['"'], ["STRAIGHT"])

    def test_a_dead_key_keeps_its_note(self):
        found = [(((26, 0),), "\u2019", ["dead"])]
        probe.annotate_quotes(found)
        self.assertEqual(found[0][2], ["dead", "SMART"])

    def test_the_file_says_which_way_the_setting_was(self):
        on = probe.render({}, smart=True)
        off = probe.render({}, smart=False)
        self.assertIn("was ON when this was measured", on)
        self.assertIn("was OFF when this was measured", off)

    def test_an_unknown_setting_says_nothing_about_it(self):
        self.assertNotIn("Smart Punctuation", probe.render({}))


class ShippedLayoutTest(unittest.TestCase):
    """The layout in layouts/ must be what the tool makes of its captures.

    It is checked in as a worked example, so it has to stay one: a layout
    that no longer matches its own captures teaches the wrong thing.
    """

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.directory = os.path.join(self.root, "layouts")
        if not os.path.isdir(self.directory):
            self.skipTest("no layouts checked in")

    def read(self, name):
        with open(os.path.join(self.directory, name), encoding="utf-8") as f:
            return f.read()

    def test_the_conf_matches_its_captures(self):
        plain, _ = probe.parse_capture(
            self.read("iphone-fr-ca.layout.capture"))
        self.assertTrue(plain, "the layout capture did not parse")
        candidates = probe.dead_key_candidates(plain)
        composed, _ = probe.parse_compositions(
            self.read("iphone-fr-ca.accents.capture"), candidates)
        self.assertTrue(composed, "the accent capture did not parse")

        expected = {char: steps for char, (steps, _) in probe.add_quote_aliases(
            probe.to_layout(plain + composed)).items()}
        actual = kbmap.load_layout(
            os.path.join(self.directory, "iphone-fr-ca.conf"))
        self.assertEqual(actual, expected)

    def rebuild(self):
        """Regenerate the layout file from its captures, in process."""
        plain, problems = probe.parse_capture(
            self.read("iphone-fr-ca.layout.capture"))
        candidates = probe.dead_key_candidates(plain)
        declined = []
        composed, trouble = probe.parse_compositions(
            self.read("iphone-fr-ca.accents.capture"), candidates,
            declined=declined)
        layout = probe.add_quote_aliases(probe.to_layout(plain + composed))
        text = probe.render(layout, missing=problems + trouble,
                            deads=probe.dead_key_candidates(plain + composed),
                            declined=declined,
                            smart=probe.smart_punctuation(plain + composed))
        return text, self.refusals(text)

    @staticmethod
    def refusals(text):
        """{accent name: [base, ...]} as the file states it."""
        found, listing = {}, False
        for line in text.splitlines():
            if line.startswith("# Probed and refused"):
                listing = True
            elif listing and line.startswith("#   ") and " with " in line:
                accent, bases = line[4:].split(" with ", 1)
                found[accent] = bases.split()
            elif listing and not line.startswith("#"):
                break
        return found

    def test_a_refusal_is_recorded_rather_than_left_as_a_gap(self):
        """Absence has two meanings and they are not the same one.

        A character missing because nothing probed for it, and one missing
        because the phone was asked and said no, look identical in a file
        that lists only what worked.  The second is an answer.
        """
        text, _ = self.rebuild()
        self.assertIn("Probed and refused", text)

    def test_the_acute_refused_y_and_the_file_says_so(self):
        # It came back as two characters, the bare accent then the base,
        # so no two keystrokes on this phone produce y with an acute.
        _, refused = self.rebuild()
        self.assertIn("y", refused["ACUTE ACCENT"])

    def test_a_pair_that_did_compose_is_not_listed_as_refused(self):
        # The diaeresis does compose with y, which is why the file has one.
        text, refused = self.rebuild()
        self.assertNotIn("y", refused["DIAERESIS"])
        self.assertIn("U+00FF", text)          # and there it is, as an entry

    def test_the_two_cases_of_a_base_are_one_answer(self):
        # Acute refused both y and Y; saying so twice says nothing extra.
        _, refused = self.rebuild()
        for accent, bases in refused.items():
            self.assertEqual(sorted(bases), sorted(set(bases)),
                             "%s lists a base twice" % accent)

    def test_the_bases_are_named_by_what_they_type(self):
        _, refused = self.rebuild()
        self.assertTrue(refused)
        for accent, bases in refused.items():
            for base in bases:
                self.assertIn(base, ("space", "a", "e", "i", "o", "u",
                                     "y", "n", "c"), accent)

    def test_the_checked_in_file_is_what_the_generator_produces(self):
        text, _ = self.rebuild()
        shipped = self.read("iphone-fr-ca.conf")
        # The source line names the capture files, which rebuild() does not.
        strip = lambda t: [l for l in t.splitlines()
                           if not l.startswith("# Generated")]
        self.assertEqual(strip(shipped), strip(text))

    def test_the_captures_carry_nothing_personal(self):
        """They came out of a mail message; only the probe should remain."""
        for name in ("iphone-fr-ca.layout.capture",
                     "iphone-fr-ca.accents.capture"):
            text = self.read(name).lower()
            for word in ("from:", "to:", "message-id", "x-mailer", "@fluxnic"):
                self.assertNotIn(word, text, "%s in %s" % (word, name))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
