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
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import evdev

from test_keys import make_session


# The session hands a device's descriptor to GLib, which insists on a real
# one.  A single pipe stands in for all of them: nothing is ever written to
# it, so a watch on it never fires, which is what a device that delivers
# through the test rather than through the kernel wants.
QUIET_PIPE = os.pipe()


class RecordingDevice:
    """Stands in for one /dev/input/event*, recording what is done to it.

    Appends to a shared trace so that order across devices - and against
    the reports the session sends - is visible, not just the final state.
    """

    def __init__(self, path, trace=None, grabbable=True, leds=0x02):
        self.path = path
        self.fd = QUIET_PIPE[0]
        self.name = "recording %s" % path
        self.saved_leds = None
        self.grabbable = grabbable
        self.grabbed = False
        self.refused = False
        self.grab_error = None
        self.closed = False
        self.gone = False        # unplugged while we were away
        self.saved_repeat = None
        self.repeat_writes = []
        self.was_held = False
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
        self.trace.append("read leds %s" % self.path)
        return self._leds

    def set_leds(self, mask):
        self.trace.append("leds %s=%#04x" % (self.path, mask))
        self.led_writes.append(mask)
        self._leds = mask
        return True

    def pressed_keys(self):
        return set(self.held)

    def hush_repeat(self):
        if self.saved_repeat is not None:
            return None
        self.saved_repeat = (250, 33)
        self.repeat_writes.append((0, 0))
        self.trace.append("hush %s" % self.path)
        return self.saved_repeat

    def restore_repeat(self):
        if self.saved_repeat is None:
            return None
        was, self.saved_repeat = self.saved_repeat, None
        if not self.closed:
            # The real one writes through an ioctl, which fails without
            # a descriptor - silently, and it still reports what it
            # meant to put back.
            self.repeat_writes.append(was)
            self.trace.append("repeat %s" % self.path)
        return was

    def read_keys(self):
        # Nothing writes to QUIET_PIPE, so no watch on it should ever
        # fire; if one did, the missing method would raise inside a GLib
        # callback on a descriptor that stays ready, which is a spin.
        return []

    def close(self):
        self.trace.append("close %s" % self.path)
        self.closed = True
        self.grabbed = False       # the kernel drops it with the fd

    def reopen(self):
        self.trace.append("reopen %s" % self.path)
        if self.gone:
            return False
        self.closed = False
        return True


# Held from before make_session() puts a factory in its place, so that the
# factory can build the real thing without calling itself.
REAL_KEYBOARD_SET = evdev.KeyboardSet


def make_set(*devices, on_event=None, on_debug=None, on_repeat_debt=None):
    """A real KeyboardSet over recording devices, skipping discovery."""
    keyboards = REAL_KEYBOARD_SET(on_event=on_event, on_debug=on_debug,
                                  on_repeat_debt=on_repeat_debt)
    keyboards.devices = {device.path: device for device in devices}
    return keyboards


def keyboard_factory(*devices):
    """A stand-in for evdev.KeyboardSet that yields the real thing."""
    def factory(extra_paths=(), on_event=None, on_debug=None,
                on_repeat_debt=None):
        keyboards = make_set(*devices, on_event=on_event, on_debug=on_debug,
                             on_repeat_debt=on_repeat_debt)
        # Discovery would sweep the injected devices away in favour of
        # whatever this machine really has; refresh has its own tests.
        keyboards.refresh = lambda: ([], [])
        return keyboards
    return factory


class LosingOneWeHadTest(unittest.TestCase):
    """A keyboard that was ours and is now somebody else's.

    That is not the same as one we never had.  Something took it while
    btkey was on another console, and whatever did may have published a
    loopback for the keys it does not want - which is the device btkey
    should be holding instead.  BRLTTY does exactly that when it is set
    up mid-session.
    """

    def test_losing_one_we_held_is_reported(self):
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        self.assertFalse(keyboards.grab_all())
        device.grabbable = False
        device.grabbed = False
        self.assertTrue(keyboards.grab_all())

    def test_being_refused_one_we_never_had_is_not(self):
        # Ordinary, and the whole reason the two are told apart: this is
        # what stops the looking around from looping.
        keyboards = make_set(RecordingDevice("/a", grabbable=False))
        self.assertFalse(keyboards.grab_all())

    def test_the_ones_we_are_not_holding_can_be_discarded(self):
        held = RecordingDevice("/held")
        refused = RecordingDevice("/refused", grabbable=False)
        keyboards = make_set(held, refused)
        keyboards.grab_all()
        keyboards.discard_refusals()
        self.assertEqual(list(keyboards.devices), ["/held"])

    def test_discarding_settles_what_they_owed(self):
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append((p, r)))
        keyboards.grab_all()
        device.grabbed = False          # let go of while we were away
        keyboards.discard_refusals()
        self.assertEqual(told[-1], ("/a", None))
        self.assertTrue(device.closed)

    def test_a_rediscovered_one_was_never_held_by_us(self):
        """Which is what makes the second look the last one.

        Discarding drops the object, so the keyboard that comes back is
        one btkey has no history with; refusing it is then ordinary, and
        nothing asks for a third look.
        """
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        self.assertTrue(device.was_held)
        keyboards.discard_refusals()    # holds it, so it stays
        device.grabbed = False
        keyboards.discard_refusals()
        again = RecordingDevice("/a", grabbable=False)
        keyboards.devices["/a"] = again
        self.assertFalse(keyboards.grab_all())


class LettingGoTest(unittest.TestCase):
    """Every way of letting a keyboard go, against the same list.

    Taking one does four things and letting it go undoes them, and the
    ways of letting go outnumber the ways of taking: a switch to another
    console, the device being unplugged, a grab that would not come, and
    btkey stopping.  Each was written separately and each was a chance
    to forget a step - which is how the guardian ended up holding
    withdrawn debts and a still-present keyboard ended up mute.

    So they all go through KeyboardSet.release, and this asks every one
    of them the same questions.  A new way of letting go that does not
    come through it fails here rather than in six months.
    """

    def taken(self):
        """A set holding one keyboard, with everything take() does done."""
        told = []
        device = RecordingDevice("/a", leds=0x04)
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append((p, r)))
        keyboards.grab_all()
        self.assertEqual(device.saved_leds, 0x04)
        self.assertEqual(device.saved_repeat, (250, 33))
        self.assertEqual(told, [("/a", (250, 33))])
        del told[:]
        return keyboards, device, told

    def check(self, device, told):
        """What must be true however the keyboard was let go."""
        self.assertEqual(device.led_writes[-1], 0x04,
                         "the console's LED state was not handed back")
        self.assertIsNone(device.saved_leds)
        self.assertEqual(device.repeat_writes[-1], (250, 33),
                         "the key repeat was not put back")
        self.assertIsNone(device.saved_repeat)
        self.assertEqual(told, [("/a", None)],
                         "the guardian still thinks it owes a restore")
        self.assertTrue(device.closed, "the descriptor was not given back")
        self.assertFalse(device.grabbed)

    def test_releasing_one(self):
        keyboards, device, told = self.taken()
        keyboards.release(device)
        self.check(device, told)

    def test_releasing_the_set(self):
        keyboards, device, told = self.taken()
        keyboards.release_all()
        self.check(device, told)

    def test_forgetting_one(self):
        keyboards, device, told = self.taken()
        keyboards.forget(device)
        self.check(device, told)
        self.assertEqual(keyboards.devices, {})

    def test_closing_the_set(self):
        keyboards, device, told = self.taken()
        keyboards.close()
        self.check(device, told)
        self.assertEqual(keyboards.devices, {})

    def test_letting_go_twice_is_harmless(self):
        # It happens: forget() on a device the switch away already
        # released, and the teardown after either.
        keyboards, device, told = self.taken()
        keyboards.release(device)
        keyboards.release(device)
        self.check(device, told)

    def test_switching_to_another_console(self):
        keyboards, device, told = self.taken()
        session = make_session(keyboards=lambda *args, **kwargs: keyboards)
        session.set_foreground(False)
        self.check(device, told)

    def test_a_grab_that_would_not_come(self):
        """Taken on one switch, refused on the next: still let go.

        hold_only_what_we_grabbed used to close it directly, which is
        the shape that leaves a keyboard mute for the rest of the
        machine's uptime.
        """
        keyboards, device, told = self.taken()
        device.grabbable = False
        device.grabbed = False
        keyboards.grab_all()          # tried again, and refused this time
        self.check(device, told)


class RealDeviceLifecycleTest(unittest.TestCase):
    """Opening, closing and opening again, on a real InputDevice.

    Everything else here drives recording devices.  These use the real
    class over an ordinary file: every ioctl it makes fails on one, and
    every one of them is written to survive that, so what is left is the
    descriptor handling, which is the part that matters here.
    """

    def device(self):
        handle, path = tempfile.mkstemp(prefix="btkey-device-")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return evdev.InputDevice(path), path

    def test_closing_gives_the_descriptor_up(self):
        device, _ = self.device()
        device.close()
        self.assertIsNone(device.fd)

    def test_closing_twice_is_not_an_error(self):
        device, _ = self.device()
        device.close()
        device.close()
        self.assertIsNone(device.fd)

    def test_reopening_gives_a_working_descriptor_back(self):
        device, _ = self.device()
        device.close()
        self.assertTrue(device.reopen())
        self.assertIsNotNone(device.fd)
        os.fstat(device.fd)                 # raises if it is not a real one

    def test_reopening_one_that_went_away_says_so(self):
        """Unplugged while another console had the screen.

        Reported rather than raised, so the session can forget it and
        carry on with the keyboards that are still there.
        """
        device, path = self.device()
        device.close()
        os.unlink(path)
        self.assertFalse(device.reopen())
        self.assertIsNone(device.fd)

    def test_reopening_an_open_one_leaves_it_alone(self):
        # Coming back to the foreground reopens everything it has, and
        # what was hotplugged while away is already open.
        device, _ = self.device()
        before = device.fd
        self.assertTrue(device.reopen())
        self.assertEqual(device.fd, before)

    def test_hushing_records_what_was_there_and_turns_it_off(self):
        device, _ = self.device()
        device.has_repeat = True
        asked = []
        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)

        def ioctl(fd, request, arg):
            if request == evdev.EVIOCGREP:
                arg[:] = struct.pack("II", 250, 33)
                return 0
            asked.append(struct.unpack("II", arg))
            return 0

        evdev.fcntl.ioctl = ioctl
        self.assertEqual(device.hush_repeat(), (250, 33))
        self.assertEqual(asked, [(0, 0)])
        self.assertEqual(device.saved_repeat, (250, 33))

    def test_hushing_a_second_time_does_not_record_the_silence(self):
        """Saving (0, 0) would mean putting back nothing at all.

        The keyboard would then stay dead for the rest of the session,
        and the guardian would faithfully restore the deadness too.
        """
        device, _ = self.device()
        device.has_repeat = True
        device.saved_repeat = (250, 33)
        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)
        evdev.fcntl.ioctl = lambda fd, request, arg: 0
        self.assertIsNone(device.hush_repeat())
        self.assertEqual(device.saved_repeat, (250, 33))

    def test_a_device_with_no_repeat_is_not_even_asked(self):
        # A trackpad or a power button has no EV_REP; the ioctl would
        # fail, which is not a reason to make it.
        device, _ = self.device()
        device.has_repeat = False
        asked = []
        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)
        evdev.fcntl.ioctl = lambda fd, request, arg: asked.append(request)
        self.assertIsNone(device.hush_repeat())
        self.assertIsNone(device.saved_repeat)
        self.assertEqual(asked, [])

    def test_restoring_puts_back_what_was_recorded(self):
        device, _ = self.device()
        device.has_repeat = True
        device.saved_repeat = (600, 40)
        written = []
        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)
        evdev.fcntl.ioctl = (
            lambda fd, request, arg: written.append(struct.unpack("II", arg)))
        # It says what it put back, which is how the set knows there was
        # a debt to withdraw.
        self.assertEqual(device.restore_repeat(), (600, 40))
        self.assertEqual(written, [(600, 40)])
        self.assertIsNone(device.saved_repeat)

    def test_restoring_nothing_says_nothing(self):
        device, _ = self.device()
        device.has_repeat = True
        self.assertIsNone(device.restore_repeat())

    def test_closing_forgets_the_grab(self):
        device, _ = self.device()
        device.grabbed = True
        device.close()
        self.assertFalse(device.grabbed)

    def test_one_that_came_back_grabs_again_for_real(self):
        """The bookkeeping has to match what the kernel did.

        Closing drops the grab, so a device that came back still
        believing it held one would return early from grab(), never
        issue EVIOCGRAB, and forward nothing for the rest of the run.
        """
        device, _ = self.device()
        grabs = []
        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)
        evdev.fcntl.ioctl = lambda fd, request, arg: grabs.append(arg) or 0

        self.assertTrue(device.grab())
        self.assertEqual(grabs, [1])
        device.close()
        device.reopen()
        self.assertTrue(device.grab())
        self.assertEqual(grabs, [1, 1], "the second grab never happened")

    def test_what_it_learned_survives_the_round_trip(self):
        # Its name and keys belong to the device, not to the descriptor.
        device, _ = self.device()
        name, keybits = device.name, device.keybits
        device.close()
        device.reopen()
        self.assertEqual((device.name, device.keybits), (name, keybits))


class ClosedDeviceTest(unittest.TestCase):
    """What the rest of btkey may do to a device that is asleep.

    The phone can send an LED report at any moment, including while
    another console has the screen and the descriptors are gone.  Every
    one of these goes through fcntl.ioctl or os.write, which raise
    TypeError rather than OSError on a closed device, so none of them is
    covered by the try/except that is already there.
    """

    def device(self):
        device = evdev.InputDevice.__new__(evdev.InputDevice)
        device.fd = None
        device.has_leds = True
        device.writable = True
        device.grabbed = False
        device.grab_error = None
        device._buffer = b""
        return device

    def test_reading_its_leds_says_nothing_is_lit(self):
        self.assertEqual(self.device().leds(), 0)

    def test_driving_its_leds_reports_failure(self):
        self.assertFalse(self.device().set_leds(0x02))

    def test_asking_what_is_held_says_nothing(self):
        self.assertEqual(self.device().pressed_keys(), set())

    def test_reading_it_yields_nothing_rather_than_going_away(self):
        # None would have the session forget a device that is only asleep.
        self.assertEqual(self.device().read_keys(), [])

    def test_grabbing_it_fails_as_a_device_that_is_not_there(self):
        device = self.device()
        self.assertFalse(device.grab())
        self.assertEqual(device.grab_error, errno.ENODEV)

    def test_ungrabbing_it_is_not_an_error(self):
        device = self.device()
        device.grabbed = True
        device.ungrab()
        self.assertFalse(device.grabbed)


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

    def test_a_grabbed_keyboard_stops_repeating(self):
        """Autorepeat is thirty wakeups a second we throw away.

        A HID keyboard reports which keys are down and the host does the
        repeating, so every repeat the kernel makes here is read and
        dropped - for as long as a key is held, and holding a modifier
        is what VoiceOver's chords are made of.
        """
        device = RecordingDevice("/a")
        make_set(device).grab_all()
        self.assertEqual(device.repeat_writes, [(0, 0)])

    def test_it_is_put_back_when_the_keyboard_is_let_go(self):
        # The setting belongs to the device, not to our descriptor, so
        # the console would keep whatever we left it.
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.release_all()
        self.assertEqual(device.repeat_writes, [(0, 0), (250, 33)])

    def test_it_is_put_back_before_the_descriptor_goes(self):
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.close()
        self.assertLess(device.trace.index("repeat /a"),
                        device.trace.index("close /a"))

    def test_hushing_twice_does_not_save_the_hush(self):
        """The second snapshot would be the silence we just installed.

        Then putting it back would put back nothing, and the keyboard
        would stay dead for the rest of the session.
        """
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.grab_all()
        keyboards.release_all()
        self.assertEqual(device.repeat_writes, [(0, 0), (250, 33)])

    def test_one_that_would_not_come_is_left_alone(self):
        # Not ours to reconfigure.
        device = RecordingDevice("/a", grabbable=False)
        make_set(device).grab_all()
        self.assertEqual(device.repeat_writes, [])

    def test_the_undo_is_handed_over_when_it_is_hushed(self):
        """So that something can put it back if btkey never can.

        Killed while holding the keyboard, btkey would otherwise leave
        one that types a single character however long a key is held.
        """
        told = []
        device = RecordingDevice("/a")
        make_set(device, on_repeat_debt=lambda p, r: told.append((p, r))
                 ).grab_all()
        self.assertEqual(told, [("/a", (250, 33))])

    def test_it_is_handed_over_once(self):
        told = []
        keyboards = make_set(RecordingDevice("/a"),
                             on_repeat_debt=lambda p, r: told.append(p))
        keyboards.grab_all()
        keyboards.grab_all()
        self.assertEqual(told, ["/a"])

    def test_the_debt_is_withdrawn_when_we_put_it_back(self):
        """Leaving it standing would have the guardian undo the wrong thing.

        btkey hands the repeat back itself on the way to another
        console.  If the record stayed, and someone set that keyboard's
        repeat rate themselves meanwhile, a btkey killed while
        backgrounded would quietly put the old rate back over theirs.
        """
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append((p, r)))
        keyboards.grab_all()
        keyboards.release_all()
        self.assertEqual(told, [("/a", (250, 33)), ("/a", None)])

    def test_a_keyboard_that_went_away_takes_its_debt_with_it(self):
        """The node has gone, so there is nothing to put back.

        And whatever takes that event number next is a different
        keyboard: setting this one's repeat on it would be somebody
        else's surprise, arriving only if btkey were killed.
        """
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append((p, r)))
        keyboards.grab_all()
        keyboards.forget(device)
        self.assertEqual(told, [("/a", (250, 33)), ("/a", None)])
        self.assertIsNone(device.saved_repeat)

    def test_one_dropped_while_still_there_gets_its_repeat_back(self):
        """A device is dropped on any read error, not only on going away.

        read_keys reports it lost for anything that is not EAGAIN, and a
        flaky USB keyboard can give EIO while still sitting there.  That
        one would otherwise keep the repeat btkey turned off, for good,
        with the guardian's undo withdrawn in the same breath.
        """
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append((p, r)))
        keyboards.grab_all()
        keyboards.forget(device)
        self.assertEqual(device.repeat_writes, [(0, 0), (250, 33)])
        self.assertEqual(told, [("/a", (250, 33)), ("/a", None)])

    def test_forgetting_one_that_was_never_hushed_says_nothing(self):
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append(p))
        keyboards.forget(device)
        self.assertEqual(told, [])

    def test_taking_it_again_records_the_debt_afresh(self):
        # The rate may have been changed while we were away, so what is
        # handed over has to be what is there now, not what was there.
        told = []
        device = RecordingDevice("/a")
        keyboards = make_set(device,
                             on_repeat_debt=lambda p, r: told.append(r))
        keyboards.grab_all()
        keyboards.release_all()
        keyboards.grab_all()
        self.assertEqual(told, [(250, 33), None, (250, 33)])

    def test_the_console_leds_are_snapshotted_when_we_take_it(self):
        """After the grab, and before anything writes them.

        What the saved state must not be is the phone's, which is why
        this has to happen before push_leds; the grab itself changes
        nothing, so it belongs on the far side of it with the rest of
        the taking.
        """
        device = RecordingDevice("/a", leds=0x04)
        make_set(device).grab_all()
        self.assertEqual(device.saved_leds, 0x04)
        self.assertLess(device.trace.index("grab /a"),
                        device.trace.index("read leds /a"))

    def test_the_snapshot_is_not_taken_twice(self):
        """The second one would be the phone's lock state, not the console's.

        grab_all runs on every switch back, and push_leds puts the
        phone's state onto the keyboards straight after it.  Snapshotting
        again would save that, and the console would be handed the
        phone's caps lock when btkey let go.
        """
        device = RecordingDevice("/a", leds=0x04)
        keyboards = make_set(device)
        keyboards.grab_all()
        device.set_leds(0x02)          # what push_leds does, from the phone
        keyboards.grab_all()
        self.assertEqual(device.saved_leds, 0x04)
        keyboards.release_all()
        self.assertEqual(device.led_writes[-1], 0x04)

    def test_one_that_would_not_come_is_left_entirely_alone(self):
        """Nothing is done to a keyboard that is somebody else's.

        It is closed and forgotten a moment later with none of this
        undone, so anything done here would be done for good: its LED
        state read for a restore that never happens, its key repeat
        turned off for the rest of the machine's uptime, and the
        guardian told to put back a setting on a device btkey never
        held.
        """
        told = []
        device = RecordingDevice("/a", grabbable=False)
        make_set(device,
                 on_repeat_debt=lambda p, r: told.append(p)).grab_all()
        # Tried, refused, and handed straight back: no LED read, no
        # repeat touched, nothing owed to the guardian.
        self.assertEqual(device.trace, ["grab /a", "close /a"])
        self.assertIsNone(device.saved_leds)
        self.assertIsNone(device.saved_repeat)
        self.assertEqual(told, [])

    def test_ungrab_all_gives_the_set_up(self):
        keyboards = make_set(RecordingDevice("/a"), RecordingDevice("/b"))
        keyboards.grab_all()
        keyboards.release_all()
        self.assertFalse(keyboards.grabbed)

    def test_closing_is_what_releases_the_keyboard(self):
        """No EVIOCGRAB(0): the close does it.

        A grab belongs to the open file description, so the kernel drops
        it when that goes.  Nothing can be holding a second reference:
        the guardian is forked before any device is opened, and
        subprocess closes descriptors it does not need.  SETUP.md already
        leans on this for the case where btkey dies without tidying up.
        """
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.close()
        self.assertFalse(device.grabbed)
        self.assertTrue(device.closed)
        self.assertNotIn("ungrab /a", device.trace)

    def test_ungrab_all_hands_the_console_leds_back(self):
        device = RecordingDevice("/a", leds=0x02)
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.set_leds(0x01)          # the phone's num lock
        keyboards.release_all()
        self.assertEqual(device.led_writes, [0x01, 0x02])
        self.assertIsNone(device.saved_leds)

    def test_the_leds_go_back_before_the_descriptor_does(self):
        # The LED write needs the descriptor, and once it is gone the
        # console owns the lights again and will drive them itself.
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.close()
        self.assertLess(device.trace.index("leds /a=0x02"),
                        device.trace.index("close /a"))

    def test_close_forgets_every_device(self):
        device = RecordingDevice("/a")
        keyboards = make_set(device)
        keyboards.grab_all()
        keyboards.close()
        self.assertEqual(device.trace[-1], "close /a")
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
        keyboards.release_all()
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
            keyboards.release_all()
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
            keyboards.release_all()
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
        keyboards.release_all()
        device.grabbable = True
        keyboards.grab_all()
        self.assertEqual(len(chatter), 2, chatter)
        self.assertIn("came free", chatter[1])

    def test_a_device_that_was_always_ours_is_never_mentioned(self):
        noted, chatter = [], []
        keyboards = make_set(RecordingDevice("/a"), on_event=noted.append,
                             on_debug=chatter.append)
        keyboards.grab_all()
        keyboards.release_all()
        keyboards.grab_all()
        self.assertEqual(noted, [])
        self.assertEqual(chatter, [])

    def test_closing_them_all_gives_every_descriptor_up(self):
        one, two = RecordingDevice("/a"), RecordingDevice("/b")
        keyboards = make_set(one, two)
        keyboards.release_all()
        self.assertTrue(one.closed and two.closed)

    def test_a_sleeping_set_closes_what_discovery_opens(self):
        """Discovery opens what it finds; a sleeping set wants none of it.

        An open device delivers everything typed on it, so one plugged in
        while another console has the screen would wake btkey for every
        keystroke meant for somebody else.  refresh() honours the set's
        posture here exactly as it honours the grab beside it.
        """
        arrival = RecordingDevice("/new")
        keyboards = make_set()
        keyboards.release_all()
        self.discovering(arrival)
        added, _ = keyboards.refresh()
        self.assertEqual(added, [arrival])
        self.assertTrue(arrival.closed)

    def test_a_waking_set_leaves_what_discovery_opens_open(self):
        arrival = RecordingDevice("/new")
        keyboards = make_set()
        self.discovering(arrival)
        keyboards.refresh()
        self.assertFalse(arrival.closed)

    def test_a_set_that_woke_up_leaves_it_open_too(self):
        # The posture has to be put back on the way in, or every keyboard
        # plugged in for the rest of the run is closed on discovery.
        arrival = RecordingDevice("/new")
        keyboards = make_set()
        keyboards.release_all()
        keyboards.open_all()
        self.discovering(arrival)
        keyboards.refresh()
        self.assertFalse(arrival.closed)

    def discovering(self, *devices):
        """Put known devices in discovery's way, for refresh() to find."""
        saved = evdev.discover

        def fake(extra_paths=(), known=()):
            # Everything that is there, held or new, is what refresh
            # reads to decide what has gone away.
            return (list(devices),
                    [device.path for device in list(known) + list(devices)])

        evdev.discover = fake
        self.addCleanup(setattr, evdev, "discover", saved)

    def test_opening_them_again_reports_the_ones_that_went(self):
        here, gone = RecordingDevice("/a"), RecordingDevice("/b")
        gone.gone = True
        keyboards = make_set(here, gone)
        keyboards.release_all()
        self.assertEqual(keyboards.open_all(), [gone])
        self.assertFalse(here.closed)

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
        self.assertLess(trace.index("report"), trace.index("close /a"))

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

    def test_leaving_the_foreground_gives_the_descriptors_up(self):
        """Ungrabbed is not the same as closed.

        An open device delivers everything typed on it whether or not it
        is grabbed, so a btkey sitting on another console wakes for every
        keystroke meant for somebody else and drops it on the floor.
        """
        device = RecordingDevice("/a")
        session = self.session(device)
        session.foreground = False
        session.set_foreground(True)
        self.assertEqual(list(session.watches), ["/a"])
        session.set_foreground(False)
        self.assertTrue(device.closed)
        self.assertEqual(session.watches, {})

    def test_coming_back_opens_them_again_and_watches_them(self):
        device = RecordingDevice("/a")
        session = self.session(device)
        session.foreground = False
        session.set_foreground(True)
        session.set_foreground(False)
        session.set_foreground(True)
        self.assertFalse(device.closed)
        self.assertEqual(list(session.watches), ["/a"])

    def test_the_leds_go_back_before_the_descriptor_does(self):
        # Handing the console its lock state back needs the device open.
        trace = []
        device = RecordingDevice("/a", trace=trace, leds=0x04)
        session = self.session(device)
        session.foreground = False
        session.set_foreground(True)
        session.set_foreground(False)
        self.assertLess(trace.index("leds /a=0x04"), trace.index("close /a"))

    def test_one_unplugged_while_we_were_away_is_dropped(self):
        device = RecordingDevice("/a")
        session = self.session(device)
        session.foreground = False
        session.set_foreground(True)
        session.set_foreground(False)
        device.gone = True
        session.set_foreground(True)
        self.assertEqual(session.keyboards.devices, {})
        self.assertEqual(session.watches, {})

    def test_watching_one_twice_keeps_one_watch(self):
        # Startup watches what discovery found, and going to the
        # foreground watches everything it has; the second must not leave
        # a source behind that nothing will ever remove.
        device = RecordingDevice("/a")
        session = self.session(device)
        session.watch_device(device)
        first = session.watches["/a"]
        session.watch_device(device)
        self.assertEqual(session.watches["/a"], first)

    def test_a_hotplugged_keyboard_is_grabbed_and_snapshotted(self):
        """What a rescan does to a keyboard that arrives mid-session.

        Written the other way round this grabbed the device and set its
        saved LEDs by hand, then asserted that those had happened; the
        only production code it reached was the restore.
        """
        device = RecordingDevice("/a", leds=0x04)
        keyboards = make_set()
        keyboards.grab_all()
        keyboards.devices["/a"] = device

        keyboards.grab_all()             # what the rescan runs afterwards
        self.assertTrue(device.grabbed)
        self.assertEqual(device.saved_leds, 0x04)

        keyboards.release_all()
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
