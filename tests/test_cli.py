# SPDX-License-Identifier: GPL-2.0-only
"""Talking to an already-running btkey.

Driving this by echoing into a FIFO was a debugging affordance that had
turned into the interface, which meant the feature was undiscoverable and
its vocabulary was the implementation's rather than the user's.  A second
btkey now carries the message.
"""

import errno
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import cli, config, probe

from test_keys import source_of


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

    def said(self, name):
        """What the caller is told, with the command actually sent."""
        if getattr(self, "fd", None) is None:
            self.fd = self.listen()      # one FIFO for however many asks
        fd = self.fd
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            cli.send_command(self.path, name)
            return sys.stderr.getvalue()
        finally:
            sys.stderr = saved
            os.read(fd, 128)

    def test_stopping_promises_no_bell(self):
        """There is not one, and nothing to wait for either.

        quit() announces and ends the loop; the bell belongs to the
        probes, which type for minutes and ring at the end.
        """
        said = self.said("quit")
        self.assertIn("stopping", said)
        self.assertNotIn("bell", said)

    def test_cancelling_promises_no_bell(self):
        # Done by the time the message is printed.
        self.assertNotIn("bell", self.said("cancel"))

    def test_a_probe_says_to_wait_for_the_bell(self):
        said = self.said("learn_layout")
        self.assertIn("layout probe", said)
        self.assertIn("bell", said)

    def test_every_command_says_what_it_asked_for(self):
        for name, (_, description, _) in cli.COMMANDS.items():
            self.assertIn(description, self.said(name))

    def test_only_what_rings_says_it_rings(self):
        """The claim has to match the code that would make the sound.

        A command that says "bell" without reaching finish_sweep is
        exactly the message that sent someone listening for a sound that
        was never coming.
        """
        self.assertIn("self.display.bell()", source_of("sweep.py"))
        ringing = {name for name, (_, _, waits) in cli.COMMANDS.items()
                   if waits}
        self.assertEqual(ringing, {"learn_layout", "learn_accents"})

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
        source = source_of("session.py")
        for command, _, _ in cli.COMMANDS.values():
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


class ListDevicesTest(unittest.TestCase):
    """--list-devices, which has to answer "can btkey have it".

    Two questions it used to get wrong: it reported whether a device
    looked like a keyboard rather than whether btkey could take it, and it
    decided which devices btkey wants by repeating part of discover()
    instead of asking it - so a keyboard's media-key interface, which
    btkey does take, was listed as one it would leave alone.
    """

    class Device:
        def __init__(self, path, name="keyboard", error=None):
            self.path, self.name, self.error = path, name, error
            self.grab_error, self.grabbed = None, False
            self.released = []

        def grab(self):
            self.grab_error = self.error
            self.grabbed = self.error is None
            return self.error is None

        def ungrab(self):
            self.released.append("ungrab")
            self.grabbed = False

        def close(self):
            self.released.append("close")

    def setUp(self):
        self.out = io.StringIO()
        saved, sys.stdout = sys.stdout, self.out
        self.addCleanup(setattr, sys, "stdout", saved)

    def listing(self, chosen, others=()):
        """chosen: what discover() picks.  others: what else is there."""
        rest = {path: self.Device(path, name) for path, name in others}
        saved_discover = cli.evdev.discover
        saved_device = cli.evdev.InputDevice
        saved_glob = cli.glob.glob
        cli.evdev.discover = lambda extra=(), known=(): (list(chosen), [])
        cli.evdev.InputDevice = lambda path: rest[path]
        cli.glob.glob = lambda pattern: sorted(
            [d.path for d in chosen] + list(rest))
        self.addCleanup(setattr, cli.evdev, "discover", saved_discover)
        self.addCleanup(setattr, cli.evdev, "InputDevice", saved_device)
        self.addCleanup(setattr, cli.glob, "glob", saved_glob)
        cli.list_devices([])
        return self.out.getvalue()

    def test_a_device_btkey_wants_and_can_have(self):
        said = self.listing([self.Device("/dev/input/event0")])
        self.assertIn("available", said)

    def test_a_device_btkey_wants_and_cannot_have(self):
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.EBUSY)])
        self.assertIn("in use", said)
        self.assertNotIn("available", said)

    def test_the_program_holding_it_is_named(self):
        """Which program is the whole of what anyone wants to know.

        "btkey" means the one already running has it and all is well;
        "brltty" means go and look at its configuration.  Bare "held" is
        what is left when the holder cannot be named.
        """
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): {p: ["brltty"]
                                                     for p in paths}
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.EBUSY)])
        self.assertIn("used by brltty", said)

    def test_a_process_that_holds_a_free_device_is_not_blamed(self):
        """systemd-logind has every input device open, and grabs none.

        Naming the first process /proc happens to list said "used by
        systemd-logind" for a keyboard BRLTTY had.  The devices that did
        come settle it: a process holding one of those open is
        demonstrably not what stops a grab.
        """
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): {
            "/dev/input/event1": ["systemd-logind", "brltty"],
            "/dev/input/event4": ["systemd-logind"],
        }
        said = self.listing([self.Device("/dev/input/event1",
                                         error=errno.EBUSY),
                             self.Device("/dev/input/event4")])
        self.assertIn("used by brltty", said)
        self.assertNotIn("systemd-logind", said)

    def test_with_nothing_free_to_compare_against_all_are_named(self):
        """Which is what a running btkey looks like: it has them all.

        Nothing proves any of them innocent then, so the honest answer
        names them and lets the reader pick out the one they know.
        """
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): {
            "/dev/input/event1": ["systemd-logind", "btkey"]}
        said = self.listing([self.Device("/dev/input/event1",
                                         error=errno.EBUSY)])
        self.assertIn("used by systemd-logind, btkey", said)

    def test_nothing_is_asked_of_proc_when_everything_came(self):
        # The walk is not free, and there is nothing to attribute.
        looked = []
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): looked.append(1) or {}
        self.listing([self.Device("/dev/input/event1")])
        self.assertEqual(looked, [])

    def test_the_running_btkey_is_named_as_such(self):
        # The answer somebody most often wants: it is already working.
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): {p: ["btkey"]
                                                     for p in paths}
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.EBUSY)])
        self.assertIn("used by btkey", said)

    def test_a_holder_that_cannot_be_named_is_still_held(self):
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): {p: [] for p in paths}
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.EBUSY)])
        self.assertIn("in use", said)
        self.assertNotIn("used by", said)

    def test_a_refusal_that_is_not_busy_is_not_blamed_on_anyone(self):
        # ENODEV is the device having gone, not somebody holding it.
        looked = []
        self.addCleanup(setattr, cli.evdev, "openers", cli.evdev.openers)
        cli.evdev.openers = lambda paths, ignore=(): looked.append(paths) or {p: [] for p in paths}
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.ENODEV)])
        self.assertIn("no such device", said)
        self.assertEqual(looked, [])

    def test_a_device_btkey_does_not_want(self):
        said = self.listing([], others=[("/dev/input/event0", "Power Button")])
        self.assertIn("ignored", said)
        self.assertNotIn("available", said)
        self.assertNotIn("in use", said)

    def test_whatever_discover_picks_is_what_is_listed(self):
        """Including a companion, which the listing cannot work out itself.

        A keyboard's media-key interface does not look like a keyboard;
        only discover() knows it belongs to one that is.
        """
        media = self.Device("/dev/input/event6", "USB keyboard media keys")
        said = self.listing([media], others=[("/dev/input/event0", "Power")])
        self.assertIn("available        USB keyboard media keys", said)
        self.assertIn("event6", said)

    def test_another_failure_is_named_for_what_it_was(self):
        said = self.listing([self.Device("/dev/input/event0",
                                         error=errno.ENODEV)])
        self.assertIn("no such device", said)

    def test_a_device_it_took_is_given_straight_back(self):
        # Holding it would take the keyboard from the console for as long
        # as the listing ran.
        device = self.Device("/dev/input/event0")
        self.listing([device])
        self.assertEqual(device.released, ["ungrab", "close"])
        self.assertFalse(device.grabbed)

    def test_they_are_listed_in_number_order(self):
        # Sorting by name puts event10 between event1 and event2.
        said = self.listing([self.Device("/dev/input/event%d" % n)
                             for n in (1, 2, 10)])
        order = [line.split()[0] for line in said.strip().splitlines()[1:]]
        self.assertEqual(order, ["event1", "event2", "event10"])

    def test_a_line_does_not_end_in_padding(self):
        # Trailing blanks are cells on a braille display, and the last
        # column is empty when a node could not be opened.
        for line in (cli.row("event8", "permission denied"),
                     cli.row("event0", "ignored", "Power Button")):
            self.assertEqual(line, line.rstrip())

    def test_a_reason_reads_as_a_phrase(self):
        # It sits in a column of lowercase phrases; strerror capitalises.
        self.assertEqual(cli.reason(OSError(errno.EACCES,
                                            "Permission denied")),
                         "permission denied")

    def test_an_errno_with_nothing_to_say_still_says_something(self):
        self.assertEqual(cli.reason(OSError()), "cannot be opened")

    def test_a_node_keeps_its_name_and_loses_the_directory(self):
        self.assertEqual(cli.short("/dev/input/event3"), "event3")

    def test_anything_elsewhere_is_shown_whole(self):
        # Nothing outside the directory reaches the listing today, but
        # trimming a fixed number of characters off one that did would
        # produce a path that names nothing.
        self.assertEqual(cli.short("/dev/other/event3"),
                         "/dev/other/event3")

    def test_the_heading_says_where_the_nodes_are(self):
        """The directory is in the heading, not on every line.

        It is the same eleven characters on every row, and these are
        read a cell at a time on a braille display.
        """
        said = self.listing([self.Device("/dev/input/event1")])
        heading = said.splitlines()[0]
        self.assertTrue(heading.startswith("/dev/input/*"), heading)
        self.assertIn("event1", said)
        self.assertNotIn("/dev/input/event1", said)

    def test_a_node_that_cannot_be_opened_says_why(self):
        """Not "ignored": btkey has no idea what it is.

        A node it cannot open is the one case where the listing has no
        answer, and saying so is different from saying it looked and
        decided against it.  The kernel's own words are the honest ones,
        and there is nothing to put after them: the name cannot be read
        without opening it.
        """
        def refuse(path):
            raise OSError(errno.EACCES, "Permission denied")

        self.addCleanup(setattr, cli.evdev, "InputDevice",
                        cli.evdev.InputDevice)
        self.addCleanup(setattr, cli.glob, "glob", cli.glob.glob)
        self.addCleanup(setattr, cli.evdev, "discover", cli.evdev.discover)
        cli.evdev.discover = lambda extra=(), known=(): ([], [])
        cli.evdev.InputDevice = refuse
        cli.glob.glob = lambda pattern: ["/dev/input/event3"]
        cli.list_devices([])
        said = self.out.getvalue()
        self.assertIn("event3", said)
        self.assertIn("permission denied", said)
        self.assertNotIn("ignored", said)


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
