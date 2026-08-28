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

import errno
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
        self.refused = False
        self.grab_error = None
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
            # What the kernel returns when another program holds it.
            self.grab_error = errno.EBUSY
            return False
        self.grab_error = None
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


def make_set(*devices, on_event=None, on_debug=None):
    """A real KeyboardSet over recording devices, skipping discovery."""
    keyboards = REAL_KEYBOARD_SET(on_event=on_event, on_debug=on_debug)
    keyboards.devices = {device.path: device for device in devices}
    return keyboards


def keyboard_factory(*devices):
    """A stand-in for evdev.KeyboardSet that yields the real thing."""
    def factory(extra_paths=(), on_event=None, on_debug=None):
        keyboards = make_set(*devices, on_event=on_event, on_debug=on_debug)
        # Discovery would sweep the injected devices away in favour of
        # whatever this machine really has; refresh has its own tests.
        keyboards.refresh = lambda: ([], [])
        return keyboards
    return factory


class RealGrabTest(unittest.TestCase):
    """InputDevice.grab itself, over a stubbed ioctl.

    Everything else here drives recording devices, so the one method that
    actually calls EVIOCGRAB, and the one place the reason for a refusal
    is captured, had no test of their own.
    """

    def device(self, fails_with=None):
        device = evdev.InputDevice.__new__(evdev.InputDevice)
        device.fd = -1
        device.grabbed = False
        device.grab_error = None

        def ioctl(fd, request, arg):
            if fails_with is not None:
                raise OSError(fails_with, os.strerror(fails_with))
            return 0

        saved, evdev.fcntl.ioctl = evdev.fcntl.ioctl, ioctl
        self.addCleanup(setattr, evdev.fcntl, "ioctl", saved)
        return device

    def test_a_grab_that_works_says_so(self):
        device = self.device()
        self.assertTrue(device.grab())
        self.assertTrue(device.grabbed)
        self.assertIsNone(device.grab_error)

    def test_a_refusal_keeps_the_reason(self):
        device = self.device(fails_with=errno.EBUSY)
        self.assertFalse(device.grab())
        self.assertEqual(device.grab_error, errno.EBUSY)
        self.assertFalse(device.grabbed)

    def test_a_different_refusal_keeps_that_reason_instead(self):
        device = self.device(fails_with=errno.ENODEV)
        self.assertFalse(device.grab())
        self.assertEqual(device.grab_error, errno.ENODEV)

    def test_a_success_after_a_refusal_clears_it(self):
        device = self.device(fails_with=errno.EBUSY)
        device.grab()
        again = self.device()
        again.grab_error = errno.EBUSY
        again.grab()
        self.assertIsNone(again.grab_error)

    def test_grabbing_what_we_hold_is_not_a_second_ioctl(self):
        device = self.device(fails_with=errno.EBUSY)
        device.grabbed = True
        self.assertTrue(device.grab())      # would raise if it tried


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

    def test_a_device_that_went_away_is_not_blamed_on_another_program(self):
        """EBUSY is somebody else holding it; ENODEV is it not being there.

        The kernel keeps one grab per device and refuses a second with
        EBUSY, so that is nearly always the reason - but reporting it as
        the reason when the device has been unplugged sends whoever reads
        the line hunting for a program that does not exist.
        """
        chatter = []
        device = RecordingDevice("/a", grabbable=False)
        keyboards = make_set(device, on_debug=chatter.append)

        def gone():
            device.grab_error = errno.ENODEV
            return False

        device.grab = gone
        keyboards.grab_all()
        self.assertEqual(len(chatter), 1)
        self.assertNotIn("another program", chatter[0])
        self.assertIn("No such device", chatter[0])

    def test_a_refused_grab_is_named_and_the_others_still_grab(self):
        chatter = []
        one = RecordingDevice("/a", grabbable=False)
        two = RecordingDevice("/b")
        make_set(one, two, on_debug=chatter.append).grab_all()
        self.assertFalse(one.grabbed)
        self.assertTrue(two.grabbed)
        self.assertEqual(len(chatter), 1)
        self.assertIn("/a", chatter[0])

    def test_one_keyboard_of_several_being_held_is_not_announced(self):
        """Every second machine has something sitting on a device.

        BRLTTY holds the keyboard it takes commands from, keywatch holds
        one to catch its hotkeys, and neither is a fault; the devices that
        matter still come.  Saying so on the console at every switch is
        noise, so it is left to --debug.
        """
        noted = []
        make_set(RecordingDevice("/a", grabbable=False),
                 RecordingDevice("/b"), on_event=noted.append).grab_all()
        self.assertEqual(noted, [])

    def test_not_one_keyboard_coming_is_announced(self):
        """The case where quiet would be a lie.

        btkey with no keyboard at all looks exactly like btkey with a
        phone that has stopped listening, and the two are chased in
        completely different places.
        """
        noted = []
        make_set(RecordingDevice("/a", grabbable=False),
                 RecordingDevice("/b", grabbable=False),
                 on_event=noted.append).grab_all()
        self.assertEqual(len(noted), 1, noted)
        self.assertIn("no keyboard", noted[0])

    def test_having_a_keyboard_again_is_announced_too(self):
        noted = []
        device = RecordingDevice("/a", grabbable=False)
        keyboards = make_set(device, on_event=noted.append)
        keyboards.grab_all()
        keyboards.ungrab_all()
        device.grabbable = True
        keyboards.grab_all()
        self.assertEqual(len(noted), 2, noted)
        self.assertIn("came free", noted[1])

    def test_having_no_keyboard_is_announced_once_not_at_every_switch(self):
        noted = []
        keyboards = make_set(RecordingDevice("/a", grabbable=False),
                             on_event=noted.append)
        for _ in range(3):
            keyboards.grab_all()
            keyboards.ungrab_all()
        self.assertEqual(len(noted), 1, noted)

    def test_no_devices_at_all_is_not_reported_here(self):
        # Nothing was discovered, which startup complains about in its own
        # words; grab_all has nothing to say about a set it was handed
        # empty.
        noted = []
        make_set(on_event=noted.append).grab_all()
        self.assertEqual(noted, [])

    def test_a_refusal_is_reported_once_not_at_every_switch(self):
        # grab_all runs on every return to the foreground, and a device
        # held by something else stays held; saying so each time buries
        # everything else.
        chatter = []
        device = RecordingDevice("/a", grabbable=False)
        keyboards = make_set(device, on_debug=chatter.append)
        for _ in range(3):
            keyboards.grab_all()
            keyboards.ungrab_all()
        self.assertEqual(len(chatter), 1, chatter)

    def test_coming_free_later_is_reported_too(self):
        """The other half of what looked like flakiness.

        Whatever held the device can let go between one console switch and
        the next, and a keyboard that quietly starts reaching the phone is
        as confusing as one that quietly stops.
        """
        chatter = []
        device = RecordingDevice("/a", grabbable=False)
        keyboards = make_set(device, on_debug=chatter.append)
        keyboards.grab_all()
        keyboards.ungrab_all()
        device.grabbable = True
        keyboards.grab_all()
        self.assertEqual(len(chatter), 2, chatter)
        self.assertIn("came free", chatter[1])

    def test_a_device_that_was_always_ours_is_never_mentioned(self):
        noted, chatter = [], []
        keyboards = make_set(RecordingDevice("/a"), on_event=noted.append,
                             on_debug=chatter.append)
        keyboards.grab_all()
        keyboards.ungrab_all()
        keyboards.grab_all()
        self.assertEqual(noted, [])
        self.assertEqual(chatter, [])

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

    def grabbing_session(self, *devices, **overrides):
        """A session whose every console line is collected.

        Session.log is patched on the class, not the instance: the
        KeyboardSet is handed the bound method while the Session is being
        built, so replacing it afterwards would leave the set reporting
        to the original and the wiring untested.
        """
        from btkey.session import Session
        logged = []
        self.addCleanup(setattr, Session, "log", Session.log)
        Session.log = lambda self, message: logged.append(message)
        session = make_session(keyboards=keyboard_factory(*devices),
                               **overrides)
        return session, logged

    def test_a_held_keyboard_is_only_named_under_debug(self):
        """The console is a few lines of braille; it has to stay readable.

        A machine running BRLTTY or a hotkey daemon has something sitting
        on a device, every session, and nothing is wrong.
        """
        session, logged = self.grabbing_session(
            RecordingDevice("/a", grabbable=False), RecordingDevice("/b"))
        session.keyboards.grab_all()
        self.assertEqual(logged, [])

        # A second session, because a refusal is only reported once.
        session, logged = self.grabbing_session(
            RecordingDevice("/a", grabbable=False), RecordingDevice("/b"),
            debug=True)
        session.keyboards.grab_all()
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("/a", logged[0])

    def test_having_no_keyboard_at_all_is_said_without_debug(self):
        session, logged = self.grabbing_session(
            RecordingDevice("/a", grabbable=False))
        session.keyboards.grab_all()
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("no keyboard", logged[0])

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
