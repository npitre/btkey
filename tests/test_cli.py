# SPDX-License-Identifier: GPL-2.0-only
"""Talking to an already-running btkey.

Driving this by echoing into a FIFO was a debugging affordance that had
turned into the interface, which meant the feature was undiscoverable and
its vocabulary was the implementation's rather than the user's.  A second
btkey now carries the message.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import cli, config, probe


class SendCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "control")

    def listen(self):
        """Stand in for the running btkey, which holds the FIFO open."""
        os.mkfifo(self.path, 0o600)
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        return fd

    def test_the_command_arrives(self):
        fd = self.listen()
        self.assertEqual(cli.send_command(self.path, "learn_layout"), 0)
        self.assertEqual(os.read(fd, 64), b"learn-layout\n")

    def test_an_argument_rides_along(self):
        fd = self.listen()
        cli.send_command(self.path, "learn_accents", "26:0 26:2")
        self.assertEqual(os.read(fd, 64), b"learn-accents 26:0 26:2\n")

    def test_each_action_has_its_own_word(self):
        fd = self.listen()
        for name in ("learn_layout", "learn_accents", "cancel"):
            cli.send_command(self.path, name)
        self.assertEqual(os.read(fd, 128),
                         b"learn-layout\nlearn-accents\ncancel\n")

    def test_no_reader_means_btkey_is_not_running(self):
        """Opening a FIFO for writing with no reader gives ENXIO, which is
        a reliable way to notice - better than writing into the void."""
        os.mkfifo(self.path, 0o600)
        self.assertEqual(cli.send_command(self.path, "cancel"), 1)

    def test_an_ordinary_file_in_the_way_is_refused(self):
        """It would accept the write and report success, read by nobody."""
        with open(self.path, "w") as handle:
            handle.write("")
        self.assertEqual(cli.send_command(self.path, "cancel"), 1)

    def test_a_missing_fifo_is_reported_not_created(self):
        self.assertEqual(cli.send_command(self.path, "cancel"), 1)
        self.assertFalse(os.path.exists(self.path))

    def test_the_commands_match_what_the_session_understands(self):
        """The two halves of the protocol have to agree.

        Splitting the trigger out of the running btkey made it possible for
        one side to be renamed without the other, which nothing else would
        catch until a command silently did nothing.
        """
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "btkey", "session.py"), encoding="utf-8").read()
        for command, _ in cli.COMMANDS.values():
            # Loose on how it is matched - one command carries an argument,
            # so it is dispatched with startswith - but strict on the word.
            self.assertTrue('"%s"' % command in source,
                            "session.py never mentions the command %r"
                            % command)


class AccentCandidateTest(unittest.TestCase):
    """Why the two passes cannot be one.

    Which keys are dead is a property of the *results*, which exist only on
    the phone: btkey knows what it typed, but not that keycode 26 gave a
    dead circumflex rather than a literal one.  Working it out here, in the
    client, from the first capture is what saves restarting the running
    btkey between the two passes.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "capture.txt")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return self.path

    def refusal(self, path=None):
        """What it said when it would not use the file.

        Returning None is not the whole of the behaviour: a file that is
        the wrong kind and a file of the right kind with nothing in it are
        different problems, and being told the wrong one sends you looking
        in the wrong place.
        """
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            self.assertIsNone(cli.accent_candidates(path or self.path))
            return sys.stderr.getvalue()
        finally:
            sys.stderr = saved

    def capture(self, rows):
        """A capture with the given row content, bounded by sentinels."""
        block = ["1" * 8] + rows + ["1" * 8]
        return self.write("\n".join(block) + "\n")

    def rows(self, **dead):
        """Sixteen rows; the named positions come back as dead keys."""
        out = []
        for level in range(4):
            for row in probe.ROWS:
                fields = []
                for keycode in row:
                    if dead.get("k%d_%d" % (keycode, level)):
                        fields.append("^")          # swallowed its space
                    else:
                        fields.append("a ")
                out.append("11".join(fields) + "11")
        return out

    def test_dead_keys_in_the_capture_become_candidates(self):
        self.capture(self.rows(k26_0=True))
        self.assertEqual(cli.accent_candidates(self.path), [(26, 0, "^")])

    def test_a_capture_with_no_dead_keys_says_so(self):
        self.capture(self.rows())
        self.assertIsNone(cli.accent_candidates(self.path))

    def test_an_unreadable_capture_says_so(self):
        self.assertIsNone(cli.accent_candidates(
            os.path.join(self.directory, "absent")))

    def test_something_that_is_neither_says_so(self):
        self.write("Subject: keys\n\nnothing useful here\n")
        self.assertIsNone(cli.accent_candidates(self.path))

    # -- either file will do ---------------------------------------------

    def test_a_built_layout_file_works_as_well(self):
        # Which of the two is at hand depends on how far through the
        # procedure you are, and it is not worth having to remember.
        self.write("dead\t26\t0x00\ndead\t39\t0x42\nU+00E9\t18\t0\n")
        self.assertEqual(cli.accent_candidates(self.path),
                         [(26, 0x00, None), (39, 0x42, None)])

    def test_the_two_sources_agree(self):
        capture = self.capture(self.rows(k26_0=True))
        from_capture = cli.accent_candidates(capture)
        built = os.path.join(self.directory, "built.conf")
        with open(built, "w", encoding="utf-8") as handle:
            for keycode, modifiers, _ in from_capture:
                handle.write("dead\t%d\t0x%02x\n" % (keycode, modifiers))
        from_file = cli.accent_candidates(built)
        self.assertEqual([(k, m) for k, m, _ in from_capture],
                         [(k, m) for k, m, _ in from_file])

    def test_a_layout_file_with_no_dead_lines_says_which_files_would_do(self):
        self.write("U+00E9\t18\t0\n")
        said = self.refusal()
        self.assertIn("--learn-layout", said)
        self.assertIn("--build-layout", said)

    def test_a_malformed_dead_line_is_reported_as_such(self):
        # Not as "no accent keys found", which would have you hunting for
        # a missing probe rather than a broken line.
        self.write("dead\t26\n")
        said = self.refusal()
        self.assertIn("keycode and modifiers", said)

    def test_the_accents_capture_is_not_a_source(self):
        # It is the second pass's *result*; it says nothing about which
        # keys are dead, and passing it is a plausible mistake.
        self.write("11111111\n^ 11a 11\n11111111\n")
        self.assertIsNone(cli.accent_candidates(self.path))


class AudioOptionTest(unittest.TestCase):
    """One switch, eight spellings, one of them in the help.

    They exist because there is no obvious winner: --audio=off reads well
    in a file-like way, --no-audio is what anyone types from habit, and
    --without-audio is what it was called yesterday.  Documenting all of
    them would be worse than documenting one.
    """

    def setUp(self):
        self.parser = cli.build_parser()

    def value(self, *argv):
        return self.parser.parse_args(list(argv)).audio

    def test_off_unless_asked(self):
        self.assertFalse(self.value())

    def test_every_way_of_saying_on(self):
        for argv in (["--audio"], ["--audio=yes"], ["--audio=on"],
                     ["--audio=true"], ["--audio", "1"], ["--with-audio"]):
            self.assertTrue(self.value(*argv), argv)

    def test_every_way_of_saying_off(self):
        for argv in (["--audio=no"], ["--audio=off"], ["--audio=false"],
                     ["--audio", "0"], ["--no-audio"], ["--without-audio"]):
            self.assertFalse(self.value(*argv), argv)

    def test_the_last_one_wins(self):
        # Which is what lets the command line answer a file.
        self.assertFalse(self.value("--with-audio", "--audio=off"))
        self.assertTrue(self.value("--no-audio", "--audio=on"))

    def test_a_word_that_is_neither_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--audio=maybe"])

    def test_only_one_spelling_is_documented(self):
        text = " ".join(self.parser.format_help().split())
        self.assertIn("--audio [on/off]", text)
        for hidden in ("--with-audio", "--without-audio", "--no-audio"):
            self.assertNotIn(hidden, text)

    def test_the_words_come_from_the_configuration_reader(self):
        # So that `audio = on` in a file and --audio=on cannot drift.
        for word in config.TRUE_WORDS:
            self.assertTrue(cli.switch(word), word)
        for word in config.FALSE_WORDS:
            self.assertFalse(cli.switch(word), word)


class CommandDispatchTest(unittest.TestCase):
    """Each command option reaches the FIFO.

    The option, the name on the wire and the branch that connects them are
    three separate things, and a command with two of the three does
    nothing at all while looking finished.
    """

    def setUp(self):
        self.sent = []
        saved = cli.send_command
        cli.send_command = lambda path, name, argument="": (
            self.sent.append((name, argument)) or 0)
        self.addCleanup(setattr, cli, "send_command", saved)

    def test_each_option_sends_its_command(self):
        for option, name in (("--learn-layout", "learn_layout"),
                             ("--cancel", "cancel"),
                             ("--quit", "quit")):
            self.sent.clear()
            self.assertEqual(cli.main([option, "--no-config"]), 0, option)
            self.assertEqual([n for n, _ in self.sent], [name], option)

    def test_every_command_option_is_dispatched(self):
        """No option may be declared and left unwired.

        --quit was written, named and documented before anything sent it.
        """
        parser = cli.build_parser()
        for name in cli.COMMANDS:
            if name == "learn_accents":
                continue          # takes a file, and has its own path
            option = "--" + name.replace("_", "-")
            self.sent.clear()
            cli.main([option, "--no-config"])
            self.assertEqual([n for n, _ in self.sent], [name],
                             "%s reached nothing" % option)


class SecondInstanceTest(unittest.TestCase):
    """main() refuses to start beside another btkey.

    Before the console is opened, before the guardian is forked, before
    bluetoothd is touched: a second instance that gets as far as starting
    services puts bluetooth.service back underneath the first one.
    """

    def setUp(self):
        self.errors = io.StringIO()
        saved_err, sys.stderr = sys.stderr, self.errors
        self.addCleanup(setattr, sys, "stderr", saved_err)
        # Pretend to be root; the check for it comes first and is not what
        # is being tested here.
        saved_uid = cli.os.geteuid
        cli.os.geteuid = lambda: 0
        self.addCleanup(setattr, cli.os, "geteuid", saved_uid)
        self.saved_hold = cli.single.hold
        self.addCleanup(setattr, cli.single, "hold", self.saved_hold)

    def test_it_stops_when_another_holds_the_lock(self):
        cli.single.hold = lambda who="", **kw: (None, "pid 999, started by nico")
        self.assertEqual(cli.main(["--no-config"]), 1)
        said = self.errors.getvalue()
        self.assertIn("already running", said)
        self.assertIn("pid 999, started by nico", said)

    def test_it_stops_before_opening_a_console(self):
        # The console is the next thing tried, and its failure is what
        # would be reported instead if the lock were checked after it.
        cli.single.hold = lambda who="", **kw: (None, "")
        cli.main(["--no-config"])
        self.assertNotIn("virtual terminal", self.errors.getvalue())

    def test_holding_the_lock_lets_it_carry_on(self):
        taken = []
        cli.single.hold = lambda who="", **kw: (taken.append(who) or 7, None)
        cli.main(["--no-config"])
        self.assertEqual(len(taken), 1)
        # Far enough to try the console, which is not there in a test.
        self.assertIn("virtual terminal", self.errors.getvalue())

    def test_it_says_who_is_asking(self):
        taken = []
        cli.single.hold = lambda who="", **kw: (taken.append(who) or 7, None)
        cli.main(["--no-config"])
        self.assertTrue(taken[0], "the lock was taken without a name")


class TopRowOptionTest(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_the_host_is_asked_by_default(self):
        self.assertEqual(self.parser.parse_args([]).top_row, "auto")

    def test_both_answers_can_be_given_outright(self):
        for choice in ("function", "media"):
            self.assertEqual(
                self.parser.parse_args(["--top-row", choice]).top_row, choice)

    def test_anything_else_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--top-row", "apple"])


class LogFileOptionTest(unittest.TestCase):
    """--log-file with a value, without one, and absent.

    Three distinct answers from one option: a path, the usual path, and no
    file at all.  The middle one exists because typing the usual path to
    ask for the usual path is a silly thing to have to do.
    """

    def setUp(self):
        self.parser = cli.build_parser()

    def value(self, *argv):
        return self.parser.parse_args(list(argv)).log_file

    def test_a_path_is_taken_as_given(self):
        self.assertEqual(self.value("--log-file", "/tmp/x"), "/tmp/x")
        self.assertEqual(self.value("--log-file=/tmp/y"), "/tmp/y")

    def test_every_way_of_asking_for_the_usual_place(self):
        for argv in (["--log-file"], ["--with-log-file"]):
            self.assertEqual(self.value(*argv), cli.DEFAULT_LOG_FILE, argv)

    def test_every_way_of_turning_it_off(self):
        for argv in (["--log-file="], ["--log-file", ""], ["--no-log-file"],
                     ["--without-log-file"]):
            self.assertEqual(self.value(*argv), "", argv)

    def test_the_last_one_wins(self):
        self.assertEqual(self.value("--with-log-file", "--no-log-file"), "")
        self.assertEqual(self.value("--no-log-file", "--log-file=/tmp/x"),
                         "/tmp/x")

    def test_the_negative_spelling_does_not_become_a_file_switch(self):
        # --no-log-file is the negative of a value option, not a switch of
        # its own: `log-file` in a file is a path, never a yes or a no.
        self.assertNotIn("log-file", cli.flag_options(self.parser))
        self.assertIn("log-file", cli.optional_values(self.parser))

    def test_absent_it_follows_where_btkey_was_started_from(self):
        # A checkout writes one; an installation does not.  These tests run
        # from a checkout.
        self.assertEqual(self.value(), cli.DEFAULT_LOG_FILE)

    def test_the_help_says_all_three_under_one_entry(self):
        text = " ".join(self.parser.format_help().split())
        self.assertIn("--log-file [PATH]", text)
        self.assertIn("On its own it means %s" % cli.DEFAULT_LOG_FILE, text)
        self.assertIn("--no-log-file, or an empty PATH, turns it off", text)
        for hidden in ("--with-log-file", "--without-log-file"):
            self.assertNotIn(hidden, text)


class ParserTest(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_the_learning_options_exist(self):
        options = self.parse("--learn-layout")
        self.assertTrue(options.learn_layout)
        self.assertIsNone(options.learn_accents)

    def test_learning_accents_takes_the_first_capture(self):
        options = self.parse("--learn-accents", "layout.txt")
        self.assertEqual(options.learn_accents, "layout.txt")

    def test_build_layout_takes_several_captures(self):
        options = self.parse("--build-layout", "a.txt", "b.txt")
        self.assertEqual(options.build_layout, ["a.txt", "b.txt"])

    def test_defaults_do_nothing_special(self):
        options = self.parse()
        self.assertFalse(options.learn_layout or options.cancel)
        self.assertIsNone(options.learn_accents)
        self.assertIsNone(options.build_layout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
