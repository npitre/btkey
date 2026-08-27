# SPDX-License-Identifier: GPL-2.0-only
"""The configuration file.

It is deliberately thin: every line names an option that could have been
typed, and the file is turned into arguments placed before the real ones.
So there is exactly one definition of what an option is called, what it
takes and what it means - the parser - and a config file cannot drift out
of step with it.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import cli, config


class ToArgumentsTest(unittest.TestCase):
    # {positive name: (option, emit when true)}, as the parser reports it.
    FLAGS = {"audio": ("--no-audio", False),
             "debug": ("--debug", True),
             "reconnect": ("--no-reconnect", False)}

    def convert(self, text):
        return config.to_arguments(text, self.FLAGS)

    def test_a_value_becomes_an_option_and_its_value(self):
        args, problems = self.convert("phone-layout = /tmp/x.conf\n")
        self.assertEqual(args, ["--phone-layout", "/tmp/x.conf"])
        self.assertEqual(problems, [])

    def test_spacing_around_the_equals_is_optional(self):
        args, _ = self.convert("name=xanadu\n")
        self.assertEqual(args, ["--name", "xanadu"])

    def test_a_switch_is_written_positively(self):
        """`audio = no`, not `no-audio`: nobody should read a double
        negative to find out whether sound is on."""
        args, problems = self.convert("audio = no\n")
        self.assertEqual(args, ["--no-audio"])
        self.assertEqual(problems, [])

    def test_a_switch_left_at_its_default_emits_nothing(self):
        args, problems = self.convert("audio = yes\n")
        self.assertEqual(args, [])
        self.assertEqual(problems, [])

    def test_the_negative_spelling_is_refused_with_the_right_one(self):
        args, problems = self.convert("no-audio = no\n")
        self.assertEqual(args, [])
        self.assertIn("write audio = no", problems[0])

    def test_yes_can_be_spelled_several_ways(self):
        for word in ("", "= yes", "= true", "= on", "= 1"):
            args, _ = self.convert("debug %s\n" % word)
            self.assertEqual(args, ["--debug"], word)

    def test_no_can_be_spelled_several_ways(self):
        for word in ("no", "false", "off", "0"):
            args, problems = self.convert("debug = %s\n" % word)
            self.assertEqual(args, [], word)
            self.assertEqual(problems, [])

    def test_a_switch_given_something_else_is_a_problem(self):
        args, problems = self.convert("debug = loudly\n")
        self.assertEqual(args, [])
        self.assertIn("wants yes or no", problems[0])

    def test_an_option_with_no_value_is_a_problem(self):
        args, problems = self.convert("phone-layout =\n")
        self.assertEqual(args, [])
        self.assertIn("needs a value", problems[0])

    def test_comments_and_blank_lines_are_ignored(self):
        args, problems = self.convert(
            "# a comment\n\n  \nname = x   # trailing\n")
        self.assertEqual(args, ["--name", "x"])
        self.assertEqual(problems, [])

    def test_several_values_become_several_arguments(self):
        args, _ = self.convert("device = /dev/input/event3 /dev/input/event4\n")
        self.assertEqual(args, ["--device", "/dev/input/event3",
                                "/dev/input/event4"])

    def test_problems_are_reported_with_a_line_number(self):
        _, problems = self.convert("name = x\nbogus =\n")
        self.assertTrue(problems[0].startswith("2:"))


class ExpandTest(unittest.TestCase):
    """A leading ~ in a config file, which no shell ever sees.

    Left to os.path.expanduser it would answer /root, since that is who
    btkey runs as - so every ~ in the documentation would be wrong in a
    way that only shows up under sudo.
    """

    def setUp(self):
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "1000", "1000"
        self.addCleanup(os.environ.pop, "SUDO_UID", None)
        self.addCleanup(os.environ.pop, "SUDO_GID", None)

    def test_it_expands_against_the_invoking_user(self):
        expanded = config.expand("~/.config/btkey/layout.conf")
        self.assertTrue(expanded.startswith("/"))
        self.assertNotIn("/root/", expanded)
        self.assertTrue(expanded.endswith("/.config/btkey/layout.conf"))

    def test_an_absolute_path_is_untouched(self):
        self.assertEqual(config.expand("/etc/btkey/x.conf"),
                         "/etc/btkey/x.conf")

    def test_a_bare_tilde_is_not_a_path(self):
        self.assertEqual(config.expand("~"), "~")

    def test_a_value_that_merely_contains_one_is_untouched(self):
        self.assertEqual(config.expand("a~/b"), "a~/b")

    def test_values_from_a_file_are_expanded(self):
        args, _ = config.to_arguments("phone-layout = ~/x.conf\n", {})
        self.assertTrue(args[1].startswith("/"))
        self.assertTrue(args[1].endswith("/x.conf"))


class FlagDetectionTest(unittest.TestCase):
    def test_flags_come_from_the_parser(self):
        flags = cli.flag_options(cli.build_parser())
        self.assertIn("debug", flags)
        self.assertNotIn("phone-layout", flags)
        self.assertNotIn("name", flags)

    def test_an_option_that_takes_a_value_owns_its_name(self):
        # --audio takes on or off, so `audio` in a file is that value and
        # none of the switches spelling the same thing may claim the key.
        parser = cli.build_parser()
        flags = cli.flag_options(parser)
        for name in ("audio", "with-audio", "without-audio", "no-audio"):
            self.assertNotIn(name, flags)
        self.assertIn("audio", cli.switch_values(parser))

    def test_a_path_option_is_not_a_switch(self):
        parser = cli.build_parser()
        self.assertIn("log-file", cli.optional_values(parser))
        self.assertNotIn("log-file", cli.switch_values(parser))
        self.assertNotIn("log-file", cli.flag_options(parser))

    def test_a_negative_only_switch_is_offered_positively_too(self):
        flags = cli.flag_options(cli.build_parser())
        self.assertEqual(flags["reconnect"], ("--no-reconnect", False))
        self.assertNotIn("no-reconnect", flags)

    def test_short_options_do_not_leak_in(self):
        self.assertNotIn("h", cli.flag_options(cli.build_parser()))


class PrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "btkey.conf")
        self.parser = cli.build_parser()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def resolve(self, *argv):
        argv = ["--config", self.path] + list(argv)
        return self.parser.parse_args(
            cli.arguments_with_config(self.parser, argv))

    def test_the_file_supplies_a_default(self):
        self.write("name = fromfile\n")
        self.assertEqual(self.resolve().name, "fromfile")

    def test_the_command_line_wins(self):
        self.write("name = fromfile\n")
        self.assertEqual(self.resolve("--name", "typed").name, "typed")

    def test_a_switch_in_the_file_takes_effect(self):
        self.write("audio = yes\n")
        self.assertTrue(self.resolve().audio)

    def test_the_command_line_can_turn_a_file_switch_back_off(self):
        # Which is the whole reason a switch has both spellings: a file
        # saying yes has to be answerable without editing the file.
        self.write("audio = yes\n")
        self.assertFalse(self.resolve("--without-audio").audio)

    def test_no_audio_is_the_same_switch_under_an_older_name(self):
        self.write("audio = yes\n")
        self.assertFalse(self.resolve("--no-audio").audio)

    def test_no_log_file_answers_a_file_that_asked_for_one(self):
        self.write("log-file = /tmp/fromfile\n")
        self.assertEqual(self.resolve("--no-log-file").log_file, "")

    def test_a_switch_left_out_of_the_file_keeps_its_default(self):
        self.write("name = fromfile\n")
        self.assertFalse(self.resolve().audio)

    def test_an_empty_value_turns_off_an_option_that_allows_one(self):
        # `log-file =` is the file turned off, the same as `--log-file=`.
        self.write("log-file =\n")
        self.assertEqual(self.resolve().log_file, "")

    def test_an_empty_value_is_still_wrong_for_anything_else(self):
        self.write("name =\n")
        _, problems = config.to_arguments("name =\n",
                                          cli.flag_options(self.parser),
                                          cli.optional_values(self.parser))
        self.assertTrue(any("needs a value" in p for p in problems), problems)

    def test_a_path_in_the_file_is_used(self):
        self.write("log-file = /tmp/somewhere\n")
        self.assertEqual(self.resolve().log_file, "/tmp/somewhere")

    def test_the_optional_ones_are_read_from_the_parser(self):
        # Not listed, so an option gaining an optional value cannot be
        # rejected in a file afterwards.
        self.assertIn("log-file", cli.optional_values(self.parser))
        self.assertNotIn("name", cli.optional_values(self.parser))

    def test_a_switch_takes_any_of_its_spellings(self):
        for word, wanted in (("yes", True), ("on", True), ("true", True),
                             ("no", False), ("off", False), ("false", False)):
            self.write("audio = %s\n" % word)
            self.assertIs(self.resolve().audio, wanted, word)

    def test_a_bare_switch_key_means_on(self):
        self.write("audio =\n")
        self.assertTrue(self.resolve().audio)

    def test_a_word_that_is_neither_is_refused_with_its_line(self):
        # argparse can say which option; only this can say which line.
        _, problems = config.to_arguments(
            "name = x\naudio = maybe\n", cli.flag_options(self.parser),
            cli.optional_values(self.parser), cli.switch_values(self.parser))
        self.assertEqual(problems, ["2: audio wants on or off, not 'maybe'"])

    def test_no_switch_is_advised_towards_the_word_no(self):
        _, problems = config.to_arguments(
            "no-audio = yes\n", cli.flag_options(self.parser),
            cli.optional_values(self.parser), cli.switch_values(self.parser))
        self.assertEqual(problems, ["1: write audio = no rather than no-audio"])

    def test_no_path_is_advised_towards_an_empty_value(self):
        # Not towards `log-file = no`, which would name a file "no".
        _, problems = config.to_arguments(
            "no-log-file = yes\n", cli.flag_options(self.parser),
            cli.optional_values(self.parser), cli.switch_values(self.parser))
        self.assertEqual(
            problems, ["1: write log-file with no value rather than no-log-file"])

    def test_no_config_ignores_the_file(self):
        self.write("name = fromfile\n")
        options = self.parser.parse_args(cli.arguments_with_config(
            self.parser, ["--config", self.path, "--no-config"]))
        self.assertEqual(options.name, "btkey")

    def test_a_missing_file_is_not_fatal(self):
        argv = ["--config", os.path.join(self.directory, "absent")]
        self.assertEqual(cli.arguments_with_config(self.parser, argv), argv)

    def test_appending_options_accumulate(self):
        """--device appends, so the file and the command line combine."""
        self.write("device = /dev/input/event3\n")
        options = self.resolve("--device", "/dev/input/event9")
        self.assertEqual(options.device,
                         ["/dev/input/event3", "/dev/input/event9"])


class SearchTest(unittest.TestCase):
    def test_the_user_comes_before_etc(self):
        paths = config.candidates()
        self.assertTrue(paths[-1].startswith("/etc/"))
        self.assertGreater(len(paths), 1)

    def test_it_is_the_invoking_user_not_root(self):
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "1000", "1000"
        self.addCleanup(os.environ.pop, "SUDO_UID", None)
        self.addCleanup(os.environ.pop, "SUDO_GID", None)
        self.assertNotIn("/root/", config.candidates()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
