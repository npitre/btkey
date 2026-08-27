#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The bytes that actually go down the interrupt channel.

Everywhere else the link is a fake that records calls, which says nothing
about whether the report on the wire is the one the descriptor promised.
A byte wrong here is not a crash: it is a phone typing something else, or
a modifier that never lets go, discovered by reading what arrived.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import btlink, hidspec, keycodes

# HIDP header for an input report: DATA transaction, INPUT report type.
HEADER = 0xA1


class FakeSocket:
    def __init__(self, delay=0.0):
        self.sent = []
        self.delay = delay

    def send(self, payload):
        if self.delay:
            time.sleep(self.delay)
        self.sent.append(payload)
        return len(payload)


class AccountingTest(unittest.TestCase):
    """How much of a probe went into waiting for the radio.

    The socket blocks, so a phone that cannot absorb reports as fast as
    btkey produces them stops the main loop for as long as it takes.  From
    outside, that is indistinguishable from btkey being slow, which is the
    reason for counting it.
    """

    def make(self, delay=0.0):
        link = object.__new__(btlink.BluetoothHID)
        link.interrupt = FakeSocket(delay)
        link.debug = False
        link.event = lambda message: None
        link._last_keyboard = b"\0" * 8
        link.sent_reports = 0
        link.send_seconds = 0.0
        return link

    def test_every_report_is_counted(self):
        link = self.make()
        link.send_keyboard(0, [0x04])
        link.send_consumer(0x00E9)
        self.assertEqual(link.sent_reports, 2)

    def test_a_slow_send_is_counted_as_waiting(self):
        link = self.make(delay=0.02)
        link.send_keyboard(0, [0x04])
        self.assertGreaterEqual(link.send_seconds, 0.015)

    def test_a_fast_send_is_barely_counted(self):
        link = self.make()
        for _ in range(10):
            link.send_keyboard(0, [0x04])
        self.assertLess(link.send_seconds, 0.05)

    def test_a_report_that_went_nowhere_is_not_counted(self):
        link = self.make()
        link.interrupt = None
        link.send_keyboard(0, [0x04])
        self.assertEqual(link.sent_reports, 0)

    def test_a_report_the_link_refused_is_not_counted(self):
        # Counting a failed write would put the count above what the phone
        # actually received, which is the one number this is for.
        link = self.make()
        dropped = []
        link.disconnect = dropped.append

        def refuse(payload):
            raise OSError(32, "Broken pipe")

        link.interrupt.send = refuse
        link.send_keyboard(0, [0x04])
        self.assertEqual(link.sent_reports, 0)
        self.assertTrue(dropped)


class WireTest(unittest.TestCase):
    def setUp(self):
        self.socket = FakeSocket()
        self.link = object.__new__(btlink.BluetoothHID)
        self.link.interrupt = self.socket
        self.link.debug = False
        self.link.event = lambda message: None
        self.link._last_keyboard = b"\0" * 8

    @property
    def last(self):
        return self.socket.sent[-1]

    # -- the keyboard report ---------------------------------------------

    def test_an_empty_report_is_the_header_and_eight_zeroes(self):
        self.link.send_keyboard(0, [])
        self.assertEqual(self.last,
                         bytes([HEADER, hidspec.REPORT_ID_KEYBOARD]) + b"\0" * 8)

    def test_the_second_byte_of_the_report_is_the_reserved_one(self):
        # Byte 1 is reserved by the boot keyboard descriptor; a usage put
        # there is silently ignored, which looks like a dropped keystroke.
        self.link.send_keyboard(0, [0x04])
        self.assertEqual(self.last[3], 0x00)
        self.assertEqual(self.last[4], 0x04)

    def test_modifiers_go_in_the_first_byte(self):
        self.link.send_keyboard(0x02, [])       # left shift
        self.assertEqual(self.last[2], 0x02)

    def test_six_keys_fit_and_the_seventh_does_not(self):
        # The boot protocol has six slots; anything beyond is dropped here
        # rather than overrunning the report.
        self.link.send_keyboard(0, list(range(4, 11)))
        self.assertEqual(len(self.last), 10)
        self.assertEqual(self.last[4:], bytes(range(4, 10)))

    def test_short_lists_are_padded_not_truncated(self):
        self.link.send_keyboard(0, [0x04, 0x05])
        self.assertEqual(self.last[4:], bytes([0x04, 0x05, 0, 0, 0, 0]))

    def test_the_report_id_distinguishes_the_two_reports(self):
        self.link.send_keyboard(0, [])
        keyboard = self.last[1]
        self.link.send_consumer(0)
        self.assertNotEqual(keyboard, self.last[1])

    def test_what_was_sent_is_what_a_get_report_replays(self):
        # iOS asks for the current report over the control channel; the
        # answer has to be the state the phone is actually in.
        self.link.send_keyboard(0x02, [0x04])
        self.assertEqual(self.link._last_keyboard,
                         bytes([0x02, 0x00, 0x04, 0, 0, 0, 0, 0]))

    # -- the consumer report ---------------------------------------------

    def test_a_consumer_usage_goes_out_little_endian(self):
        # Volume up is 0x00e9: low byte first, or the phone reads 0xe900.
        self.link.send_consumer(0x00E9)
        self.assertEqual(self.last,
                         bytes([HEADER, hidspec.REPORT_ID_CONSUMER, 0xE9, 0x00]))

    def test_letting_go_is_a_zero_usage(self):
        self.link.send_consumer(0)
        self.assertEqual(self.last[2:], b"\0\0")

    def test_a_two_byte_usage_survives_intact(self):
        self.link.send_consumer(0x0223)         # AC Home
        self.assertEqual(self.last[2:], bytes([0x23, 0x02]))

    # -- a real keystroke, end to end ------------------------------------

    def test_shift_a_is_the_report_the_descriptor_promises(self):
        KEY_A, KEY_LEFTSHIFT = 30, 42
        self.link.send_keyboard(keycodes.MODIFIERS[KEY_LEFTSHIFT],
                                [keycodes.KEYBOARD[KEY_A]])
        self.assertEqual(self.last,
                         bytes([HEADER, 1, 0x02, 0x00, 0x04, 0, 0, 0, 0, 0]))

    # -- a channel that is not there -------------------------------------

    def test_nothing_is_sent_with_no_interrupt_channel(self):
        self.link.interrupt = None
        self.link.send_keyboard(0, [0x04])      # must not raise
        self.assertEqual(self.socket.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
