#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The class of device, and holding it against bluetoothd.

Getting this wrong is expensive out of all proportion to the code: iOS
caches what a device advertises at bond time, so every mistake here costs
a forget-and-re-pair to see whether the correction worked.  That is what
these tests are for.
"""

import argparse
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib

from btkey import advertising, hidspec

AUDIO_BITS = (hidspec.SERVICE_AUDIO | hidspec.SERVICE_RENDERING
              | hidspec.SERVICE_CAPTURING)


class FakeLink:
    def __init__(self, cod=0x0c0104):
        self.cod = cod
        self.watch = None
        self.removed = False

    def class_of_device(self):
        return self.cod

    def audio_profiles(self):
        return ["A2DP Sink"]

    def all_uuids(self):
        return ["0000110b-0000-1000-8000-00805f9b34fb"]

    def watch_class(self, callback):
        self.watch = callback
        link = self

        class Match:
            def remove(self):
                link.removed = True
                link.watch = None

        return Match()


class FakeCod:
    def __init__(self):
        self.written = []

    def write(self, value):
        self.written.append(value)
        return True


def make(audio=True, logged=None, announced=None, recorded=None):
    options = argparse.Namespace(device_class=0x000540, audio=audio)
    link = FakeLink()
    agent = advertising.Advertising(
        link, options,
        logged.append if logged is not None else (lambda text: None),
        announced.append if announced is not None else (lambda text: None),
        recorded.append if recorded is not None else (lambda text: None))
    return agent, link


class WantedClassTest(unittest.TestCase):
    def test_the_audio_bits_are_added(self):
        agent, _ = make()
        self.assertEqual(agent.wanted_class(), 0x000540 | AUDIO_BITS)

    def test_no_audio_leaves_the_bare_class(self):
        agent, _ = make(audio=False)
        self.assertEqual(agent.wanted_class(), 0x000540)

    def test_audio_is_the_bit_a_phone_looks_at(self):
        # Rendering and Capturing come with A2DP; Audio does not, and it
        # is the one that decides whether sound can go here at all.
        agent, _ = make()
        self.assertTrue(agent.wanted_class() & hidspec.SERVICE_AUDIO)


class PutBackTest(unittest.TestCase):
    """bluetoothd recomputes the class whenever the UUID set changes."""

    def setUp(self):
        self.agent, self.link = make()
        self.agent.cod = FakeCod()

    def test_a_class_that_lost_our_bits_is_written_back(self):
        self.agent.class_changed(0x0c0104)
        self.assertEqual(self.agent.cod.written, [self.agent.wanted_class()])

    def test_a_class_that_still_has_them_is_left_alone(self):
        # bluetoothd sets bits of its own; only ours have to survive.
        self.agent.class_changed(self.agent.wanted_class() | 0x000004)
        self.assertEqual(self.agent.cod.written, [])

    def test_the_recheck_backstop_puts_it_back_too(self):
        self.link.cod = 0x0c0104
        self.assertTrue(self.agent.recheck())
        self.assertEqual(self.agent.cod.written, [self.agent.wanted_class()])


class StartTest(unittest.TestCase):
    """What start() puts in place, without letting it near the controller."""

    def setUp(self):
        self.agent, self.link = make()
        self.addCleanup(setattr, advertising, "ClassOfDevice",
                        advertising.ClassOfDevice)
        advertising.ClassOfDevice = lambda on_event=None: FakeCod()
        timer = advertising.GLib.timeout_add_seconds
        advertising.GLib.timeout_add_seconds = lambda *args: 1
        self.addCleanup(setattr, advertising.GLib, "timeout_add_seconds",
                        timer)

    def test_it_records_the_class_being_advertised(self):
        # What the backstop compares against.  Without it the first look
        # finds a difference no signal was ever going to account for, and
        # reports a lost signal on every run.
        self.link.cod = 0x0c0104
        self.agent.start()
        self.assertEqual(self.agent.seen, 0x0c0104)

    def test_it_watches_for_the_class_moving(self):
        self.agent.start()
        self.assertIsNotNone(self.link.watch)

    def test_no_audio_puts_nothing_in_place(self):
        agent, link = make(audio=False)
        agent.start()
        self.assertIsNone(link.watch)


class BackstopTest(unittest.TestCase):
    """Whether the five-second backstop is earning its wakeups.

    It reads the same property the PropertiesChanged watch reports, so it
    can only ever catch a signal that went missing.  Nothing said whether
    that happens, so now it says so, and the answer decides whether it
    stays.
    """

    # What bluetoothd puts back when it recomputes the class itself.
    OVERWRITTEN = 0x0c0104

    def setUp(self):
        self.agent, self.link = make()
        self.agent.cod = FakeCod()
        self.logged = []
        self.agent.log = self.logged.append
        # What start() does, without the rest of it: the watch delivers
        # into class_changed, and the settled state is what we asked for.
        self.link.watch_class(self.agent.class_changed)
        self.link.cod = self.agent.wanted_class()
        self.agent.seen = self.link.cod

    def blamed(self):
        """The lines blaming a lost signal, not the ordinary put-back."""
        return [line for line in self.logged if "no PropertiesChanged" in line]

    def test_an_unchanged_class_says_nothing(self):
        for _ in range(3):
            self.agent.recheck()
        self.assertEqual(self.logged, [])

    def test_a_class_the_watch_reported_is_not_blamed_on_a_lost_signal(self):
        self.link.cod = self.OVERWRITTEN
        self.link.watch(self.OVERWRITTEN)          # the signal arrives
        self.agent.recheck()
        self.agent.recheck()
        self.assertEqual(self.blamed(), [])

    def test_one_look_is_not_enough_to_call_a_signal_missing(self):
        """It may have been emitted and not yet dispatched.

        Saying a signal was lost when it was merely in flight would send
        whoever reads it looking for a bug in bluetoothd.
        """
        self.link.cod = self.OVERWRITTEN
        self.agent.recheck()
        self.assertEqual(self.blamed(), [])

    def test_a_signal_in_flight_is_not_blamed_when_it_lands(self):
        self.link.cod = self.OVERWRITTEN
        self.agent.recheck()                       # noticed, not yet blamed
        self.link.watch(self.OVERWRITTEN)          # and here it comes
        self.agent.recheck()
        self.assertEqual(self.blamed(), [])

    def test_a_change_no_signal_accounted_for_is_reported(self):
        self.link.cod = self.OVERWRITTEN
        self.agent.recheck()
        self.agent.recheck()
        self.assertEqual(len(self.blamed()), 1, self.logged)

    def test_it_is_blamed_once_and_not_every_five_seconds(self):
        self.link.cod = self.OVERWRITTEN
        for _ in range(6):
            self.agent.recheck()
        self.assertEqual(len(self.blamed()), 1, self.logged)

    def test_a_second_lost_signal_is_reported_again(self):
        self.link.cod = self.OVERWRITTEN
        self.agent.recheck()
        self.agent.recheck()
        self.link.cod = self.OVERWRITTEN | 0x000008
        self.agent.recheck()
        self.agent.recheck()
        self.assertEqual(len(self.blamed()), 2, self.logged)

    def test_a_class_it_cannot_read_is_not_a_lost_signal(self):
        self.link.cod = None
        self.agent.recheck()
        self.agent.recheck()
        self.assertEqual(self.blamed(), [])

    def test_it_puts_the_class_back_on_the_first_look(self):
        """Deciding whose fault it is must not delay the correction.

        Waiting for the second look to be sure a signal was lost would
        leave the wrong class advertised for another RECHECK_SECONDS,
        which is the very window the watch exists to keep short.
        """
        self.link.cod = self.OVERWRITTEN
        self.agent.recheck()
        self.assertEqual(self.agent.cod.written, [self.agent.wanted_class()])
        self.assertEqual(self.blamed(), [])

    def test_it_puts_the_class_back_once_not_at_every_look(self):
        self.link.cod = self.OVERWRITTEN
        for _ in range(5):
            self.agent.recheck()
        self.assertEqual(self.agent.cod.written, [self.agent.wanted_class()])


class ChangeNoticeTest(unittest.TestCase):
    """Saying what moved, not only that something did.

    A phone already paired cannot see a change to what this machine
    advertises until it is forgotten and paired again, so noticing is worth
    a message.  Saying only that it changed leaves the next question
    unanswered, and an installed btkey writes no log file to answer it
    from - which makes the console the only place it can go.
    """

    def setUp(self):
        self.logged, self.announced, self.recorded = [], [], []
        self.agent, self.link = make(logged=self.logged,
                                     announced=self.announced,
                                     recorded=self.recorded)
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        saved = advertising.STATE_FILE
        advertising.STATE_FILE = os.path.join(self.directory, "advertised")
        self.addCleanup(setattr, advertising, "STATE_FILE", saved)

    def test_the_first_run_has_nothing_to_compare_against(self):
        self.agent.check_advertised()
        self.assertEqual(self.announced, [])

    def test_an_unchanged_second_run_says_nothing(self):
        self.agent.check_advertised()
        self.agent.check_advertised()
        self.assertEqual(self.announced, [])

    def test_a_change_is_announced(self):
        self.agent.check_advertised()
        self.link.cod = 0x000540
        self.agent.check_advertised()
        self.assertTrue(any("forget and re-pair" in line
                            for line in self.announced), self.announced)

    def test_what_changed_reaches_the_console_not_only_the_file(self):
        self.agent.check_advertised()
        self.link.cod = 0x000540
        self.agent.check_advertised()
        self.assertTrue(any("advertised was" in line for line in self.logged),
                        self.logged)
        self.assertTrue(any("advertised now" in line for line in self.logged),
                        self.logged)


class StopTest(unittest.TestCase):
    def setUp(self):
        self.agent, self.link = make()
        self.agent.cod = FakeCod()
        self.agent.class_watch = self.link.watch_class(self.agent.class_changed)
        self.agent.recheck_id = GLib.timeout_add_seconds(5, self.agent.recheck)

    def test_stop_lets_go_of_the_signal(self):
        self.agent.stop()
        self.assertTrue(self.link.removed)
        self.assertIsNone(self.agent.class_watch)

    def test_stop_lets_go_of_the_backstop(self):
        source_id = self.agent.recheck_id
        self.agent.stop()
        self.assertEqual(self.agent.recheck_id, 0)
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(
            source_id))

    def test_nothing_is_written_after_stopping(self):
        cod = self.agent.cod
        self.agent.stop()
        self.agent.class_changed(0x0c0104)
        self.assertEqual(cod.written, [])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
