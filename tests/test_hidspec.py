#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The HID report descriptor, parsed rather than eyeballed.

The descriptor is a flat byte string whose meaning depends on state: the
global items - report size, count, logical range, usage page - stay in
force until something changes them, so an item can be wrong because of a
line thirty bytes earlier that says nothing about it.  Reading it by eye
is how the LED range went unnoticed.

It is also the most expensive thing here to get wrong.  iOS reads it out
of the SDP record at bond time and never looks again, so a correction is
invisible to an already-paired phone until it is forgotten and paired
afresh.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import hidspec

INPUT, OUTPUT, FEATURE = 0x80, 0x90, 0xB0
COLLECTION, END_COLLECTION = 0xA0, 0xC0


class Item:
    """One main item, with the global state that was in force for it."""

    def __init__(self, kind, flags, state, usages):
        self.kind = kind
        self.flags = flags
        self.state = dict(state)
        self.usages = list(usages)

    @property
    def bits(self):
        return self.state["report_size"] * self.state["report_count"]

    @property
    def constant(self):
        return bool(self.flags & 0x01)

    def __repr__(self):
        return "<%s report %s, %d x %d bits, logical %s..%s>" % (
            self.kind, self.state.get("report_id"),
            self.state["report_count"], self.state["report_size"],
            self.state["logical_min"], self.state["logical_max"])


GLOBALS = {0x04: "usage_page", 0x14: "logical_min", 0x24: "logical_max",
           0x74: "report_size", 0x94: "report_count", 0x84: "report_id"}


def parse(descriptor):
    """Walk the item stream, carrying the global state as the host does."""
    items, state, usages = [], {}, []
    index = 0
    while index < len(descriptor):
        prefix = descriptor[index]
        size = prefix & 0x03
        size = 4 if size == 3 else size
        data = int.from_bytes(descriptor[index + 1:index + 1 + size], "little")
        tag = prefix & 0xFC
        index += 1 + size

        if tag in GLOBALS:
            state[GLOBALS[tag]] = data
        elif tag == 0x08:                       # Usage
            usages.append(data)
        elif tag == 0x18:                       # Usage Minimum
            state["usage_min"] = data
        elif tag == 0x28:                       # Usage Maximum
            state["usage_max"] = data
        elif tag in (INPUT, OUTPUT, FEATURE):
            kind = {INPUT: "input", OUTPUT: "output", FEATURE: "feature"}[tag]
            items.append(Item(kind, data, state, usages))
            usages = []
        elif tag == COLLECTION:
            usages = []
        elif tag == END_COLLECTION:
            pass
    return items


class ParseTest(unittest.TestCase):
    """The parser above, against a descriptor whose answers are known."""

    def test_it_carries_globals_forward(self):
        # Report Size (8), then two Input items: both are eight bits.
        items = parse(bytes([0x75, 0x08, 0x95, 0x01, 0x81, 0x02, 0x81, 0x02]))
        self.assertEqual([item.bits for item in items], [8, 8])

    def test_a_later_global_replaces_an_earlier_one(self):
        items = parse(bytes([0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
                             0x75, 0x08, 0x81, 0x02]))
        self.assertEqual([item.bits for item in items], [8, 64])

    def test_it_reads_two_byte_data(self):
        items = parse(bytes([0x26, 0x3C, 0x02, 0x75, 0x10, 0x95, 0x01,
                             0x81, 0x00]))
        self.assertEqual(items[0].state["logical_max"], 0x023C)


class DescriptorTest(unittest.TestCase):
    def setUp(self):
        self.items = parse(hidspec.REPORT_DESCRIPTOR)

    def named(self, kind, report_id):
        return [item for item in self.items
                if item.kind == kind
                and item.state.get("report_id") == report_id]

    # -- the property that was wrong -------------------------------------

    def test_every_field_fits_its_logical_range(self):
        """A logical range wider than the field it describes is malformed.

        This is the check that was missing.  The LED output item is one bit
        per field and inherited Logical Maximum (255) from the key array
        above it, because those items are global and the boot keyboard
        descriptor - which omits them there - puts the LEDs first.
        """
        for item in self.items:
            if item.constant:
                continue          # padding: the range does not describe it
            size = item.state["report_size"]
            span = item.state["logical_max"] - item.state["logical_min"]
            self.assertLessEqual(
                span, (1 << size) - 1,
                "%r describes more values than %d bit%s can hold"
                % (item, size, "" if size == 1 else "s"))

    def test_the_led_bits_are_one_bit_booleans(self):
        led = self.named("output", hidspec.REPORT_ID_KEYBOARD)[0]
        self.assertEqual(led.state["report_size"], 1)
        self.assertEqual(led.state["logical_min"], 0)
        self.assertEqual(led.state["logical_max"], 1)

    # -- the shape the code sends ----------------------------------------

    def test_the_keyboard_input_report_is_eight_bytes(self):
        # One modifier byte, one reserved, six key slots - what
        # send_keyboard builds.
        bits = sum(item.bits
                   for item in self.named("input", hidspec.REPORT_ID_KEYBOARD))
        self.assertEqual(bits, 64)

    def test_the_led_output_report_is_one_byte(self):
        bits = sum(item.bits
                   for item in self.named("output", hidspec.REPORT_ID_KEYBOARD))
        self.assertEqual(bits, 8)

    def test_the_consumer_report_is_two_bytes(self):
        bits = sum(item.bits
                   for item in self.named("input", hidspec.REPORT_ID_CONSUMER))
        self.assertEqual(bits, 16)

    def test_every_report_is_a_whole_number_of_bytes(self):
        totals = {}
        for item in self.items:
            key = (item.kind, item.state.get("report_id"))
            totals[key] = totals.get(key, 0) + item.bits
        for key, bits in totals.items():
            self.assertEqual(bits % 8, 0,
                             "%s report %s is %d bits" % (key + (bits,)))

    def test_the_key_slots_reach_every_usage_the_table_uses(self):
        from btkey import keycodes
        slots = self.named("input", hidspec.REPORT_ID_KEYBOARD)[-1]
        self.assertGreaterEqual(slots.state["logical_max"],
                                max(keycodes.KEYBOARD.values()))

    def test_the_consumer_range_reaches_every_usage_the_table_uses(self):
        from btkey import keycodes
        item = self.named("input", hidspec.REPORT_ID_CONSUMER)[0]
        self.assertGreaterEqual(item.state["logical_max"],
                                max(keycodes.CONSUMER.values()))

    # -- collections balance ---------------------------------------------

    def test_the_collections_close(self):
        depth = 0
        index = 0
        descriptor = hidspec.REPORT_DESCRIPTOR
        while index < len(descriptor):
            prefix = descriptor[index]
            size = prefix & 0x03
            size = 4 if size == 3 else size
            tag = prefix & 0xFC
            if tag == COLLECTION:
                depth += 1
            elif tag == END_COLLECTION:
                depth -= 1
            self.assertGreaterEqual(depth, 0, "a collection closes twice")
            index += 1 + size
        self.assertEqual(depth, 0, "a collection is left open")


class SdpTest(unittest.TestCase):
    def setUp(self):
        self.record = hidspec.service_record("btkey", "a keyboard", "btkey")

    def test_the_profile_version_matches_the_hid_version(self):
        # The two have to agree; a host reads whichever it prefers.
        self.assertEqual(self.record.count('value="0x0101"'), 2)

    def test_the_descriptor_is_carried_in_the_record(self):
        # This is the copy iOS caches at bond time.
        self.assertIn(hidspec.REPORT_DESCRIPTOR.hex(), self.record.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
