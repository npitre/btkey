# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the keycode -> HID report path with the VT and radio stubbed.

These are the parts that are easy to get subtly wrong and awkward to debug
against a real phone: modifier packing, rollover, and above all which chords
btkey swallows.  Swallowing Ctrl+Option+arrow would quietly break VoiceOver
navigation, so that has an explicit test.
"""

import argparse
import ast
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import btlink, evdev, kbmap, keycodes, probe, vt
from btkey import session as session_module
from btkey import typist as typist_module
from btkey.typist import INTERVAL_MS as TYPE_INTERVAL_MS

KEY_A, KEY_B, KEY_C, KEY_D, KEY_E, KEY_F, KEY_G = 30, 48, 46, 32, 18, 33, 34
KEY_LEFTSHIFT, KEY_LEFTCTRL, KEY_LEFTALT = 42, 29, 56
KEY_F2, KEY_F5, KEY_ESC, KEY_RIGHT, KEY_VOLUMEUP = 60, 63, 1, 106, 115
KEY_F3, KEY_F12 = 61, 88
KEY_CAPSLOCK, KEY_NUMLOCK = 58, 69


class FakeConsoles:
    def __init__(self):
        self.vt = 4
        self.switched = []
        self.watch_fd = None       # set to a descriptor to be watchable
        self.rearmed = 0

    def is_foreground(self): return True
    def close(self): pass
    def watch(self): return self.watch_fd

    def rearm(self):
        self.rearmed += 1
        return True

    def switch_to(self, target):
        self.switched.append(target)
        return True


class FakeDevice:
    """One keyboard, with a queue of events waiting to be read.

    A queue of None is a device that has gone away: read_keys() returning
    None is how evdev reports the node disappearing.
    """

    def __init__(self, path="/dev/input/event0"):
        self.path = path
        self.name = "fake keyboard"
        self.fd = -1
        self.queue = []
        self.saved_leds = None
        self.closed = False
        # Held, unless a test says otherwise: btkey does not forward from
        # a keyboard it could not take.
        self.grabbed = True
        self.refused = False

    def read_keys(self):
        if self.queue is None:
            return None
        events, self.queue = self.queue, []
        return events

    def pressed_keys(self):
        return set()

    def close(self):
        self.closed = True


class FakeKeyboards:
    """Stands in for the set of grabbed evdev keyboards."""

    def __init__(self, extra_paths=(), on_event=None, on_debug=None,
                 on_repeat_debt=None):
        self.devices = {}
        self.held = set()          # what is physically down right now
        self.leds = None
        self.restored = 0
        self.grabbed = False
        self.opened = True
        self.asleep = False

    def held_keys(self): return set(self.held)
    was_held = False
    def hush_repeat(self): return None
    def restore_repeat(self): pass
    def grab_all(self): self.grabbed = True
    def ungrab_all(self): self.grabbed = False
    def refresh(self): return [], []
    def close(self): pass
    def release_all(self): self.opened = False; self.asleep = True
    def release(self, device): device.close()
    def open_all(self): self.opened = True; self.asleep = False; return []

    def forget(self, device):
        self.devices.pop(device.path, None)
        device.close()

    def set_leds(self, mask):
        self.leds = mask
        return ["fake keyboard"]

    def restore_leds(self):
        self.restored += 1


class FakeLink:
    # The real link counts these; the sweep timing reads them.
    sent_reports = 0
    send_seconds = 0.0
    # Taken from the real class rather than repeated, so a test cannot
    # agree with itself about which company identifier is Apple's.
    APPLE_VENDOR = btlink.BluetoothHID.APPLE_VENDOR

    def __init__(self, **kwargs):
        self.connected = True
        self.reports = []
        self.consumer = []
        self.passkeys = []
        self.abandoned = 0
        self.dialled = 0

    def send_keyboard(self, modifiers, keys):
        slots = list(keys[:6]) + [0] * (6 - len(keys[:6]))
        self.reports.append((modifiers, tuple(slots)))

    def send_consumer(self, usage): self.consumer.append(usage)
    def cancel_passkey_entry(self): pass
    def confirm(self, peer, passkey): return True
    def show_passkey(self, passkey): pass
    def supply_passkey(self, value): self.passkeys.append(value)
    def abandon_passkey(self): self.abandoned += 1
    def reconnect(self): self.dialled += 1
    def class_of_device(self): return getattr(self, "cod", 0x2C0540)
    def all_uuids(self): return getattr(self, "uuids", [])
    def last_host(self): return None
    def start(self): pass
    def stop(self): pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source_of(module):
    """The text of one btkey module, for the tests that read the code."""
    with open(os.path.join(ROOT, "btkey", module), encoding="utf-8") as handle:
        return handle.read()


def calls_in(module, function):
    """Every call in one function, as source text.

    Used to check that a mechanism is wired into startup at all, which
    is a kind of mistake no amount of testing the mechanism itself will
    catch.
    """
    tree = ast.parse(source_of(module))
    wanted = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == function)
    return [ast.unparse(node) for node in ast.walk(wanted)
            if isinstance(node, ast.Call)]


def capture_timers(call, scheduler="timeout_add"):
    """Run something with a GLib scheduler stubbed out.

    Returns a (delay, callback, arguments) triple per timer armed.  The
    scheduler is named because the two differ in their unit, and a test
    that watched the wrong one would see nothing and say so.
    """
    from btkey import session as session_module
    timers = []
    real = getattr(session_module.GLib, scheduler)
    setattr(session_module.GLib, scheduler,
            lambda delay, fn, *args: timers.append((delay, fn, args)) or 1)
    try:
        call()
    finally:
        setattr(session_module.GLib, scheduler, real)
    return timers


def make_session(keyboards=FakeKeyboards, keeper=None, **overrides):
    """Build a Session on fakes.

    A Session reaches for its keyboards and its link through the module
    globals, so the fakes have to be in place while it is constructed.
    They are put back straight afterwards: the session keeps the
    instances, and a later test that wants the real classes - to check
    what actually goes on the wire - gets them.
    """
    from btkey.session import Session
    options = argparse.Namespace(name="btkey", adapter=None, vt=None,
                                 device=[], list_devices=False,
                                 no_reconnect=False,
                                 pairing="keyboard", debug=False,
                                 audio=True, top_row="function",
                                 log_file=None, device_class=0x000540,
                                 shift_newline=True, control_fifo=None,
                                 phone_layout=None,
                                 system_bluetoothd=True)
    for name, value in overrides.items():
        setattr(options, name, value)

    saved = vt.Consoles, btlink.BluetoothHID, evdev.KeyboardSet
    vt.Consoles, btlink.BluetoothHID, evdev.KeyboardSet = (
        FakeConsoles, FakeLink, keyboards)
    try:
        session = Session(options, FakeConsoles(), keeper)
    finally:
        vt.Consoles, btlink.BluetoothHID, evdev.KeyboardSet = saved
    session.foreground = True
    return session


def press(session, *keycodes_):
    for code in keycodes_:
        session.handle_key(code, True)


def release(session, *keycodes_):
    for code in keycodes_:
        session.handle_key(code, False)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.link = self.session.link

    def test_plain_key(self):
        press(self.session, KEY_A)
        release(self.session, KEY_A)
        self.assertEqual(self.link.reports,
                         [(0, (0x04, 0, 0, 0, 0, 0)),
                          (0, (0, 0, 0, 0, 0, 0))])

    def test_shifted_key(self):
        press(self.session, KEY_LEFTSHIFT, KEY_A)
        self.assertEqual(self.link.reports[-1],
                         (keycodes.MOD_LEFTSHIFT, (0x04, 0, 0, 0, 0, 0)))

    def test_rollover(self):
        press(self.session, KEY_A, KEY_B, KEY_C, KEY_D, KEY_E, KEY_F)
        self.assertEqual(len([k for k in self.link.reports[-1][1] if k]), 6)
        press(self.session, KEY_G)
        self.assertEqual(self.link.reports[-1], (0, (1, 1, 1, 1, 1, 1)))

    def test_consumer_key(self):
        press(self.session, KEY_VOLUMEUP)
        release(self.session, KEY_VOLUMEUP)
        self.assertEqual(self.link.consumer, [0x00E9, 0])
        self.assertEqual(self.link.reports, [])

    def test_dials_out_when_disconnected(self):
        self.link.connected = False
        press(self.session, KEY_A)
        self.assertEqual(self.link.dialled, 1)
        self.assertEqual(self.link.reports, [])


class ChordTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.link = self.session.link

    def test_alt_fn_switches_vt_and_releases_keys(self):
        press(self.session, KEY_LEFTALT, KEY_F2)
        self.assertEqual(self.session.consoles.switched, [2])
        # Alt must not be left held down on the phone.
        self.assertEqual(self.link.reports[-1], (0, (0, 0, 0, 0, 0, 0)))
        self.assertEqual(self.session.modifiers, 0)
        # F2 itself is never forwarded.
        self.assertNotIn(0x3B, [key for _, keys in self.link.reports
                                for key in keys])

    def test_alt_esc_quits(self):
        press(self.session, KEY_LEFTALT, KEY_ESC)
        self.assertTrue(self.session.quit_requested)

    def test_escape_alone_passes_through(self):
        press(self.session, KEY_ESC)
        self.assertFalse(self.session.quit_requested)
        self.assertEqual(self.link.reports[-1], (0, (0x29, 0, 0, 0, 0, 0)))

    def test_ctrl_escape_passes_through(self):
        press(self.session, KEY_LEFTCTRL, KEY_ESC)
        self.assertFalse(self.session.quit_requested)
        self.assertEqual(self.link.reports[-1][1][0], 0x29)

    def test_the_voiceover_modifier_with_escape_reaches_the_phone(self):
        """Ctrl+Option is VoiceOver's, and this used to be the quit chord.

        Quitting is Alt+Escape now, so Ctrl+Option+Escape goes to the phone
        with the rest of that family rather than being taken from it.
        """
        press(self.session, KEY_LEFTCTRL, KEY_LEFTALT, KEY_ESC)
        self.assertFalse(self.session.quit_requested)
        self.assertEqual(self.link.reports[-1][1][0], 0x29)

    def test_ctrl_alt_fn_still_switches_console(self):
        """The chord every Linux console has used forever; Ctrl or not."""
        press(self.session, KEY_LEFTCTRL, KEY_LEFTALT, KEY_F2)
        self.assertEqual(self.session.consoles.switched, [2])

    def test_voiceover_chord_passes_through(self):
        """Ctrl+Option+Right is VoiceOver's "next item" - must not be eaten."""
        press(self.session, KEY_LEFTCTRL, KEY_LEFTALT, KEY_RIGHT)
        self.assertEqual(self.session.consoles.switched, [])
        self.assertEqual(
            self.link.reports[-1],
            (keycodes.MOD_LEFTCTRL | keycodes.MOD_LEFTALT,
             (0x4F, 0, 0, 0, 0, 0)))

    def test_plain_function_key_passes_through(self):
        press(self.session, KEY_F2)
        self.assertEqual(self.session.consoles.switched, [])
        self.assertEqual(self.link.reports[-1], (0, (0x3B, 0, 0, 0, 0, 0)))


class TeardownKeyboards:
    """Minimal stand-in for the teardown tests: one device, nothing else."""

    def __init__(self):
        self.devices = {"/dev/input/eventX": object()}
        self.closed = False

    def refresh(self): return [], []
    def grab_all(self): pass
    def ungrab_all(self): pass
    def close(self): self.closed = True


class TeardownTest(unittest.TestCase):
    """A failed start must still hand bluetoothd back.

    This is the path that matters most on a machine with one Bluetooth
    controller: btkey stops the system bluetooth.service before it does
    anything else, so any failure after that point which skips teardown
    leaves the machine with no Bluetooth at all.
    """

    def setUp(self):
        self.session = make_session()
        self.session.keyboards = TeardownKeyboards()
        self.stopped = []
        self.session.btd = type("FakeBtd", (), {
            "stop": lambda _self: self.stopped.append("btd")})()
        self.session.link.stop = lambda: self.stopped.append("link")

    def test_expected_failure_is_reported_and_cleaned_up(self):
        def boom():
            raise btlink.ProfileNotAvailable("PSM 17 already bound")
        self.session.start_services = boom
        self.assertEqual(self.session.run(), 1)
        self.assertEqual(self.stopped, ["link", "btd"])

    def test_unexpected_failure_still_cleans_up_before_propagating(self):
        def boom():
            raise RuntimeError("something nobody predicted")
        self.session.start_services = boom
        with self.assertRaises(RuntimeError):
            self.session.run()
        self.assertEqual(self.stopped, ["link", "btd"])

    def test_a_broken_teardown_step_does_not_block_the_next(self):
        def explode():
            raise OSError("link teardown is itself broken")
        self.session.link.stop = explode
        self.session.start_services = lambda: (_ for _ in ()).throw(
            btlink.ProfileNotAvailable("nope"))
        self.assertEqual(self.session.run(), 1)
        self.assertEqual(self.stopped, ["btd"])


class UnsendableKeyTest(unittest.TestCase):
    """Naming a key btkey decoded but has no HID usage for.

    A key that never arrives at the phone looks exactly like a key that
    was never pressed, so the one line saying which it was has to name
    something a person can act on.  Every key keycodes.NAMES names is one
    btkey can send, so this path only ever sees the unnamed ones, and it
    used to report every one of them as "?".
    """

    def unsendable(self, keycode):
        session = make_session()
        logged = []
        session.typist.log = logged.append
        session.link.connected = True
        # Stand in for the escape decoder, so the test does not depend on
        # which sequence a terminal spells this key with.
        self.addCleanup(setattr, typist_module.escapes, "decode",
                        typist_module.escapes.decode)
        typist_module.escapes.decode = (
            lambda text: ([("steps", ((keycode, 0),))], ""))
        session.typist.type_text("x")
        return " ".join(logged)

    def test_it_is_reported_by_number_rather_than_a_question_mark(self):
        said = self.unsendable(700)
        self.assertIn("nothing to send for", said)
        self.assertIn("700", said)
        self.assertNotIn("?", said)

    def test_a_key_that_can_be_sent_is_not_complained_about(self):
        # 102 is Home, which keycodes.KEYBOARD has, so it goes to the
        # phone and nothing is said about it.
        self.assertEqual(self.unsendable(102), "")


class TypingTest(unittest.TestCase):
    """Pasted text becomes key positions, since BRLTTY delivers characters.

    BRLTTY pastes with TIOCSTI, which never touches the input layer, so
    there are no keycodes to forward and btkey has to work out which key
    would have produced each character.  Each entry is a sequence, because
    an accented character may need a dead key before the base letter.
    """

    def setUp(self):
        self.session = make_session()
        self.link = self.session.link
        self.session.typist.keymap = {
            "a": ((30, 0),),
            "A": ((30, keycodes.MOD_LEFTSHIFT),),
            "é": ((18, keycodes.MOD_RIGHTALT),),   # third level, as on cf
            "è": ((40, 0), (18, 0)),               # dead grave, then e
            "\n": ((28, keycodes.MOD_LEFTSHIFT),),   # pasted, so Shift+Enter
        }

    def drain(self):
        while self.session.typist.drain():
            pass

    def test_plain_character(self):
        self.session.typist.type_text("a")
        self.drain()
        self.assertEqual(self.link.reports[:2],
                         [(0, (0x04, 0, 0, 0, 0, 0)),
                          (0, (0, 0, 0, 0, 0, 0))])

    def test_modifiers_come_from_the_keymap(self):
        self.session.typist.type_text("Aé")
        self.drain()
        self.assertEqual(self.link.reports[0],
                         (keycodes.MOD_LEFTSHIFT, (0x04, 0, 0, 0, 0, 0)))
        self.assertEqual(self.link.reports[2],
                         (keycodes.MOD_RIGHTALT, (0x08, 0, 0, 0, 0, 0)))

    def test_a_pasted_newline_is_shift_enter(self):
        """Plain Enter would send the message in a chat app mid-paste."""
        self.session.typist.type_text("\n")
        self.drain()
        self.assertEqual(self.link.reports[0],
                         (keycodes.MOD_LEFTSHIFT, (0x28, 0, 0, 0, 0, 0)))

    def test_unmapped_character_is_skipped_not_fatal(self):
        self.session.typist.type_text("aßa")
        self.drain()
        typed = [report for report in self.link.reports if report[1][0]]
        self.assertEqual(len(typed), 2)

    def test_nothing_is_queued_while_disconnected(self):
        self.link.connected = False
        self.session.typist.type_text("hello")
        self.assertEqual(len(self.session.typist.queue), 0)

    def test_dead_key_composition_types_both_keystrokes(self):
        """è is dead-grave then e, rolled straight from one to the other."""
        self.session.typist.type_text("è")
        self.drain()
        self.assertEqual(self.link.reports[:2],
                         [(0, (0x34, 0, 0, 0, 0, 0)),    # dead grave key
                          (0, (0x08, 0, 0, 0, 0, 0))])   # e

    def test_physical_key_state_is_restored_after_a_paste(self):
        """A paste borrows the link; held keys must survive it."""
        press(self.session, KEY_LEFTSHIFT)
        self.session.typist.type_text("a")
        self.drain()
        self.assertEqual(self.link.reports[-1],
                         (keycodes.MOD_LEFTSHIFT, (0, 0, 0, 0, 0, 0)))


class StrokeCoalescingTest(unittest.TestCase):
    """One report per character where that is safe, two where it is not.

    A HID keyboard reports its whole state each time, so rolling from one
    key to the next in a single report is what a real one does.  Sending
    an empty report between every character doubles the traffic and holds
    no key any less long.
    """

    def setUp(self):
        self.session = make_session()
        self.typist = self.session.typist

    def strokes(self, *pairs):
        self.typist.queue.clear()
        for modifiers, usage in pairs:
            self.typist._stroke(modifiers, usage)
        return list(self.typist.queue)

    def test_different_keys_roll_straight_on(self):
        self.assertEqual(self.strokes((0, 0x04), (0, 0x05)),
                         [(0, [0x04]), (0, [0x05])])

    def test_the_same_key_twice_needs_a_gap(self):
        """Without one nothing in the report changes, so the host sees a
        held key rather than a second press."""
        self.assertEqual(self.strokes((0, 0x04), (0, 0x04)),
                         [(0, [0x04]), (0, []), (0, [0x04])])

    def test_a_run_of_capitals_holds_shift_down(self):
        shift = keycodes.MOD_LEFTSHIFT
        reports = self.strokes((shift, 0x04), (shift, 0x05), (shift, 0x06))
        self.assertEqual(reports,
                         [(shift, [0x04]), (shift, [0x05]), (shift, [0x06])])
        self.assertTrue(all(mods == shift for mods, _ in reports))

    def test_a_repeat_within_a_run_of_capitals_keeps_shift(self):
        shift = keycodes.MOD_LEFTSHIFT
        self.assertEqual(self.strokes((shift, 0x04), (shift, 0x04)),
                         [(shift, [0x04]), (shift, []), (shift, [0x04])])

    def test_a_change_of_modifiers_gets_a_gap(self):
        """Legal to do in one report, but hosts differ on whether the old
        or the new modifiers apply to the key, and a wrong character is
        worse than a slow one."""
        shift = keycodes.MOD_LEFTSHIFT
        self.assertEqual(self.strokes((shift, 0x04), (0, 0x05)),
                         [(shift, [0x04]), (0, []), (0, [0x05])])

    def test_the_gap_keeps_what_the_two_have_in_common(self):
        shift, altgr = keycodes.MOD_LEFTSHIFT, keycodes.MOD_RIGHTALT
        reports = self.strokes((shift | altgr, 0x04), (shift, 0x05))
        self.assertEqual(reports[1], (shift, []))

    def test_the_probe_is_left_uncoalesced(self):
        """It measures the host, so each key should be presented plainly
        rather than as fast as possible."""
        self.typist.queue.clear()
        self.typist.enqueue([(0, 0x04), (0, 0x05)])
        self.assertEqual(list(self.typist.queue),
                         [(0, [0x04]), (0, []), (0, [0x05]), (0, [])])


class PasskeyTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.link = self.session.link

    def test_six_digits_submit_without_enter(self):
        """Against the phone's clock, Enter is one step too many."""
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        for keycode in (3, 4, 5, 6, 7, 8):          # 2 3 4 5 6 7
            press(self.session, keycode)
        self.assertEqual(self.link.passkeys, [234567])
        self.assertIsNone(self.session.pairing.digits)

    def test_enter_submits_a_short_passkey(self):
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        press(self.session, 3, 4, 5)                 # 2 3 4
        press(self.session, 28)                      # Enter
        self.assertEqual(self.link.passkeys, [234])

    def test_quit_chord_still_works_during_passkey_entry(self):
        """A pairing that never completes must not trap the keyboard."""
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        press(self.session, KEY_LEFTALT, KEY_ESC)
        self.assertTrue(self.session.quit_requested)

    def test_vt_switch_still_works_during_passkey_entry(self):
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        press(self.session, KEY_LEFTALT, KEY_F2)
        self.assertEqual(self.session.consoles.switched, [2])

    def test_numeric_comparison_needs_no_typing(self):
        """--pairing confirm: accept without entering passkey mode at all."""
        self.assertTrue(self.session.pairing.on_confirm("AA:BB:CC:DD:EE:FF", 123456))
        self.assertIsNone(self.session.pairing.digits)

    def test_cancellation_is_announced_and_clears_the_mode(self):
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        self.session.pairing.on_cancelled()
        self.assertIsNone(self.session.pairing.digits)

    def test_leading_zero_yields_the_numeric_value(self):
        # BlueZ takes a uint32 and renders it back as %06u, so a passkey
        # displayed as 023456 is the number 23456.
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        for keycode in (11, 3, 4, 5, 6, 7):          # 0 2 3 4 5 6
            press(self.session, keycode)
        self.assertEqual(self.link.passkeys, [23456])

    def test_backspace_and_escape(self):
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        press(self.session, 3, 4, 14)                # 2 3 Backspace
        self.assertEqual(self.session.pairing.digits, "2")
        press(self.session, 1)                       # Esc
        self.assertIsNone(self.session.pairing.digits)
        self.assertEqual(self.link.passkeys, [])

    def test_keys_are_not_forwarded_during_entry(self):
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        press(self.session, KEY_A)
        self.assertEqual(self.link.reports, [])


class ForegroundTest(unittest.TestCase):
    """Coming back to btkey's console with a key still down.

    A grab only shows transitions, so anything pressed while btkey was
    ungrabbed went to the kernel and never to it.  btkey answers that by
    not taking a keyboard until the keys are up, which is also what
    keeps the console from being left with a modifier stuck.
    """

    def setUp(self):
        self.session = make_session()
        self.link = self.session.link
        self.keyboards = self.session.keyboards

    def come_back(self, held):
        """Switch away and back, with `held` still down on return."""
        self.session.set_foreground(False)
        self.keyboards.held = set(held)
        self.session.set_foreground(True)

    def key_comes_up(self):
        """The release arriving on a keyboard btkey has not taken."""
        self.keyboards.held = set()
        waited = FakeDevice()
        waited.grabbed = False          # which is what waiting on it means
        self.session.on_device_input(waited.fd, None, waited)

    def test_nothing_is_taken_while_a_key_is_down(self):
        self.come_back({KEY_LEFTALT})
        self.assertTrue(self.session.waiting_for_release)
        self.assertFalse(self.keyboards.grabbed)

    def test_nothing_held_is_taken_at_once(self):
        self.come_back(set())
        self.assertFalse(self.session.waiting_for_release)
        self.assertTrue(self.keyboards.grabbed)
        self.assertEqual(self.session.modifiers, 0)

    def test_the_key_waited_for_is_not_left_held(self):
        """The reported bug: come back holding Alt, and Alt sticks.

        Reading what is held belongs to the moment of taking the
        keyboard.  Done at the switch instead, it adopted the very key
        being waited on - Alt, nearly always, that being how the console
        is reached - and every letter afterwards went to the phone as
        Alt and the letter, until Alt was pressed and released again.
        """
        self.come_back({KEY_LEFTALT})
        # What the bug left behind: Alt adopted at the switch, while it
        # was still down and still the console's.  Whatever btkey thinks
        # is held, taking the keyboard asks the keyboards afresh.
        self.session.modifiers = keycodes.MOD_LEFTALT
        self.key_comes_up()
        self.assertTrue(self.keyboards.grabbed)
        self.assertEqual(self.session.modifiers, 0)

    def test_the_lock_lights_are_written_once_it_is_taken(self):
        # push_leds ran at the switch too, when nothing was grabbed yet,
        # so it wrote to nothing and the deferred take never wrote at all.
        self.session.leds = 0x02
        self.come_back({KEY_LEFTALT})
        self.keyboards.leds = None
        self.key_comes_up()
        self.assertEqual(self.keyboards.leds, 0x02)

    def test_a_modifier_down_at_the_grab_is_still_adopted(self):
        """The breath between the check and the grab.

        Vanishingly unlikely, but asking is how it stays known about
        rather than stuck, and it costs one ioctl per keyboard.
        """
        self.keyboards.held = {KEY_LEFTALT}
        self.session.sync_modifiers()
        self.assertEqual(self.session.modifiers, keycodes.MOD_LEFTALT)

    def test_a_held_letter_is_not_a_modifier(self):
        self.keyboards.held = {KEY_A}
        self.session.sync_modifiers()
        self.assertEqual(self.session.modifiers, 0)


class InferredLockTest(unittest.TestCase):
    """iOS never sends an LED report, so the lock state has to be inferred.

    A lock only changes when its key is pressed, and every one of those
    passes through btkey, so following them is enough - the one thing it
    cannot know is a lock the phone was already holding when we connected.
    """

    def setUp(self):
        self.session = make_session()
        self.session.on_connection_state(True, "AA:BB:CC:DD:EE:FF")

    def test_caps_lock_raises_the_indicator(self):
        press(self.session, KEY_CAPSLOCK)
        self.assertEqual(self.session.display.indicator, "CAPS")

    def test_pressing_it_again_clears_it(self):
        press(self.session, KEY_CAPSLOCK)
        press(self.session, KEY_CAPSLOCK)
        self.assertEqual(self.session.display.indicator, "")

    def test_locks_are_independent(self):
        press(self.session, KEY_CAPSLOCK, KEY_NUMLOCK)
        self.assertEqual(self.session.display.indicator, "NUM CAPS")

    def test_ordinary_keys_change_nothing(self):
        press(self.session, KEY_CAPSLOCK)
        press(self.session, KEY_A, KEY_LEFTSHIFT, KEY_F5)
        self.assertEqual(self.session.display.indicator, "CAPS")

    def test_release_does_not_toggle_back(self):
        press(self.session, KEY_CAPSLOCK)
        release(self.session, KEY_CAPSLOCK)
        self.assertEqual(self.session.display.indicator, "CAPS")

    def test_nothing_is_inferred_while_disconnected(self):
        self.session.link.connected = False
        press(self.session, KEY_CAPSLOCK)
        self.assertEqual(self.session.display.indicator, "")

    def test_a_real_host_report_takes_over(self):
        press(self.session, KEY_CAPSLOCK)
        self.session.on_host_leds(0x01)
        self.assertTrue(self.session.leds_from_host)
        self.assertEqual(self.session.display.indicator, "NUM")

    def test_inference_stops_once_the_host_has_spoken(self):
        self.session.on_host_leds(0x01)
        press(self.session, KEY_CAPSLOCK)
        self.assertEqual(self.session.display.indicator, "NUM")

    def test_a_new_link_starts_from_off(self):
        """A reconnected phone's lock state is unknown, not what it was."""
        press(self.session, KEY_CAPSLOCK)
        self.session.on_connection_state(False, "AA:BB:CC:DD:EE:FF")
        self.session.on_connection_state(True, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.session.display.indicator, "")
        self.assertEqual(self.session.leds, 0)


class AdvertisedChangeTest(unittest.TestCase):
    """Noticing that what we advertise has moved since the last run.

    iOS caches a device's class and profile set at bond time, so a change
    is invisible to an already-paired phone until it is forgotten and
    re-paired.  Both times that happened it looked exactly like a fix that
    had not worked, which is worth one line of warning to avoid.
    """

    def setUp(self):
        import tempfile, shutil
        from btkey import advertising
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.original = advertising.STATE_FILE
        advertising.STATE_FILE = os.path.join(self.directory, "adv")
        self.addCleanup(setattr, advertising, "STATE_FILE", self.original)
        self.session = make_session()
        self.announced = []
        self.session.advertising.announce = self.announced.append

    def advertise(self, cod, uuids):
        self.session.link.cod = cod
        self.session.link.uuids = uuids
        self.session.advertising.check_advertised()

    def test_first_run_says_nothing(self):
        self.advertise(0x2C0540, ["0000110b", "00001124"])
        self.assertEqual(self.announced, [])

    def test_an_unchanged_set_says_nothing(self):
        self.advertise(0x2C0540, ["0000110b", "00001124"])
        self.advertise(0x2C0540, ["0000110b", "00001124"])
        self.assertEqual(self.announced, [])

    def test_order_alone_is_not_a_change(self):
        self.advertise(0x2C0540, ["0000110b", "00001124"])
        self.advertise(0x2C0540, ["00001124", "0000110b"])
        self.assertEqual(self.announced, [])

    def test_a_dropped_profile_is_a_change(self):
        """What dropping a2dp_source looked like, class held constant."""
        self.advertise(0x2C0540, ["0000110a", "0000110b", "00001124"])
        self.advertise(0x2C0540, ["0000110b", "00001124"])
        self.assertEqual(len(self.announced), 1)
        self.assertIn("re-pair", self.announced[0])

    def test_a_class_change_is_a_change(self):
        self.advertise(0x0C0104, ["0000110b"])
        self.advertise(0x2C0540, ["0000110b"])
        self.assertEqual(len(self.announced), 1)

    def test_it_warns_once_not_every_run(self):
        self.advertise(0x2C0540, ["0000110a", "0000110b"])
        self.advertise(0x2C0540, ["0000110b"])
        self.advertise(0x2C0540, ["0000110b"])
        self.assertEqual(len(self.announced), 1)


class SweepTest(unittest.TestCase):
    """The layout probe, which has to be self-labelling to be readable.

    A combination that produces nothing must leave a visibly empty result
    rather than shifting every later line out of alignment - which is the
    whole reason the label is typed rather than the order being trusted.
    """

    def setUp(self):
        self.session = make_session()
        self.session.typist.keymap = {
            char: ((code, 0),) for char, code in
            [("=", 13), (" ", 57), ("\n", 28), (".", 52), ("o", 24),
             ("s", 31), ("p", 25)]}
        for digit, code in zip("0123456789", [11, 2, 3, 4, 5, 6, 7, 8, 9, 10]):
            self.session.typist.keymap[digit] = ((code, 0),)

    def test_it_covers_every_row_at_every_level(self):
        positions = sum(len(row) for row in probe.ROWS)
        # Each key costs four strokes; each row and each sentinel a few more.
        self.assertGreater(len(probe.capture_strokes()), positions * 4)

    def test_the_option_level_uses_right_alt(self):
        """iOS maps both Options, but AltGr is the level being probed."""
        levels = dict((name, mods) for name, mods in probe.LEVELS)
        self.assertEqual(levels["L3"], keycodes.MOD_RIGHTALT)
        self.assertEqual(levels["L4"],
                         keycodes.MOD_RIGHTALT | keycodes.MOD_LEFTSHIFT)

    def test_nothing_in_the_capture_is_typed_as_text(self):
        """Text would go through the console keymap, which is the mapping
        the probe exists to check.  Every stroke is a position."""
        usages = {usage for _, usage in probe.capture_strokes()}
        self.assertTrue(usages <= set(keycodes.KEYBOARD.values()))

    def test_positions_have_no_duplicates(self):
        flat = [k for row in probe.ROWS for k in row]
        self.assertEqual(len(flat), len(set(flat)))

    def test_every_position_has_a_hid_usage(self):
        for row in probe.ROWS:
            for keycode in row:
                self.assertIn(keycode, keycodes.KEYBOARD,
                              "keycode %d" % keycode)

    def test_nothing_is_typed_while_disconnected(self):
        self.session.link.connected = False
        self.session.sweep.learn_layout()
        self.assertEqual(len(self.session.typist.queue), 0)

    def test_a_probe_is_enqueued_with_its_raw_modifiers(self):
        """enqueue bypasses the keymap; that is the point of the sweep."""
        self.session.typist.enqueue([(keycodes.MOD_RIGHTALT, 0x08)])
        self.assertEqual(list(self.session.typist.queue),
                         [(keycodes.MOD_RIGHTALT, [0x08]), (0, [])])


class SweepProgressTest(unittest.TestCase):
    """A sweep says how far it has got, and says when it is done.

    The instruction during a sweep is not to touch the keyboard for a
    minute, so an end signal is not a nicety: without one there is nothing
    to do but guess.  The bell carries that, since BRLTTY monitors it.
    """

    def setUp(self):
        self.session = make_session()
        self.bells, self.announced = [], []
        self.session.display.bell = lambda: self.bells.append(1)
        # Handed over when the sweep was built, so replace it there.
        self.session.sweep.announce = self.announced.append

    def batch(self, count=3):
        self.session.sweep.start("test sweep",
                                [(0, 0x04), (0, 0x2C)] * count)

    def test_progress_appears_in_the_indicator(self):
        self.batch()
        self.session.sweep.poll()
        self.assertRegex(self.session.display.shown_indicator, r"^\d+%$")

    def test_it_finishes_when_the_queue_drains(self):
        self.batch()
        while self.session.typist.drain():
            pass
        self.assertFalse(self.session.sweep.poll())
        self.assertIsNone(self.session.sweep.name)

    def test_finishing_rings_the_bell(self):
        self.batch()
        while self.session.typist.drain():
            pass
        self.session.sweep.poll()
        self.assertEqual(len(self.bells), 1)

    def test_the_lock_indicator_comes_back_afterwards(self):
        """The percentage borrows the lock indicator's slot and gives it back.

        The sweep does not put the lock state back itself, which would
        mean knowing what was in the slot; it hands the slot over and the
        display remembers.
        """
        self.session.apply_leds(0x02)
        self.batch()
        self.session.sweep.poll()
        while self.session.typist.drain():
            pass
        self.session.sweep.poll()
        self.assertEqual(self.session.display.shown_indicator, "CAPS")

    def test_a_lock_key_pressed_mid_sweep_is_not_lost(self):
        """The standing indicator keeps changing underneath the borrow.

        Caps Lock during a probe still has to be there when the slot is
        handed back, or the display goes on showing a state the phone
        left behind a minute ago.
        """
        self.batch()
        self.session.sweep.poll()
        self.session.apply_leds(0x02)            # Caps, mid-probe
        self.assertRegex(self.session.display.shown_indicator, r"^\d+%$")
        while self.session.typist.drain():
            pass
        self.session.sweep.poll()
        self.assertEqual(self.session.display.shown_indicator, "CAPS")

    def test_a_disconnect_mid_sweep_is_not_completion(self):
        """drain() empties the queue when the phone goes.

        An empty queue is how completion is recognised, so without this
        a probe cut short reads as done, bell and all, and sends someone
        off to mail a capture that stops halfway.
        """
        self.batch(count=50)
        self.session.sweep.poll()
        self.session.link.connected = False
        self.assertFalse(self.session.sweep.poll())
        self.assertFalse(self.session.sweep.running)
        self.assertIn("disconnected", "\n".join(self.announced))

    def test_cancel_abandons_the_queue(self):
        self.batch(count=50)
        self.session.sweep.cancel()
        self.assertEqual(len(self.session.typist.queue), 0)
        self.assertIsNone(self.session.sweep.name)
        self.assertEqual(len(self.bells), 1)

    def test_cancel_with_nothing_running_is_harmless(self):
        self.session.sweep.cancel()
        self.assertEqual(self.bells, [])

    def test_polling_with_no_sweep_stops_the_timer(self):
        self.assertFalse(self.session.sweep.poll())


class LearnAccentsTest(unittest.TestCase):
    """The accent pass takes its targets from the client, not from a file.

    That is what removes the restart: the first capture is a file the
    client can read, so the running btkey never needs to be told about it.
    """

    def setUp(self):
        self.session = make_session()
    def test_it_probes_the_keys_it_was_given(self):
        self.session.sweep.learn_accents(["26:0", "26:2"])
        self.assertEqual(self.session.sweep.name, "learning accent keys")
        self.assertGreater(len(self.session.typist.queue), 0)

    def test_no_keys_means_nothing_is_typed(self):
        self.session.sweep.learn_accents([])
        self.assertIsNone(self.session.sweep.name)
        self.assertEqual(len(self.session.typist.queue), 0)

    def test_a_malformed_key_is_skipped_not_fatal(self):
        self.session.sweep.learn_accents(["26:0", "rubbish", "26:2"])
        self.assertEqual(self.session.sweep.name, "learning accent keys")

    def test_nothing_is_typed_while_disconnected(self):
        self.session.link.connected = False
        self.session.sweep.learn_accents(["26:0"])
        self.assertEqual(len(self.session.typist.queue), 0)


class PasskeyTeardownTest(unittest.TestCase):
    """RequestPasskey is an asynchronous D-Bus call.

    Returning from the method does not answer it, so every way of leaving
    passkey mode has to either supply a passkey or say nobody will - or
    BlueZ waits for its own timeout with the pairing half open.
    """

    def setUp(self):
        self.session = make_session()
        self.link = self.session.link
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)

    def test_escape_tells_bluez_nobody_is_answering(self):
        press(self.session, KEY_ESC)
        self.assertEqual(self.link.abandoned, 1)

    def test_a_timeout_tells_bluez_too(self):
        self.session.pairing.abandon()
        self.assertEqual(self.link.abandoned, 1)

    def test_submitting_does_not_also_abandon(self):
        for keycode in (3, 4, 5, 6, 7, 8):
            press(self.session, keycode)
        self.assertEqual(self.link.passkeys, [234567])
        self.assertEqual(self.link.abandoned, 0)

    def test_the_phone_cancelling_does_not_answer_twice(self):
        """Agent.Cancel has already answered the call."""
        self.session.pairing.on_cancelled()
        self.assertEqual(self.link.abandoned, 0)

    def test_a_second_request_does_not_leave_the_first_timeout(self):
        first = self.session.pairing.timeout
        self.session.pairing.on_request("AA:BB:CC:DD:EE:FF", False)
        self.assertNotEqual(self.session.pairing.timeout, first)
        self.assertTrue(self.session.pairing.active)


class ReleaseTest(unittest.TestCase):
    """Nothing may be left held down on the phone."""

    def setUp(self):
        self.session = make_session()
        self.link = self.session.link

    def test_a_consumer_key_is_released_too(self):
        """It is a separate report, and stays held until its own zero."""
        press(self.session, KEY_VOLUMEUP)
        self.link.consumer.clear()
        self.session.release_all()
        self.assertEqual(self.link.consumer, [0])

    def test_cancelling_a_paste_hands_the_key_back(self):
        """clear() can land between a press report and its release."""
        self.session.typist.enqueue([(0, 0x04)])
        self.session.typist.drain()          # the press has gone out
        self.link.reports.clear()
        self.session.typist.clear()
        self.assertEqual(self.link.reports[-1], (0, (0, 0, 0, 0, 0, 0)))

    def test_clearing_an_empty_queue_says_nothing(self):
        self.link.reports.clear()
        self.session.typist.clear()
        self.assertEqual(self.link.reports, [])


class LedTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.keyboards = self.session.keyboards

    def test_host_leds_reach_the_keyboard(self):
        self.session.on_host_leds(0x02)          # Caps Lock
        self.assertEqual(self.keyboards.leds, 0x02)

    def test_disconnect_gives_the_console_its_leds_back(self):
        self.session.on_host_leds(0x02)
        self.session.on_connection_state(False, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.keyboards.restored, 1)

    def test_indicator_is_set_from_the_host_report(self):
        self.session.on_host_leds(0x02)
        self.assertEqual(self.session.display.indicator, "CAPS")

    def test_disconnect_clears_the_indicator(self):
        self.session.on_host_leds(0x02)
        self.session.on_connection_state(False, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.session.display.indicator, "")

    def test_led_indicator_shortens_the_names(self):
        self.assertEqual(btlink.led_indicator(0x02), "CAPS")
        self.assertEqual(btlink.led_indicator(0x03), "NUM CAPS")
        self.assertEqual(btlink.led_indicator(0), "")

    def test_led_names(self):
        self.assertEqual(btlink.led_names(0x02), "CapsLock")
        self.assertEqual(btlink.led_names(0x03), "NumLock CapsLock")
        self.assertEqual(btlink.led_names(0), "")


class LedReportTest(unittest.TestCase):
    """The host re-sends its LED report unchanged; that must stay quiet."""

    def setUp(self):
        self.link = object.__new__(btlink.BluetoothHID)
        self.link.leds = 0
        self.seen = []
        self.link._on_leds = self.seen.append

    def report(self, mask):
        self.link._note_output_report(bytes([1, mask]))

    def test_a_change_is_reported(self):
        self.report(0x02)
        self.assertEqual(self.seen, [0x02])

    def test_an_unchanged_report_is_not(self):
        self.report(0x02)
        self.report(0x02)
        self.assertEqual(self.seen, [0x02])

    def test_an_initial_all_off_report_is_not_news(self):
        self.report(0x00)
        self.assertEqual(self.seen, [])

    def test_turning_it_back_off_is(self):
        self.report(0x02)
        self.report(0x00)
        self.assertEqual(self.seen, [0x02, 0x00])


class ForegroundGuardTest(unittest.TestCase):
    """Nothing leaves this machine while the console is not in front.

    The guard is the whole safety property: whatever is typed at another
    console belongs to that console, and a phone receiving it would be
    receiving somebody's password as readily as anything else.
    """

    def setUp(self):
        self.session = make_session()
        self.device = FakeDevice()

    def send(self, *events):
        # Going to the background sends a release of its own; what matters
        # here is what arrives after that.
        self.session.link.reports = []
        self.device.queue = list(events)
        return self.session.on_device_input(0, None, self.device)

    def test_keystrokes_are_forwarded_from_the_foreground(self):
        self.session.set_foreground(True)
        self.send((KEY_A, True))
        self.assertTrue(self.session.link.reports)

    def test_keystrokes_from_the_background_are_not(self):
        self.session.set_foreground(False)
        self.send((KEY_A, True))
        self.assertEqual(self.session.link.reports, [])

    def test_they_are_still_read_from_the_background(self):
        # Leaving them unread keeps the descriptor readable, and the loop
        # spins on it at whatever rate the poll allows.
        self.session.set_foreground(False)
        self.send((KEY_A, True))
        self.assertEqual(self.device.queue, [])

    def test_the_watch_survives_a_backgrounded_read(self):
        self.session.set_foreground(False)
        self.assertTrue(self.send((KEY_A, True)))

    def test_text_from_the_background_is_not_typed(self):
        typed = []
        self.session.typist.type_text = typed.append
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"hello")
        os.close(write_fd)
        self.session.foreground = False
        self.session.typist.on_text_input(read_fd, None)
        self.assertEqual(typed, [])

    def test_text_from_the_foreground_is(self):
        typed = []
        self.session.typist.type_text = typed.append
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"hello")
        os.close(write_fd)
        self.session.foreground = True
        self.session.typist.on_text_input(read_fd, None)
        self.assertEqual(typed, ["hello"])


class LockLightTest(unittest.TestCase):
    """The phone's lock state, on the physical keyboards.

    Two owners take turns: while btkey holds the grab the lights show the
    phone, and while another console is in front they show that console.
    Handing them over is only half of it - taking them back is the half
    that was missing.
    """

    def setUp(self):
        self.session = make_session()
        self.session.set_foreground(True)
        self.session.leds_from_host = True

    def test_a_report_from_the_phone_lights_the_keyboards(self):
        self.session.on_host_leds(0x02)
        self.assertEqual(self.session.keyboards.leds, 0x02)

    def test_leaving_hands_the_console_its_own_lights_back(self):
        self.session.on_host_leds(0x02)
        self.session.set_foreground(False)
        self.assertFalse(self.session.keyboards.grabbed)

    def test_coming_back_puts_the_phone_state_on_again(self):
        self.session.on_host_leds(0x02)
        self.session.set_foreground(False)
        self.session.keyboards.leds = None      # as the console left them
        self.session.set_foreground(True)
        self.assertEqual(self.session.keyboards.leds, 0x02)

    def test_coming_back_with_nothing_locked_says_so(self):
        # Not "leave them alone": the console may have lit caps itself
        # while it had them.
        self.session.on_host_leds(0x00)
        self.session.set_foreground(False)
        self.session.keyboards.leds = 0x02
        self.session.set_foreground(True)
        self.assertEqual(self.session.keyboards.leds, 0x00)

    def test_the_status_line_and_the_lights_agree_after_a_round_trip(self):
        self.session.on_host_leds(0x02)
        self.session.set_foreground(False)
        self.session.set_foreground(True)
        self.assertEqual(self.session.display.indicator, "CAPS")
        self.assertEqual(self.session.keyboards.leds, 0x02)

    def test_a_keyboard_plugged_in_while_we_hold_the_grab_is_lit(self):
        self.session.on_host_leds(0x02)
        self.session.keyboards.leds = None
        self.session.keyboards.refresh = lambda: ([FakeDevice()], [])
        self.session.watch_device = lambda device: None
        self.session.rescan_devices()
        self.assertEqual(self.session.keyboards.leds, 0x02)

    def test_a_rescan_that_found_nothing_does_not_touch_them(self):
        self.session.on_host_leds(0x02)
        self.session.keyboards.leds = None
        self.session.rescan_devices()
        self.assertIsNone(self.session.keyboards.leds)

    def test_a_keyboard_plugged_in_from_the_background_is_left_alone(self):
        # That console owns the lights; it is not ours to write to.
        self.session.on_host_leds(0x02)
        self.session.set_foreground(False)
        self.session.keyboards.leds = None
        self.session.keyboards.refresh = lambda: ([FakeDevice()], [])
        self.session.watch_device = lambda device: None
        self.session.rescan_devices()
        self.assertIsNone(self.session.keyboards.leds)


class DropReportTest(unittest.TestCase):
    """Saying what could not be typed, including what has no printable form."""

    def test_a_control_character_is_named(self):
        from btkey.typist import describe
        self.assertEqual(describe("\x7f"), "Backspace")
        self.assertEqual(describe("\x1b"), "Escape")

    def test_an_unnamed_one_is_given_its_code_point(self):
        from btkey.typist import describe
        self.assertEqual(describe("\x01"), "U+0001")

    def test_a_printable_one_is_itself(self):
        from btkey.typist import describe
        self.assertEqual(describe("\u00e9"), "\u00e9")

    def test_an_untypeable_control_character_is_reported(self):
        # It used to be dropped in silence: the complaint listed only
        # printable characters, so a Backspace going nowhere looked exactly
        # like a Backspace that was never pressed.
        said = []
        session = make_session()
        session.typist.log = said.append
        session.link.connected = True
        session.typist.keymap = {}
        session.typist.type_text("\x7f")
        self.assertTrue(any("Backspace" in line for line in said), said)


class BrailleKeyTest(unittest.TestCase):
    """Text arriving on the console, as BRLTTY's braille keyboard sends it.

    Measured with tools/btkey-trace-input: braille typing comes in as text,
    not through evdev.  So every key that produces no character has to be
    decoded from an escape sequence before anything else can happen to it.
    """

    def setUp(self):
        self.session = make_session()
        self.session.foreground = True
        self.session.link.connected = True
        self.typist = self.session.typist
        self.typist.keymap = {"a": ((30, 0),), "\x7f": ((14, 0),)}

    def usages(self):
        return [usage for _, keys in self.session.link.reports
                for usage in keys if usage]

    def type(self, text):
        self.session.link.reports = []
        self.typist.type_text(text)
        while self.typist.queue:
            self.typist.drain()

    def test_a_letter_still_goes_through(self):
        self.type("a")
        self.assertIn(0x04, self.usages())

    def test_backspace_arrives_as_a_key(self):
        self.type("\x7f")
        self.assertIn(0x2A, self.usages())

    def test_delete_is_a_key_not_three_characters(self):
        # "[3~" used to land in the message.
        self.type("\x1b[3~")
        self.assertEqual(self.usages(), [0x4C])

    def test_the_arrows_arrive(self):
        self.type("\x1b[C")
        self.assertEqual(self.usages(), [0x4F])

    def test_a_modified_arrow_carries_its_modifier(self):
        self.type("\x1b[1;5D")
        modifiers = [mods for mods, keys in self.session.link.reports if keys]
        self.assertIn(keycodes.MOD_LEFTCTRL, modifiers)

    def test_a_sequence_split_across_reads_still_works(self):
        self.session.link.reports = []
        self.typist.type_text("\x1b[3")
        self.assertEqual(self.usages(), [])      # nothing yet
        self.typist.type_text("~")
        while self.typist.queue:
            self.typist.drain()
        self.assertEqual(self.usages(), [0x4C])

    def test_an_escape_on_its_own_is_held_then_sent(self):
        self.typist.keymap["\x1b"] = ((1, 0),)
        self.session.link.reports = []
        self.typist.type_text("\x1b")
        self.assertEqual(self.usages(), [])      # waiting to become an arrow
        self.typist._give_up_waiting()
        while self.typist.queue:
            self.typist.drain()
        self.assertEqual(self.usages(), [0x29])

    def test_backtab_reaches_the_phone_as_shift_tab(self):
        self.type("\x1b[Z")
        self.assertEqual(self.usages(), [0x2B])          # Tab
        modifiers = [mods for mods, keys in self.session.link.reports if keys]
        self.assertIn(keycodes.MOD_LEFTSHIFT, modifiers)

    def test_cancelling_drops_a_half_arrived_sequence(self):
        self.typist.type_text("\x1b[3")
        self.typist.clear()
        self.assertEqual(self.typist.pending, "")
        self.assertIsNone(self.typist.escape_timer)

    def test_an_undecodable_sequence_is_reported_not_typed(self):
        said = []
        self.typist.log = said.append
        self.type("\x1b[200~")
        self.assertEqual(self.usages(), [])
        self.assertTrue(any("ESC" in line for line in said), said)


class TopRowTest(unittest.TestCase):
    """Sending what an Apple keyboard's top row sends.

    A phone is built for a keyboard whose F-row carries brightness, search,
    playback and volume on the consumer page.  A PC keyboard sends F1 to
    F12 on the keyboard page, which iOS does nothing with, so the row does
    nothing at all until it is translated.
    """

    def media(self):
        # Through the option, not by setting the table: the wiring from one
        # to the other is the part worth testing.
        session = make_session(top_row="media")
        session.foreground = True
        return session

    def test_the_row_is_untranslated_by_default(self):
        session = make_session()
        press(session, KEY_F12)
        self.assertIn(0x45, [k for _, keys in session.link.reports
                             for k in keys])          # F12 on the key page

    def test_media_mode_sends_volume_instead(self):
        session = self.media()
        press(session, KEY_F12)
        self.assertEqual(session.link.consumer, [0x00E9])

    def test_letting_go_of_it_lets_go_of_the_media_key(self):
        session = self.media()
        press(session, KEY_F12)
        release(session, KEY_F12)
        self.assertEqual(session.link.consumer, [0x00E9, 0])

    def test_the_keyboard_page_is_not_used_in_media_mode(self):
        session = self.media()
        press(session, KEY_F12)
        self.assertEqual([k for _, keys in session.link.reports
                          for k in keys if k], [])

    def test_dictation_is_on_f5_as_it_is_on_an_apple_row(self):
        session = self.media()
        press(session, KEY_F5)
        self.assertEqual(session.link.consumer, [0x00CF])

    def test_expose_is_on_f3(self):
        # 0x029F, which needed the consumer range widened to reach.
        session = self.media()
        press(session, KEY_F3)
        self.assertEqual(session.link.consumer, [0x029F])

    def test_the_whole_row_is_mapped(self):
        for keycode in keycodes.FUNCTION_KEYS:
            self.assertIn(keycode, keycodes.TOP_ROW_MEDIA, keycode)

    def test_every_usage_fits_what_the_descriptor_declares(self):
        """Read off the descriptor, not written down beside it.

        A usage above the declared maximum is one the phone was told to
        expect nothing above, and raising that maximum costs a re-pair, so
        this has to fail here rather than on the phone.
        """
        import test_hidspec
        from btkey import hidspec
        consumer = [item for item in test_hidspec.parse(hidspec.REPORT_DESCRIPTOR)
                    if item.state.get("report_id") == hidspec.REPORT_ID_CONSUMER]
        top = consumer[0].state["logical_max"]
        for target in keycodes.TOP_ROW_MEDIA.values():
            self.assertLessEqual(keycodes.CONSUMER[target], top, target)

    def test_the_console_chord_still_wins(self):
        # Alt+F2 switches console in either mode; the translation happens
        # after the chords precisely so it cannot swallow them.
        session = self.media()
        press(session, KEY_LEFTALT, KEY_F2)
        self.assertEqual(session.consoles.switched, [2])
        # Switching lets go of everything, which is a zero and not a key.
        self.assertEqual(session.link.consumer, [0])

    def test_every_mapping_lands_on_a_key_that_can_be_sent(self):
        for source, target in keycodes.TOP_ROW_MEDIA.items():
            self.assertIn(source, keycodes.FUNCTION_KEYS, source)
            self.assertIn(target, keycodes.CONSUMER, target)


class TopRowDetectionTest(unittest.TestCase):
    """Asking the host what it is, rather than being told.

    An Apple host acts on the consumer usages its own top row sends and
    does nothing with F1 to F12; anything else is likelier to want the
    function keys.  The host publishes a Device ID record saying who made
    it, so the answer is available without guessing from its name.
    """

    def session(self, setting, vendor):
        session = make_session(top_row=setting)
        session.link.host_vendor = lambda peer: vendor
        session.said = []
        session.log = session.said.append
        return session

    def connect(self, session, peer="AA:BB:CC:DD:EE:FF"):
        session.link.peer = peer
        session.link.connected = True
        session.choose_top_row(peer)
        return session

    def test_an_apple_host_gets_the_media_row(self):
        session = self.connect(self.session("auto", 0x004C))
        self.assertEqual(session.top_row, keycodes.TOP_ROW_MEDIA)

    def test_anything_else_keeps_the_function_keys(self):
        session = self.connect(self.session("auto", 0x0006))   # Microsoft
        self.assertEqual(session.top_row, {})

    def test_a_host_that_says_nothing_keeps_them_too(self):
        # No Device ID record is not the same answer as "not Apple", but
        # it is the same decision: do not change what F1 does on a guess.
        session = self.connect(self.session("auto", None))
        self.assertEqual(session.top_row, {})

    def test_it_says_which_it_decided_and_why(self):
        session = self.connect(self.session("auto", 0x004C))
        self.assertTrue(any("Apple" in line for line in session.said),
                        session.said)

    def test_being_told_media_outright_is_not_second_guessed(self):
        session = self.connect(self.session("media", 0x0006))
        self.assertEqual(session.top_row, keycodes.TOP_ROW_MEDIA)

    def test_being_told_function_outright_is_not_either(self):
        session = self.connect(self.session("function", 0x004C))
        self.assertEqual(session.top_row, {})

    def test_connecting_is_what_asks(self):
        # Not a call anyone makes by hand: without this the detection is
        # written and never reached.
        session = self.session("auto", 0x004C)
        session.link.peer = "AA:BB:CC:DD:EE:FF"
        session.on_connection_state(True, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(session.top_row, keycodes.TOP_ROW_MEDIA)

    def test_the_answer_follows_the_host_that_connected(self):
        # A second phone must not inherit the first one's answer.
        session = self.session("auto", 0x004C)
        self.connect(session)
        session.link.host_vendor = lambda peer: 0x0006
        self.connect(session, "11:22:33:44:55:66")
        self.assertEqual(session.top_row, {})


class StartupLineTest(unittest.TestCase):
    """What btkey says about itself before it says anything else.

    Which version is running, and who started it.  Both were reachable
    only from the log file, which an installed btkey does not write, and
    "am I running the copy I just built" is the question that costs the
    most to answer wrongly.
    """

    def setUp(self):
        from btkey import session as module
        self.module = module
        self.saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(),
                                 os.environ.update(self.saved)))

    def run_startup(self):
        from btkey import btlink
        said = []
        session = make_session()
        # run() gives up before anything else when it finds no keyboard.
        device = FakeDevice()
        session.keyboards.devices[device.path] = device
        session.watch_device = lambda d: None
        session.log = said.append
        session.start_services = lambda: (_ for _ in ()).throw(
            btlink.ProfileNotAvailable("no"))
        session.stop_services = lambda: None
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            session.run()
        finally:
            sys.stderr = stderr
        return said

    def test_the_version_is_said_on_the_console(self):
        from btkey import __version__
        self.assertTrue(any(__version__ in line for line in self.run_startup()),
                        "no version in the startup output")

    def test_it_is_said_before_anything_can_fail(self):
        # start_services raises here, and the version is still the first
        # thing out: a failed startup has to say which version failed.
        said = self.run_startup()
        from btkey import __version__
        self.assertIn(__version__, said[0])

    def test_who_started_it_is_said_too(self):
        os.environ["SUDO_UID"] = str(os.getuid())
        os.environ["SUDO_GID"] = str(os.getgid())
        import pwd
        name = pwd.getpwuid(os.getuid()).pw_name
        self.assertTrue(any(name in line for line in self.run_startup()), name)

    def test_the_person_is_named_not_the_process(self):
        # Under sudo the process is root; saying "root" says nothing.
        from btkey.session import started_by
        os.environ["SUDO_UID"] = str(os.getuid())
        os.environ["SUDO_GID"] = str(os.getgid())
        import pwd
        self.assertEqual(started_by(), pwd.getpwuid(os.getuid()).pw_name)

    def test_an_account_that_no_longer_exists_is_still_reported(self):
        from btkey.session import started_by
        os.environ["SUDO_UID"] = "424242"
        os.environ["SUDO_GID"] = "424242"
        self.assertEqual(started_by(), "uid 424242")

    def test_without_sudo_it_is_whoever_we_are(self):
        from btkey.session import started_by
        os.environ.pop("SUDO_UID", None)
        os.environ.pop("SUDO_GID", None)
        import pwd
        self.assertEqual(started_by(), pwd.getpwuid(os.geteuid()).pw_name)


class LastHostTest(unittest.TestCase):
    """The paired host is read from disk once, not per keystroke.

    reconnect() asks for it on every key event while the link is down -
    that is deliberate, a real keyboard wakes its host - and it asks
    before its own ten second rate limit has had a chance to say no.  So
    this was an open, a read and a close per keystroke, answering a
    question this class is the only thing that ever changes.
    """

    def link(self, contents=None):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        link = object.__new__(btlink.BluetoothHID)
        link.STATE_FILE = os.path.join(directory, "host")
        link._known_host = btlink.BluetoothHID.UNREAD
        if contents is not None:
            with open(link.STATE_FILE, "w") as handle:
                handle.write(contents)
        return link

    def test_it_reads_what_is_there(self):
        link = self.link("AA:BB:CC:DD:EE:FF\n")
        self.assertEqual(link.last_host(), "AA:BB:CC:DD:EE:FF")

    def test_no_file_is_no_host(self):
        self.assertIsNone(self.link().last_host())

    def test_an_empty_file_is_no_host(self):
        self.assertIsNone(self.link("\n").last_host())

    def test_the_file_is_read_once(self):
        link = self.link("AA:BB:CC:DD:EE:FF\n")
        self.assertEqual(link.last_host(), "AA:BB:CC:DD:EE:FF")
        os.unlink(link.STATE_FILE)          # the disk no longer agrees
        self.assertEqual(link.last_host(), "AA:BB:CC:DD:EE:FF")

    def test_having_no_host_is_remembered_too(self):
        # Otherwise the miss is re-tried on every keystroke, forever,
        # which is the case with no rate limit in front of it at all.
        link = self.link()
        self.assertIsNone(link.last_host())
        with open(link.STATE_FILE, "w") as handle:
            handle.write("AA:BB:CC:DD:EE:FF\n")
        self.assertIsNone(link.last_host())

    def test_pairing_with_someone_updates_it(self):
        link = self.link()
        self.assertIsNone(link.last_host())
        link._remember_host("11:22:33:44:55:66")
        self.assertEqual(link.last_host(), "11:22:33:44:55:66")

    def test_the_written_file_says_the_same(self):
        link = self.link()
        link._remember_host("11:22:33:44:55:66")
        with open(link.STATE_FILE) as handle:
            self.assertEqual(handle.read().strip(), "11:22:33:44:55:66")


class LinkFailureTest(unittest.TestCase):
    """A BlueZ refusal reaches the console as a line, not a traceback."""

    def test_a_dbus_failure_is_converted_at_the_boundary(self):
        link = object.__new__(btlink.BluetoothHID)
        link._find_adapter = lambda: (_ for _ in ()).throw(
            btlink.dbus.DBusException("nope"))
        with self.assertRaises(btlink.LinkError):
            link.start()

    def test_the_message_says_who_refused_and_why(self):
        exc = btlink.dbus.DBusException("adapter is busy")
        exc.get_dbus_name = lambda: "org.bluez.Error.Busy"
        exc.get_dbus_message = lambda: "adapter is busy"
        said = btlink.describe(exc)
        self.assertIn("org.bluez.Error.Busy", said)
        self.assertIn("adapter is busy", said)

    def test_a_failure_with_no_detail_still_says_who(self):
        exc = btlink.dbus.DBusException()
        exc.get_dbus_name = lambda: "org.bluez.Error.Failed"
        exc.get_dbus_message = lambda: ""
        self.assertIn("org.bluez.Error.Failed", btlink.describe(exc))

    def test_something_that_is_not_a_dbus_failure_is_left_alone(self):
        self.assertEqual(btlink.describe(OSError("plain")), "plain")

    def test_the_session_does_not_know_what_dbus_is(self):
        """The seam this conversion exists to draw.

        btd converts to BluetoothdError and btlink to LinkError, so the
        loop can report a BlueZ refusal without importing dbus to catch
        one or to format it.
        """
        source = source_of("session.py")
        self.assertNotIn("import dbus", source)
        self.assertNotIn("dbus.", source)
        self.assertIn("btlink.LinkError", source)


class ProxyCacheTest(unittest.TestCase):
    """BlueZ proxies are built once and kept.

    Every dbus.Interface(bus.get_object(...)) costs a GetNameOwner to the
    bus daemon and an Introspect of the object, both blocking, both on
    the main loop, before the call anyone wanted is sent.  The 5 second
    class-of-device backstop was paying all three every time.
    """

    def link(self):
        built = []

        class Bus:
            def get_object(self, name, path):
                built.append(path)
                return ("object", path)

        link = object.__new__(btlink.BluetoothHID)
        link.adapter_path = "/org/bluez/hci0"
        link.bus = Bus()
        link._proxies = {}
        self.addCleanup(setattr, btlink.dbus, "Interface",
                        btlink.dbus.Interface)
        btlink.dbus.Interface = lambda obj, iface: (obj, iface)
        return link, built

    def test_the_adapter_is_looked_up_once(self):
        link, built = self.link()
        self.assertIs(link._adapter_props(), link._adapter_props())
        self.assertEqual(built, ["/org/bluez/hci0"])

    def test_two_interfaces_on_one_path_stay_apart(self):
        # Same object, different interface: one cache entry each.
        link, _ = self.link()
        self.assertIsNot(link._manager(btlink.AGENT_MANAGER_IFACE),
                         link._manager(btlink.PROFILE_MANAGER_IFACE))

    def test_two_devices_stay_apart(self):
        link, _ = self.link()
        self.assertIsNot(link._device("AA:BB:CC:DD:EE:FF",
                                      btlink.DEVICE_IFACE),
                         link._device("11:22:33:44:55:66",
                                      btlink.DEVICE_IFACE))

    def test_one_device_is_looked_up_once(self):
        link, built = self.link()
        link._device("AA:BB:CC:DD:EE:FF", btlink.DEVICE_IFACE)
        link._device("AA:BB:CC:DD:EE:FF", btlink.DEVICE_IFACE)
        self.assertEqual(built, ["/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"])

    def test_the_path_is_spelled_the_way_bluez_spells_it(self):
        link, built = self.link()
        link._device("AA:BB:CC:DD:EE:FF", btlink.DEVICE_IFACE)
        self.assertEqual(built, ["/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"])


class HostVendorTest(unittest.TestCase):
    """Reading the company identifier out of the host's Device ID record."""

    def link_reporting(self, modalias):
        link = object.__new__(btlink.BluetoothHID)
        link.adapter_path = "/org/bluez/hci0"
        link._proxies = {}

        class Props:
            def Get(self, interface, name):
                if modalias is None:
                    raise btlink.dbus.DBusException("no such property")
                return modalias

        class Bus:
            def get_object(self, name, path):
                return None

        link.bus = Bus()
        self.addCleanup(setattr, btlink.dbus, "Interface",
                        btlink.dbus.Interface)
        btlink.dbus.Interface = lambda obj, iface: Props()
        return link

    def test_apple_is_004c(self):
        # The company identifier Apple holds with the Bluetooth SIG, and
        # what this phone puts in its own record: bluetooth:v004Cp7510d1A60.
        self.assertEqual(btlink.BluetoothHID.APPLE_VENDOR, 0x004C)

    def test_the_vendor_is_read_from_the_modalias(self):
        link = self.link_reporting("bluetooth:v004Cp7510d1A60")
        self.assertEqual(link.host_vendor("AA:BB:CC:DD:EE:FF"), 0x004C)

    def test_four_digits_of_it_and_not_two(self):
        link = self.link_reporting("bluetooth:v0006p0001d0001")
        self.assertEqual(link.host_vendor("AA:BB:CC:DD:EE:FF"), 0x0006)

    def test_a_modalias_of_another_shape_is_no_answer(self):
        link = self.link_reporting("usb:v1234p5678")
        self.assertIsNone(link.host_vendor("AA:BB:CC:DD:EE:FF"))

    def test_a_host_with_no_record_is_no_answer(self):
        link = self.link_reporting(None)
        self.assertIsNone(link.host_vendor("AA:BB:CC:DD:EE:FF"))


class AudioOfferTest(unittest.TestCase):
    """Asking the phone to open its audio channel, not just the keyboard.

    Pairing a keyboard and routing audio are separate decisions to a phone
    and it makes the second by connecting a second profile.  After a fresh
    bond it often connects HID and stops, so the machine advertises
    somewhere to send sound and nothing ever asks it to: everything is in
    place and the audio simply does not arrive.
    """

    def setUp(self):
        self.said = []
        self.session = make_session()
        self.session.log = self.said.append
        self.asked = []
        self.session.link.connect_audio = self.answer

    #: Set to a (message, again) pair to answer with something else.
    reply = ("audio channel connected", False)

    def answer(self, peer):
        self.asked.append(peer)
        return self.reply

    def connect(self, peer="AA:BB:CC:DD:EE:FF"):
        self.session.link.peer = peer
        self.session.link.connected = True
        return peer

    def test_it_asks_once_the_link_is_up(self):
        peer = self.connect()
        self.session.offer_audio(peer)
        self.assertEqual(self.asked, [peer])

    def test_what_happened_is_logged(self):
        peer = self.connect()
        self.session.offer_audio(peer)
        self.assertIn("audio channel connected", self.said)

    def test_a_link_that_went_away_is_not_asked(self):
        peer = self.connect()
        self.session.link.connected = False
        self.session.offer_audio(peer)
        self.assertEqual(self.asked, [])

    def test_a_different_phone_since_is_not_asked(self):
        # The delay means the answer can arrive after another connection.
        self.connect("AA:BB:CC:DD:EE:FF")
        self.session.offer_audio("11:22:33:44:55:66")
        self.assertEqual(self.asked, [])

    def test_it_does_not_repeat_itself(self):
        peer = self.connect()
        self.session.offer_audio(peer)
        self.session.offer_audio(peer)
        self.assertEqual(len(self.asked), 2)      # once per call, not a loop
        self.assertFalse(self.session.offer_audio(peer))   # never reschedules

    def scheduled(self, session, peer="AA:BB:CC:DD:EE:FF"):
        """What connecting queues up.

        The offer goes on a timer, so asserting on what it did by the time
        on_connection_state returns asserts nothing at all: the timer has
        not fired and never will in a test.
        """
        def connect():
            session.link.peer = peer
            session.on_connection_state(True, peer)

        return [(callback, arguments)
                for _, callback, arguments
                in capture_timers(connect, "timeout_add_seconds")]

    def test_connecting_schedules_the_offer(self):
        session = make_session()
        session.options.audio = True
        planned = self.scheduled(session)
        self.assertEqual([(c.__name__, a) for c, a in planned],
                         [("offer_audio", ("AA:BB:CC:DD:EE:FF",))])

    def test_no_audio_means_it_is_never_offered(self):
        session = make_session()
        session.options.audio = False
        self.assertEqual(self.scheduled(session), [])

    def test_a_refusal_is_reported_and_survived(self):
        peer = self.connect()
        self.reply = ("no audio channel: not supported", False)
        self.session.offer_audio(peer)
        self.assertIn("no audio channel: not supported", self.said)

    def test_being_told_busy_is_not_an_answer(self):
        """It is a request to come back, and used to end the matter.

        A single attempt refused left the machine advertising somewhere
        to send sound with nothing ever asking for it again, and nothing
        anywhere saying why.
        """
        peer = self.connect()
        self.reply = ("no audio channel: in progress", True)
        timers = capture_timers(lambda: self.session.offer_audio(peer),
                                "timeout_add_seconds")
        self.assertEqual([delay for delay, _, _ in timers],
                         [2 * session_module.AUDIO_CONNECT_DELAY])

    def test_asking_again_carries_the_phone_and_the_next_wait(self):
        peer = self.connect()
        self.reply = ("no audio channel: in progress", True)
        timers = capture_timers(lambda: self.session.offer_audio(peer),
                                "timeout_add_seconds")
        _, _, arguments = timers[0]
        self.assertEqual(arguments[0], peer)
        self.assertEqual(arguments[2],
                         session_module.AUDIO_RETRIES - 1)   # one fewer left

    def test_it_gives_up_in_the_end(self):
        peer = self.connect()
        self.reply = ("no audio channel: in progress", True)
        timers = capture_timers(
            lambda: self.session.offer_audio(peer, wait=8, left=0),
            "timeout_add_seconds")
        self.assertEqual(timers, [])
        self.assertIn("no audio channel: in progress", self.said)

    def test_the_waits_double(self):
        peer = self.connect()
        self.reply = ("no audio channel: in progress", True)
        timers = capture_timers(
            lambda: self.session.offer_audio(peer, wait=4, left=2),
            "timeout_add_seconds")
        self.assertEqual([delay for delay, _, _ in timers], [8])

    def test_the_first_ask_comes_soon(self):
        # Long enough not to be refused out of hand, short enough that
        # the sound is there before anyone wonders where it is.
        self.assertLessEqual(session_module.AUDIO_CONNECT_DELAY, 2)


class SweepTimingTest(unittest.TestCase):
    """What a probe actually cost, against what it was estimated to cost.

    A probe running slower than its estimate has two possible causes that
    call for different things.  The send is on a blocking socket, so a
    phone that cannot absorb reports as fast as btkey produces them stops
    the main loop for as long as it takes; sharing the link with A2DP audio
    is enough to do that.  From the outside the two look identical, so the
    waiting is counted rather than guessed at.
    """

    def setUp(self):
        self.said = []
        self.session = make_session()
        # The sweep was handed the session's log when it was built, so
        # replacing the session's own would leave it reporting elsewhere.
        self.session.sweep.log = self.said.append
        self.session.link.connected = True

    def sweep(self, steps, reports, waiting):
        self.session.sweep.start("probing", steps)
        self.session.link.sent_reports += reports
        self.session.link.send_seconds += waiting
        self.session.sweep.finish("done")
        return "\n".join(self.said)

    def test_it_reports_what_the_probe_cost(self):
        said = self.sweep([(0, 0x04)], reports=2, waiting=0.0)
        self.assertRegex(said, r"\b2 reports")

    def test_it_names_the_time_spent_on_the_link(self):
        said = self.sweep([(0, 0x04)], reports=2, waiting=4.5)
        self.assertIn("4.5s of that waiting on the link", said)

    def test_it_gives_the_estimate_to_compare_against(self):
        # The number, not the word: without it the measurement has nothing
        # to be measured against.
        self.session.sweep.start("probing", [(0, 0x04)] * 125)
        queued = self.session.sweep.queued
        self.session.sweep.finish("done")
        expected = queued * TYPE_INTERVAL_MS / 1000.0
        self.assertIn("estimated %.1fs" % expected, "\n".join(self.said))

    def test_the_counters_are_per_sweep_not_cumulative(self):
        # A second probe must not inherit the first one's waiting.
        self.session.link.sent_reports = 500
        self.session.link.send_seconds = 30.0
        said = self.sweep([(0, 0x04)], reports=2, waiting=1.0)
        self.assertRegex(said, r"\b2 reports")
        self.assertNotIn("502", said)
        self.assertIn("1.0s of that", said)
        self.assertNotIn("31.0s", said)

    def test_finishing_without_starting_says_nothing_misleading(self):
        self.session.sweep.finish("done")
        self.assertNotIn("waiting on the link", "\n".join(self.said))


class LayoutAuthorityTest(unittest.TestCase):
    """Which of the two answers about the phone's layout is believed.

    btkey sends positions, not characters.  The console keymap says what a
    position types *here*; only a measured layout says what it types on the
    phone.  Where the two disagree, or where only the console has an
    answer, the console's is a guess that types some other character.
    """

    def setUp(self):
        self.said = []
        self.session = make_session()
        self.typist = self.session.typist
        self.typist.log = self.said.append
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        # A console that can type three characters the phone may not have.
        self.console = {"a": ((30, 0),), "q": ((16, 0),), "\u00ab": ((26, 0),)}
        self.console.update(kbmap.whitespace(True))
        kbmap_build = kbmap.build
        kbmap.build = lambda fd, shift_newline=True: dict(self.console)
        self.addCleanup(setattr, kbmap, "build", kbmap_build)

    def layout_file(self, body):
        path = os.path.join(self.directory, "phone.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def load(self, body=None):
        self.typist.layout_path = self.layout_file(body) if body else None
        self.typist.load_keymap(0)
        return self.typist.keymap

    # -- with a measured layout ------------------------------------------

    def test_a_measured_entry_wins_over_the_console(self):
        keymap = self.load("a\t16\t0\n")          # the phone has a where q is
        self.assertEqual(keymap["a"], ((16, 0),))

    def test_a_character_the_phone_does_not_have_is_dropped(self):
        # Not kept as a guess: sending the console's position for it types
        # whatever the phone has there, which is a different character.
        keymap = self.load("a\t30\t0\n")
        self.assertNotIn("\u00ab", keymap)
        self.assertNotIn("q", keymap)

    def test_dropping_is_said_out_loud(self):
        self.load("a\t30\t0\n")
        self.assertTrue(any("cannot be sent" in line for line in self.said),
                        self.said)

    def test_the_keys_that_are_not_layout_survive(self):
        # Enter, Tab, Space, Backspace and Escape are positions on any
        # keyboard; a measured layout says nothing about them, and
        # dropping them took Backspace out once already.
        keymap = self.load("a\t30\t0\n")
        for char in ("\n", "\t", " ", "\x7f", "\x08", "\x1b"):
            self.assertIn(char, keymap, repr(char))

    def test_newline_still_follows_the_paste_setting(self):
        self.typist.shift_newline = False
        keymap = self.load("a\t30\t0\n")
        self.assertEqual(keymap["\n"], ((kbmap.KEY_ENTER, 0),))

    def test_a_layout_entry_for_a_key_wins_even_so(self):
        # U+0009 is how a layout file names Tab, the field separator
        # being a tab itself.
        keymap = self.load("U+0009\t15\t2\n")
        self.assertEqual(keymap["\t"], ((15, 2),))

    # -- without one -----------------------------------------------------

    def test_the_console_is_used_whole_when_there_is_no_layout(self):
        keymap = self.load(None)
        self.assertIn("\u00ab", keymap)
        self.assertIn("q", keymap)

    def test_an_unreadable_layout_falls_back_to_the_console(self):
        # Rather than to almost nothing, which would be a worse failure
        # than the guessing this replaces.
        self.typist.layout_path = os.path.join(self.directory, "absent.conf")
        self.typist.load_keymap(0)
        self.assertIn("\u00ab", self.typist.keymap)
        self.assertTrue(any("ignoring" in line for line in self.said))


class DeviceLifecycleTest(unittest.TestCase):
    """Keyboards coming and going while btkey runs."""

    def setUp(self):
        self.session = make_session()
        self.session.set_foreground(True)

    def test_a_keyboard_that_goes_away_is_dropped(self):
        device = FakeDevice()
        device.queue = None                  # read_keys returns None
        self.session.keyboards.devices[device.path] = device
        self.session.watches[device.path] = 0
        self.assertFalse(self.session.on_device_input(0, None, device))
        self.assertNotIn(device.path, self.session.keyboards.devices)
        self.assertNotIn(device.path, self.session.watches)

    def test_a_keyboard_that_appears_is_named(self):
        # Watching it comes later, once grab_all has said whether it is
        # ours; a rescan on its own only says what is there.
        device = FakeDevice()
        said = []
        self.session.log = said.append
        self.session.keyboards.refresh = lambda: ([device], [])
        self.session.rescan_devices()
        self.assertTrue([line for line in said if "appeared" in line], said)

    def test_losing_one_we_had_makes_btkey_look_again(self):
        """Whatever took it may have left us its loopback.

        BRLTTY set up mid-session grabs the keyboard and publishes what
        it does not want through uinput; that new device is the one to
        hold, and nothing would have looked for it.
        """
        rounds = []
        self.session.keyboards.grab_all = lambda: rounds.append("grab") or (
            len(rounds) == 1)          # lost one, the first time round
        self.session.keyboards.discard_refusals = (
            lambda: rounds.append("discard"))
        self.session.rescan_devices = lambda: rounds.append("rescan")
        self.session.watch_held_devices = lambda: None
        self.session.take_keyboards()
        self.assertEqual(rounds, ["grab", "discard", "rescan", "grab"])

    def test_being_refused_one_we_never_had_looks_no_further(self):
        rounds = []
        self.session.keyboards.grab_all = (
            lambda: rounds.append("grab") or False)
        self.session.rescan_devices = lambda: rounds.append("rescan")
        self.session.watch_held_devices = lambda: None
        self.session.take_keyboards()
        self.assertEqual(rounds, ["grab"])

    def test_the_second_look_cannot_start_a_third(self):
        # grab_all reporting a loss every time must still terminate.
        rounds = []
        self.session.keyboards.grab_all = (
            lambda: rounds.append("grab") or True)
        self.session.keyboards.discard_refusals = lambda: None
        self.session.rescan_devices = lambda: None
        self.session.watch_held_devices = lambda: None
        self.session.take_keyboards()
        self.assertEqual(len(rounds), 2)

    def test_only_a_keyboard_we_hold_is_watched(self):
        """There is no in between: we have the grab or we have let go.

        Watching one we do not hold would wake btkey for keys that
        either never arrive, because somebody else has the device, or
        arrive twice, because nobody does and the console gets them too.
        """
        held, refused = FakeDevice(), FakeDevice()
        held.grabbed, refused.grabbed = True, False
        held.path, refused.path = "/held", "/refused"
        self.session.keyboards.devices = {"/held": held,
                                          "/refused": refused}
        watched, unwatched = [], []
        self.session.watch_device = watched.append
        self.session.unwatch_device = unwatched.append
        self.session.watch_held_devices()
        self.assertEqual(watched, [held])
        self.assertEqual(unwatched, [refused])

    def test_a_keyboard_that_is_unplugged_is_forgotten(self):
        device = FakeDevice()
        self.session.keyboards.devices[device.path] = device
        self.session.watches[device.path] = 0
        self.session.keyboards.refresh = lambda: ([], [device])
        self.session.rescan_devices()
        self.assertNotIn(device.path, self.session.watches)

    def test_rescanning_keeps_the_timer_alive(self):
        self.assertTrue(self.session.rescan_devices())


class ControlCommandTest(unittest.TestCase):
    """The FIFO takes commands, never text."""

    def setUp(self):
        self.session = make_session()
        self.done = []
        self.session.sweep.learn_layout = lambda: self.done.append("layout")
        self.session.sweep.learn_accents = lambda specs: self.done.append(specs)
        self.session.sweep.cancel = lambda: self.done.append("cancel")

    def feed(self, text):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, text.encode())
        os.close(write_fd)
        return self.session.on_control(read_fd, None)

    def test_learn_layout(self):
        self.feed("learn-layout\n")
        self.assertEqual(self.done, ["layout"])

    def test_learn_accents_carries_its_arguments(self):
        self.feed("learn-accents 16 2 17 2\n")
        self.assertEqual(self.done, [["16", "2", "17", "2"]])

    def test_cancel(self):
        self.feed("cancel\n")
        self.assertEqual(self.done, ["cancel"])

    def test_quit(self):
        session = make_session()
        self.assertFalse(session.quit_requested)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"quit\n")
        os.close(write_fd)
        session.on_control(read_fd, None)
        self.assertTrue(session.quit_requested)

    def test_quitting_says_why(self):
        # The console it was started on gets a reason, since from there
        # an exit somebody asked for from elsewhere has no visible cause.
        said = []
        session = make_session()
        session.announce = said.append
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"quit\n")
        os.close(write_fd)
        session.on_control(read_fd, None)
        self.assertTrue(any("asked to" in line for line in said), said)

    def test_several_commands_in_one_write(self):
        self.feed("learn-layout\ncancel\n")
        self.assertEqual(self.done, ["layout", "cancel"])

    def test_blank_lines_are_not_commands(self):
        self.feed("\n  \n")
        self.assertEqual(self.done, [])

    def test_an_unknown_command_is_logged_and_survived(self):
        self.assertTrue(self.feed("please type my password\n"))
        self.assertEqual(self.done, [])

    def test_the_watch_stays_open_across_commands(self):
        # O_RDWR means end-of-file never arrives; a watch that removed
        # itself would leave a channel that had logged itself as working.
        self.assertTrue(self.feed("cancel\n"))


class KeyboardYankedTest(unittest.TestCase):
    """A keyboard that goes while something is still down on it.

    Unplugged, the kernel sends key-ups for everything it was holding
    before it disappears, so those arrive as ordinary releases.  Dropped
    for any other reason - a read that failed on one still sitting there
    - it sends nothing, and the phone would hold what was down for ever,
    with no key anywhere able to lift it.
    """

    def setUp(self):
        self.session = make_session()
        self.link = self.session.link
        self.gone = FakeDevice()
        self.gone.path = "/gone"
        self.stayed = FakeDevice()
        self.stayed.path = "/stayed"
        self.session.keyboards.devices = {"/gone": self.gone,
                                          "/stayed": self.stayed}

    def test_what_it_was_holding_is_let_go(self):
        press(self.session, 30)                 # a, and nothing left down
        self.session.keyboards.held = set()
        self.link.reports.clear()
        self.session.drop_device(self.gone)
        self.assertEqual(self.session.pressed, [])
        self.assertEqual(self.link.reports[-1], (0, (0, 0, 0, 0, 0, 0)))

    def test_what_another_keyboard_still_holds_stays_down(self):
        """Only that keyboard's keys go, not everything.

        Two keyboards is the ordinary arrangement here - the real one
        and BRLTTY's loopback - and dropping one must not lift what the
        other is holding.
        """
        press(self.session, 30)                 # a
        # What the keyboards left still report as down.
        self.session.keyboards.held = {30}
        self.session.drop_device(self.gone)
        self.assertEqual(self.session.pressed, [30])

    def test_a_media_key_is_let_go_as_well(self):
        """Nothing records that one is down; it is a report, not a key.

        Cutting a volume key short is the safe way to be wrong about it.
        """
        self.link.consumer.clear()
        self.session.drop_device(self.gone)
        self.assertEqual(self.link.consumer, [0])

    def test_a_modifier_that_went_with_it_is_let_go(self):
        """The one that matters: Ctrl leaves on the keyboard that leaves.

        btkey would otherwise put Ctrl into every report it sent
        afterwards, and the phone would read the rest of the session as
        chords.
        """
        press(self.session, 29)                 # Ctrl, held nowhere now
        self.session.keyboards.held = set()
        self.session.drop_device(self.gone)
        self.assertEqual(self.session.modifiers, 0)

    def test_a_modifier_another_keyboard_holds_stays(self):
        press(self.session, 29)
        self.session.keyboards.held = {29}
        self.session.drop_device(self.gone)
        self.assertEqual(self.session.modifiers, keycodes.MOD_LEFTCTRL)


class BackgroundedTest(unittest.TestCase):
    """What btkey holds while another console has the screen: nothing.

    The keyboards are given back, the watch for new ones comes off, and
    the guardian is told to stand down.  Backgrounded, btkey holds no
    grab, so a wedged one is a process doing nothing rather than a
    machine that cannot be typed at, and there is nothing for the
    watchdog's SIGKILL to release.
    """

    class Keeper:
        def __init__(self):
            self.watched = []
            self.beats = 0

        def watch_me(self, seconds):
            self.watched.append(seconds)

        def heartbeat(self):
            self.beats += 1

    def session(self, *devices):
        from test_grab import RecordingDevice, keyboard_factory
        self.keeper = self.Keeper()
        session = make_session(keyboards=keyboard_factory(*devices),
                               keeper=self.keeper)
        session.foreground = False
        self.timers = []
        real = session_module.GLib.timeout_add
        self.addCleanup(setattr, session_module.GLib, "timeout_add", real)
        session_module.GLib.timeout_add = (
            lambda ms, fn, *a: self.timers.append((ms, fn)) or len(self.timers))
        removed = self.removed = []
        real_remove = session_module.GLib.source_remove
        self.addCleanup(setattr, session_module.GLib, "source_remove",
                        real_remove)
        session_module.GLib.source_remove = removed.append
        return session

    def test_taking_the_screen_arms_the_watchdog(self):
        session = self.session()
        session.set_foreground(True)
        self.assertEqual(self.keeper.watched,
                         [session_module.WATCHDOG_SECONDS])

    def told(self, session):
        """What the guardian is asked to remember, and to forget."""
        asked = []
        self.keeper.restore_repeat_on_death = (
            lambda path, delay, period: asked.append(("keep", path,
                                                      delay, period)))
        self.keeper.forget_repeat = lambda path: asked.append(("drop", path))
        return asked

    def test_a_hushed_keyboard_is_handed_to_the_guardian(self):
        """The undo has to outlive us, because the setting does.

        btkey killed while holding a keyboard would leave it typing one
        character however long a key is held, and nothing anywhere
        saying why.
        """
        session = self.session()          # builds self.keeper
        asked = self.told(session)
        session.repeat_debt("/dev/input/event0", (250, 33))
        self.assertEqual(asked, [("keep", "/dev/input/event0", 250, 33)])

    def test_a_settled_debt_is_withdrawn(self):
        """Or the guardian would put back a setting nobody owes.

        btkey hands the repeat back itself on the way to another
        console; anything done to that keyboard afterwards is not ours
        to undo.
        """
        session = self.session()
        asked = self.told(session)
        session.repeat_debt("/dev/input/event0", None)
        self.assertEqual(asked, [("drop", "/dev/input/event0")])

    def test_no_guardian_means_nothing_to_hand_it_to(self):
        session = make_session()
        session.repeat_debt("/dev/input/event0", (250, 33))   # must not raise
        session.repeat_debt("/dev/input/event0", None)

    def test_arming_it_is_not_followed_by_a_beat(self):
        """Arming is itself a message, and that is what it counts from.

        A beat sent in the same breath tells the guardian nothing it did
        not just learn; the first timed one lands well inside the
        deadline.
        """
        session = self.session()
        session.set_foreground(True)
        self.assertEqual(self.keeper.beats, 0)
        self.assertIn(session_module.HEARTBEAT_MS,
                      [ms for ms, _ in self.timers])

    def test_giving_it_up_stands_the_watchdog_down(self):
        session = self.session()
        session.set_foreground(True)
        session.set_foreground(False)
        self.assertEqual(self.keeper.watched,
                         [session_module.WATCHDOG_SECONDS, 0])

    def test_giving_it_up_stops_the_beating(self):
        session = self.session()
        session.set_foreground(True)
        beating = session.heartbeat_timer
        session.set_foreground(False)
        self.assertIsNone(session.heartbeat_timer)
        self.assertIn(beating, self.removed)

    def test_coming_back_arms_it_again(self):
        session = self.session()
        session.set_foreground(True)
        session.set_foreground(False)
        session.set_foreground(True)
        self.assertEqual(self.keeper.watched,
                         [session_module.WATCHDOG_SECONDS, 0,
                          session_module.WATCHDOG_SECONDS])

    def test_the_deadline_allows_for_scheduling_and_no_more(self):
        """Two intervals: enough for jitter, not enough to sit out a wedge.

        A beat is not something that goes missing.  The timer fires
        unless the main loop has stopped turning, and inside the running
        loop the only call that blocks is the send to the phone, which
        blocks while holding the keyboard - a machine nobody can type
        at, which is the case the guardian exists for.  So the deadline
        only has to survive ordinary scheduling jitter; anything longer
        is the thing it is meant to catch.
        """
        self.assertGreaterEqual(session_module.WATCHDOG_SECONDS * 1000,
                                session_module.HEARTBEAT_MS * 2)

    def test_no_guardian_is_not_an_error(self):
        session = make_session()
        session.foreground = False
        session.set_foreground(True)
        session.set_foreground(False)
        self.assertIsNone(session.heartbeat_timer)


class DevicesWeCouldNotTakeTest(unittest.TestCase):
    """Only the keyboards we hold a grab on are kept open.

    One we hold no grab on is either somebody else's, and delivers us
    nothing, or nobody's, and then its keys reach the console too and
    come back to us as text.  Either way the descriptor buys nothing.
    """

    def session(self, *devices):
        from test_grab import keyboard_factory
        session = make_session(keyboards=keyboard_factory(*devices))
        session.foreground = False
        return session

    def test_one_that_would_not_come_is_given_back(self):
        from test_grab import RecordingDevice
        held = RecordingDevice("/a", grabbable=False)
        session = self.session(held)
        session.set_foreground(True)
        self.assertTrue(held.closed)
        self.assertNotIn("/a", session.watches)

    def test_one_we_took_is_kept(self):
        from test_grab import RecordingDevice
        ours = RecordingDevice("/a")
        session = self.session(ours)
        session.set_foreground(True)
        self.assertFalse(ours.closed)
        self.assertIn("/a", session.watches)

    def test_it_is_tried_again_on_the_way_back(self):
        # Whatever held it can let go, so giving the descriptor back must
        # not mean giving the keyboard up for the rest of the run.
        from test_grab import RecordingDevice
        device = RecordingDevice("/a", grabbable=False)
        session = self.session(device)
        session.set_foreground(True)
        session.set_foreground(False)
        device.grabbable = True
        session.set_foreground(True)
        self.assertTrue(device.grabbed)
        self.assertFalse(device.closed)
        self.assertIn("/a", session.watches)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
