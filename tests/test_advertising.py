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
