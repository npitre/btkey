#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The grab lifecycle: who holds the keyboards, and when they let go.

Every other test in the suite drives a fake KeyboardSet, so the real one -
which decides whether your keystrokes reach the console or vanish - was
never exercised.  These tests run the real class over recording devices,
and the session tests below hand that real class to a real Session.

The failure being guarded against is not subtle: a grab that outlives the
console leaves the machine with no working keyboard, and the only way out
is another machine or the power button.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import evdev

import test_keys
from test_keys import make_session


class RecordingDevice:
    """Stands in for one /dev/input/event*, recording what is done to it.

    Appends to a shared trace so that order across devices - and against
    the reports the session sends - is visible, not just the final state.
    """

    def __init__(self, path, trace=None, grabbable=True, leds=0x02):
        self.path = path
        self.name = "recording %s" % path
        self.saved_leds = None
        self.grabbable = grabbable
        self.grabbed = False
        self.closed = False
        self.held = set()
        self.led_writes = []
        self._leds = leds
        self.trace = trace if trace is not None else []

    def is_keyboard(self):
        return True

    def grab(self):
        self.trace.append("grab %s" % self.path)
        if not self.grabbable:
            return False
        self.grabbed = True
        return True

    def ungrab(self):
        self.trace.append("ungrab %s" % self.path)
        self.grabbed = False

    def leds(self):
        return self._leds

    def set_leds(self, mask):
        self.trace.append("leds %s=%#04x" % (self.path, mask))
        self.led_writes.append(mask)
        self._leds = mask
        return True

    def pressed_keys(self):
        return set(self.held)

    def close(self):
        self.trace.append("close %s" % self.path)
        self.closed = True


# Held from before make_session() puts a factory in its place, so that the
# factory can build the real thing without calling itself.
REAL_KEYBOARD_SET = evdev.KeyboardSet


def make_set(*devices, on_event=None):
    """A real KeyboardSet over recording devices, skipping discovery."""
    keyboards = REAL_KEYBOARD_SET(on_event=on_event)
    keyboards.devices = {device.path: device for device in devices}
    return keyboards


def keyboard_factory(*devices):
    """A stand-in for evdev.KeyboardSet that yields the real thing."""
    def factory(extra_paths=(), on_event=None):
        keyboards = make_set(*devices, on_event=on_event)
        # Discovery would sweep the injected devices away in favour of
        # whatever this machine really has; refresh has its own tests.
        keyboards.refresh = lambda: ([], [])
        return keyboards
    return factory


class KeyboardSetTest(unittest.TestCase):
    """The real KeyboardSet, over devices that record."""

    def test_grab_all_takes_every_keyboard(self):
        one, two = RecordingDevice("/a"), RecordingDevice("/b")
        make_set(one, two).grab_all()
        self.assertTrue(one.grabbed)
        self.assertTrue(two.grabbed)

    def test_grab_all_snapshots_the_console_leds_first(self):
        # Before the grab, or the saved state is whatever the phone had.
        device = RecordingDevice("/a", leds=0x04)
        make_set(device).grab_all()
        self.assertEqual(device.saved_leds, 0x04)
        self.assertEqual(device.trace, ["grab /a"])

    def test_ungrab_all_releases_every_keyboard(self):
        one, two = RecordingDevice("/a"), RecordingDevice("/b")
        keyboards = make_set(one, two)
        keyboards.grab_all()
        keyboards.ungrab_all()
        self.assertFalse(one.grabbed)
        self.assertFalse(two.grabbed)
        self.assertFalse(keyboards.grabbed)

    def test_ungrab_all_hands_the_console_leds_back(self):
        device = RecordingDevice("/a", leds=0x02)
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.set_leds(0x01)          # the phone's num lock
        keyboards.ungrab_all()
        self.assertEqual(device.led_writes, [0x01, 0x02])
        self.assertIsNone(device.saved_leds)

    def test_the_leds_go_back_before_the_grab_is_released(self):
        # Once ungrabbed the console owns the LEDs again and will drive
        # them itself; a write landing after that is a race.
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.ungrab_all()
        self.assertLess(device.trace.index("leds /a=0x02"),
                        device.trace.index("ungrab /a"))

    def test_close_releases_before_closing(self):
        # Closing the fd drops the grab too, but only as a side effect of
        # the last reference going away - too subtle to rely on.
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.close()
        self.assertEqual(device.trace[-2:], ["ungrab /a", "close /a"])
        self.assertEqual(keyboards.devices, {})

    def test_close_covers_every_device(self):
        one, two = RecordingDevice("/a"), RecordingDevice("/b")
        keyboards = make_set(one, two)
        keyboards.grab_all()
        keyboards.close()
        for device in (one, two):
            self.assertFalse(device.grabbed)
            self.assertTrue(device.closed)

    def test_a_refused_grab_is_named_and_the_others_still_grab(self):
        logged = []
        one = RecordingDevice("/a", grabbable=False)
        two = RecordingDevice("/b")
        make_set(one, two, on_event=logged.append).grab_all()
        self.assertFalse(one.grabbed)
        self.assertTrue(two.grabbed)
        self.assertEqual(len(logged), 1)
        self.assertIn("/a", logged[0])

    def test_restore_leds_keeps_the_grab(self):
        # Between the console's state and the phone's, without giving the
        # keyboard back in between.
        device = RecordingDevice("/a", leds=0x02)
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.set_leds(0x01)
        keyboards.restore_leds()
        self.assertEqual(device.led_writes, [0x01, 0x02])
        self.assertTrue(device.grabbed)
        self.assertEqual(device.saved_leds, 0x02)

    def test_held_keys_unions_across_devices(self):
        # Modifiers and letters routinely live on different devices.
        one, two = RecordingDevice("/a"), RecordingDevice("/b")
        one.held = {42}
        two.held = {30}
        self.assertEqual(make_set(one, two).held_keys(), {42, 30})


class SessionGrabTest(unittest.TestCase):
    """A real Session over a real KeyboardSet."""

    def session(self, *devices):
        return make_session(keyboards=keyboard_factory(*devices))

    def test_going_to_the_foreground_grabs(self):
        device = RecordingDevice("/a")
        session = self.session(device)
        session.foreground = False
        session.set_foreground(True)
        self.assertTrue(device.grabbed)

    def test_leaving_the_foreground_ungrabs(self):
        device = RecordingDevice("/a")
        session = self.session(device)
        session.set_foreground(True)
        session.set_foreground(False)
        self.assertFalse(device.grabbed)

    def test_the_phone_is_let_go_before_the_keyboard_is(self):
        # Ungrab first and the console gets the key-up, so the phone never
        # hears it and holds that key down for good.
        trace = []
        device = RecordingDevice("/a", trace=trace)
        session = self.session(device)
        session.set_foreground(True)
        report = session.link.send_keyboard

        def watched(*args, **kwargs):
            trace.append("report")
            return report(*args, **kwargs)

        session.link.send_keyboard = watched
        session.set_foreground(False)
        self.assertIn("report", trace)
        self.assertLess(trace.index("report"), trace.index("ungrab /a"))

    def test_a_hotplugged_keyboard_is_grabbed_and_snapshotted(self):
        device = RecordingDevice("/a", leds=0x04)
        keyboards = make_set()
        keyboards.grab_all()
        keyboards.devices["/a"] = device
        device.saved_leds = device.leds()
        self.assertTrue(device.grab())
        keyboards.ungrab_all()
        self.assertEqual(device.led_writes, [0x04])

    def test_startup_failure_still_gives_the_keyboards_back(self):
        # The path that used to exit past the teardown entirely.
        from btkey import btlink

        device = RecordingDevice("/a")
        session = self.session(device)
        session.set_foreground(True)

        def refuse():
            raise btlink.ProfileNotAvailable("bluetoothd holds the profile")

        session.start_services = refuse
        session.stop_services = lambda: None
        errors = []
        saved, sys.stderr = sys.stderr, _Collector(errors)
        try:
            code = session.run()
        finally:
            sys.stderr = saved
        self.assertEqual(code, 1)
        self.assertFalse(device.grabbed)
        self.assertTrue(device.closed)
        self.assertIn("bluetoothd holds the profile", "".join(errors))


class _Collector:
    def __init__(self, sink):
        self.sink = sink

    def write(self, text):
        self.sink.append(text)

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
