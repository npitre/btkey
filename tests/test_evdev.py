# SPDX-License-Identifier: GPL-2.0-only
"""Device classification and event decoding.

The classification fixtures are the real capability bitmaps from this
machine, taken from /proc/bus/input/devices.  They matter because getting
the filter wrong is not a cosmetic bug: grabbing the power button or the
ACPI video bus would take the machine's own power key away, and *not*
grabbing BRLTTY's injector would silently break pasting to the phone.
"""

import os
import struct
import shutil
import tempfile
import time
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import evdev


def keybits(proc_words):
    """Convert a `B: KEY=` line into the bitmap EVIOCGBIT returns.

    /proc prints unsigned longs most significant first, so the last word
    holds bits 0..63.
    """
    words = [int(word, 16) for word in proc_words.split()]
    raw = b"".join(word.to_bytes(8, "little") for word in reversed(words))
    return raw.ljust(evdev.KEY_BYTES, b"\0")


def device_with(proc_words, phys=""):
    device = evdev.InputDevice.__new__(evdev.InputDevice)
    device.keybits = keybits(proc_words)
    device.phys = phys.rsplit("/input", 1)[0] if "/input" in phys else phys
    return device


# Real capability bitmaps out of /proc/bus/input/devices, named for what
# each device is rather than what it was: nothing in btkey looks at a device
# name, and a fixture list full of one make of keyboard reads as though
# something might.
FIXTURES = {
    "power button":
        ("8000 10000000000000 0", False),
    "ACPI video bus":
        ("3e000b00000000 0 0 0", False),
    "built-in keyboard":
        ("402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe", True),
    "USB keyboard":
        ("1000000000007 ff800000000007ff febeffdff3cfffff fffffffffffffffe",
         True),
    "USB keyboard, media keys":
        ("100 0 0 300a010802000 0 0 1078002044000 603878d80116e9 "
         "e000000000000 0", False),
    "USB keyboard, power keys":
        ("c000 10000000000000 0", False),
    "BRLTTY injector":
        ("402000007 ffc03078f800d2a9 f2beffdfffefffff fffffffffffffffe", True),
}


# The physical paths the same devices report, which is what says the media
# keys and the letters are one keyboard.  The USB tree position is whatever
# it happened to be; only the shape of it matters.
PHYS = {
    "power button": "PNP0C0C/button/input0",
    "ACPI video bus": "LNXVIDEO/video/input0",
    "built-in keyboard": "isa0060/serio0/input0",
    "USB keyboard": "usb-0000:c5:00.3-2.2.4/input0",
    "USB keyboard, media keys":
        "usb-0000:c5:00.3-2.2.4/input1",
    "USB keyboard, power keys":
        "usb-0000:c5:00.3-2.2.4/input1",
    "BRLTTY injector": "pid-1215/brltty/15",
}


class CompanionTest(unittest.TestCase):
    """The other interfaces of a keyboard that is already being grabbed.

    A keyboard usually presents its letters on one device and its volume
    and media keys on another, which cannot pass the letter-block test.
    Left alone, those keys go on working on this machine while btkey has
    the rest of the keyboard, and the phone never sees them.
    """

    def make(self, name):
        return device_with(FIXTURES[name][0], PHYS[name])

    def roots(self):
        return {self.make(name).phys for name in FIXTURES
                if FIXTURES[name][1]}

    def test_the_media_keys_of_a_grabbed_keyboard_are_taken(self):
        device = self.make("USB keyboard, media keys")
        self.assertTrue(device.is_companion_of(self.roots()))

    def test_its_power_keys_are_not(self):
        # System Control carries Power, Sleep and Wake.  They belong to
        # this machine, and taking them leaves the power key doing nothing.
        device = self.make("USB keyboard, power keys")
        self.assertFalse(device.is_companion_of(self.roots()))

    def test_the_power_button_is_not_a_companion_of_anything(self):
        self.assertFalse(self.make("power button").is_companion_of(self.roots()))

    def test_nor_is_the_acpi_video_bus(self):
        # It emits brightness keys, which btkey does forward, so only the
        # physical path keeps it out.
        self.assertFalse(self.make("ACPI video bus").is_companion_of(self.roots()))

    def test_a_device_on_its_own_path_is_not_taken(self):
        device = device_with(
            FIXTURES["USB keyboard, media keys"][0],
            "usb-0000:c5:00.3-9.9.9/input1")
        self.assertFalse(device.is_companion_of(self.roots()))

    def test_a_sibling_with_nothing_to_forward_is_not_taken(self):
        # A trackpad on the same physical device, say.
        device = device_with("0", "usb-0000:c5:00.3-2.2.4/input2")
        self.assertFalse(device.is_companion_of(self.roots()))

    def test_a_device_with_no_physical_path_is_not_taken(self):
        device = device_with(
            FIXTURES["USB keyboard, media keys"][0])
        self.assertFalse(device.is_companion_of(self.roots()))

    def test_the_interfaces_of_one_keyboard_share_a_root(self):
        self.assertEqual(
            self.make("USB keyboard").phys,
            self.make("USB keyboard, media keys").phys)


class NameBlindnessTest(unittest.TestCase):
    """No decision here may depend on what a device calls itself.

    Every keyboard has to work, not the ones somebody thought to name, so
    classification reads capability bits and the physical path and nothing
    else.  The device name exists to be printed in a log line.
    """

    def test_the_name_plays_no_part_in_classification(self):
        bits = FIXTURES["USB keyboard"][0]
        for name in ("", "USB keyboard", "something else entirely", "\u00e9"):
            device = device_with(bits)
            device.name = name
            self.assertTrue(device.is_keyboard(), name)

    def test_nor_in_deciding_a_companion(self):
        bits = FIXTURES["USB keyboard, media keys"][0]
        for name in ("", "media keys", "a name nobody predicted"):
            device = device_with(bits, "usb-1/input1")
            device.name = name
            self.assertTrue(device.is_companion_of({"usb-1"}), name)

    def test_a_device_named_like_a_keyboard_is_still_judged_on_its_bits(self):
        device = device_with(FIXTURES["power button"][0])
        device.name = "Some Brand Mechanical Keyboard"
        self.assertFalse(device.is_keyboard())


class ClassificationTest(unittest.TestCase):
    def test_real_devices_are_classified_correctly(self):
        for name, (bits, expected) in FIXTURES.items():
            with self.subTest(device=name):
                self.assertEqual(device_with(bits).is_keyboard(), expected)

    def test_brltty_injector_is_grabbed(self):
        """Pasting from BRLTTY depends on this device being taken."""
        bits = FIXTURES["BRLTTY injector"][0]
        self.assertTrue(device_with(bits).is_keyboard())

    def test_power_button_is_left_alone(self):
        bits = FIXTURES["power button"][0]
        self.assertFalse(device_with(bits).is_keyboard())


class EventDecodingTest(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(os.close, self.read_fd)
        self.device = evdev.InputDevice.__new__(evdev.InputDevice)
        self.device.fd = self.read_fd
        self.device._buffer = b""

    def feed(self, *events):
        payload = b"".join(
            struct.pack(evdev.EVENT_FORMAT, 0, 0, kind, code, value)
            for kind, code, value in events)
        os.write(self.write_fd, payload)
        os.close(self.write_fd)
        return self.device.read_keys()

    def test_press_and_release(self):
        self.assertEqual(
            self.feed((evdev.EV_KEY, 30, 1), (evdev.EV_KEY, 30, 0)),
            [(30, True), (30, False)])

    def test_autorepeat_is_dropped(self):
        """The HID host generates its own repeat; forwarding ours doubles it."""
        self.assertEqual(
            self.feed((evdev.EV_KEY, 30, 1), (evdev.EV_KEY, 30, 2),
                      (evdev.EV_KEY, 30, 2), (evdev.EV_KEY, 30, 0)),
            [(30, True), (30, False)])

    def test_syn_events_are_ignored(self):
        self.assertEqual(
            self.feed((evdev.EV_SYN, 0, 0), (evdev.EV_KEY, 57, 1)),
            [(57, True)])

    def test_partial_event_is_buffered(self):
        blob = struct.pack(evdev.EVENT_FORMAT, 0, 0, evdev.EV_KEY, 30, 1)
        os.write(self.write_fd, blob[:10])
        self.assertEqual(self.device.read_keys(), [])
        os.write(self.write_fd, blob[10:])
        os.close(self.write_fd)
        self.assertEqual(self.device.read_keys(), [(30, True)])


class ExplicitDeviceTest(unittest.TestCase):
    """--device names a device the keyboard filter would not take.

    It is usually a /dev/input/by-id symlink, which is the stable form and
    the reason the option exists.  Deduping on the real path let the glob
    reach the node first, fail the keyboard test, close it, and drop the
    request without a word.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.opened = []

        class Stub:
            def __init__(stub, path):
                stub.path = path
                self.opened.append(path)
                stub.name = "stub"
                stub.phys = ""
            def is_keyboard(stub):
                return False
            def is_companion_of(stub, roots):
                return False
            def close(stub):
                pass

        self.original = evdev.InputDevice
        evdev.InputDevice = Stub
        self.addCleanup(setattr, evdev, "InputDevice", self.original)

        self.node = os.path.join(self.directory, "event9")
        open(self.node, "w").close()
        self.link = os.path.join(self.directory, "by-id-stub")
        os.symlink(self.node, self.link)
        self.original_glob = evdev.glob.glob
        evdev.glob.glob = lambda pattern: [self.node]
        self.addCleanup(setattr, evdev.glob, "glob", self.original_glob)

    def test_a_symlink_is_honoured(self):
        found, _ = evdev.discover([self.link])
        self.assertEqual([d.path for d in found], [self.link])

    def test_the_real_node_is_honoured(self):
        found, _ = evdev.discover([self.node])
        self.assertEqual([d.path for d in found], [self.node])

    def test_it_is_opened_once_not_twice(self):
        evdev.discover([self.link])
        self.assertEqual(len(self.opened), 1)

    def test_a_device_nobody_asked_for_is_still_filtered(self):
        self.assertEqual(evdev.discover([])[0], [])


class PhysTest(unittest.TestCase):
    """Reading the physical path, and cutting it back to the device."""

    def phys_of(self, reported):
        device = evdev.InputDevice.__new__(evdev.InputDevice)
        device.fd = -1

        def ioctl(fd, request, buf):
            buf[:len(reported)] = reported.encode()
            return 0

        saved, evdev.fcntl.ioctl = evdev.fcntl.ioctl, ioctl
        try:
            return device._phys()
        finally:
            evdev.fcntl.ioctl = saved

    def test_the_interface_number_is_cut_off(self):
        # It is the only part that differs between the letters and the
        # media keys of one keyboard.
        self.assertEqual(self.phys_of("usb-0000:c5:00.3-2.2.4/input1"),
                         "usb-0000:c5:00.3-2.2.4")

    def test_two_interfaces_come_back_the_same(self):
        self.assertEqual(self.phys_of("usb-0000:c5:00.3-2.2.4/input0"),
                         self.phys_of("usb-0000:c5:00.3-2.2.4/input1"))

    def test_a_path_without_one_is_left_alone(self):
        self.assertEqual(self.phys_of("ALSA"), "ALSA")

    def test_an_unreadable_path_is_empty_not_fatal(self):
        device = evdev.InputDevice.__new__(evdev.InputDevice)
        device.fd = -1

        def refuse(fd, request, buf):
            raise OSError(25, "Inappropriate ioctl for device")

        saved, evdev.fcntl.ioctl = evdev.fcntl.ioctl, refuse
        try:
            self.assertEqual(device._phys(), "")
        finally:
            evdev.fcntl.ioctl = saved


class DiscoverCompanionTest(unittest.TestCase):
    """discover() taking the second interface of a keyboard it found."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        for name in ("event0", "event1", "event2"):
            open(os.path.join(self.directory, name), "w").close()

        plan = {
            "event0": (True, "usb-1/input0"),      # the letters
            "event1": (False, "usb-1/input1"),     # its media keys
            "event2": (False, "usb-2/input0"),     # something else entirely
        }
        closed = self.closed = []

        class Stub:
            def __init__(stub, path):
                stub.path = path
                keyboard, phys = plan[os.path.basename(path)]
                stub.name = os.path.basename(path)
                stub.phys = phys.rsplit("/input", 1)[0]
                stub.keyboard = keyboard
            def is_keyboard(stub):
                return stub.keyboard
            def is_companion_of(stub, roots):
                return not stub.keyboard and stub.phys in roots
            def close(stub):
                closed.append(stub.name)
            def reopen(stub):
                # The real one keeps what it learned and gets a fresh
                # descriptor; nothing here has one to get.
                return True

        saved_device, evdev.InputDevice = evdev.InputDevice, Stub
        self.addCleanup(setattr, evdev, "InputDevice", saved_device)
        saved_glob = evdev.glob.glob
        evdev.glob.glob = lambda pattern: sorted(
            os.path.join(self.directory, name)
            for name in os.listdir(self.directory))
        self.addCleanup(setattr, evdev.glob, "glob", saved_glob)

    def test_the_companion_is_taken(self):
        found = [d.name for d in evdev.discover()[0]]
        self.assertEqual(sorted(found), ["event0", "event1"])

    def test_nothing_is_held_open_while_the_question_is_decided(self):
        """Every node that is not a keyboard is closed as it is judged.

        Waiting for the second pass to decide meant a dozen descriptors
        open at once on a machine with a dozen nodes, to end up holding
        three.  What was learned about each one is kept, so the few that
        come back cost an open and no ioctls.
        """
        found, _ = evdev.discover()
        self.assertEqual(sorted(self.closed), ["event1", "event2"])
        self.assertIn("event1", [device.name for device in found],
                      "the companion should have been taken back")

    def test_the_stranger_is_not_taken_back(self):
        found, _ = evdev.discover()
        self.assertNotIn("event2", [device.name for device in found])

    def test_it_needs_the_whole_first_pass_to_decide(self):
        # event1 is only worth taking because event0 was found, and event0
        # is listed first here; the answer must not depend on that.
        evdev.glob.glob = lambda pattern: [
            os.path.join(self.directory, name)
            for name in ("event1", "event2", "event0")]
        found = [d.name for d in evdev.discover()[0]]
        self.assertEqual(sorted(found), ["event0", "event1"])



class PressedKeysTest(unittest.TestCase):
    """Which keys are physically down, read out of an EVIOCGKEY bitmap.

    A grab only shows transitions, so on taking a keyboard back btkey has
    to ask what is already held; get the bit order wrong and it adopts
    modifiers nobody is pressing, which is how a bare Fn turns into
    Alt+Fn and goes to the phone instead of switching console.
    """

    def device_holding(self, *keycodes_):
        buf = bytearray(evdev.KEY_BYTES)
        for keycode in keycodes_:
            buf[keycode // 8] |= 1 << (keycode % 8)

        device = evdev.InputDevice.__new__(evdev.InputDevice)
        device.fd = 3

        def ioctl(fd, request, into):
            into[:] = buf
            return 0

        self.addCleanup(setattr, evdev.fcntl, "ioctl", evdev.fcntl.ioctl)
        evdev.fcntl.ioctl = ioctl
        return device

    def test_nothing_held_is_an_empty_set(self):
        self.assertEqual(self.device_holding().pressed_keys(), set())

    def test_one_key_is_reported_by_its_own_keycode(self):
        self.assertEqual(self.device_holding(42).pressed_keys(), {42})

    def test_the_bit_order_is_least_significant_first(self):
        # 0 and 7 share a byte and sit at its two ends; reversing the
        # order inside the byte swaps them and nothing else would notice.
        self.assertEqual(self.device_holding(0, 7).pressed_keys(), {0, 7})

    def test_several_across_several_bytes(self):
        held = {1, 29, 42, 56, 100, 255}
        self.assertEqual(self.device_holding(*held).pressed_keys(), held)

    def test_the_highest_keycode_is_not_lost(self):
        self.assertEqual(self.device_holding(evdev.KEY_MAX).pressed_keys(),
                         {evdev.KEY_MAX})

    def test_a_closed_device_holds_nothing(self):
        device = self.device_holding(42)
        device.fd = None
        self.assertEqual(device.pressed_keys(), set())



class RediscoveryTest(unittest.TestCase):
    """A rescan should not relearn what it already knows.

    Every switch back to btkey's console rescans, and every node the
    directory holds used to be opened, asked its name, its physical path,
    its keys and its LEDs, and closed again - to arrive at the answers
    already in hand.  On this machine that is fourteen nodes and about a
    hundred syscalls, per switch.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="btkey-rediscover-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.opened, self.closed, self.reopened = [], [], []
        for name in ("event0", "event1"):
            with open(os.path.join(self.directory, name), "w"):
                pass

        test = self

        class Stub:
            def __init__(self, path):
                test.opened.append(os.path.basename(path))
                self.path = path
                self.name = os.path.basename(path)
                self.phys = ""

            def is_keyboard(self):
                return True

            def is_companion_of(self, roots):
                return False

            def close(self):
                pass

            def reopen(self):
                return True

        self.addCleanup(setattr, evdev, "InputDevice", evdev.InputDevice)
        self.addCleanup(setattr, evdev, "DEVICE_GLOB", evdev.DEVICE_GLOB)
        evdev.InputDevice = Stub
        evdev.DEVICE_GLOB = os.path.join(self.directory, "event*")

    def test_a_first_look_opens_everything(self):
        found, present = evdev.discover()
        self.assertEqual(sorted(self.opened), ["event0", "event1"])
        self.assertEqual(len(found), 2)
        self.assertEqual(len(present), 2)

    def test_a_second_look_opens_nothing(self):
        found, _ = evdev.discover()
        del self.opened[:]
        again, present = evdev.discover(known=found)
        self.assertEqual(self.opened, [])
        self.assertEqual(again, [])
        self.assertEqual(len(present), 2, "held devices must still count")

    def test_only_the_new_one_is_opened(self):
        found, _ = evdev.discover()
        del self.opened[:]
        with open(os.path.join(self.directory, "event2"), "w"):
            pass
        again, present = evdev.discover(known=found)
        self.assertEqual(self.opened, ["event2"])
        self.assertEqual([d.name for d in again], ["event2"])
        self.assertEqual(len(present), 3)

    def test_one_that_went_away_is_left_out_of_what_is_there(self):
        found, _ = evdev.discover()
        os.unlink(os.path.join(self.directory, "event1"))
        _, present = evdev.discover(known=found)
        self.assertEqual([os.path.basename(p) for p in present], ["event0"])

    def test_a_companion_can_still_find_its_keyboard(self):
        """The root it belongs to may be one we are already holding.

        A keyboard's media-key interface can appear on its own - after a
        rescan that already took the keyboard - and it is recognised by
        sharing that keyboard's physical path.  Skipping the keyboard
        must not lose the path it contributes.
        """
        class Keyboard:
            path, name, phys = "/dev/input/event0", "kbd", "usb-1"

            def is_keyboard(self):
                return True

            def close(self):
                pass

            def reopen(self):
                return True

        test = self

        class Companion:
            def __init__(self, path):
                test.opened.append(os.path.basename(path))
                self.path = path
                self.name = "media"
                self.phys = "usb-1"

            def is_keyboard(self):
                return False

            def is_companion_of(self, roots):
                return self.phys in roots

            def close(self):
                test.closed.append(self.name)

            def reopen(self):
                test.reopened.append(self.name)
                return True

        evdev.InputDevice = Companion
        evdev.DEVICE_GLOB = os.path.join(self.directory, "event1")
        self.reopened = []
        found, _ = evdev.discover(known=[Keyboard()])
        self.assertEqual([d.name for d in found], ["media"])
        # Closed while the first pass ran, and taken back once the
        # keyboard it belongs to was known.
        self.assertEqual(self.closed, ["media"])
        self.assertEqual(self.reopened, ["media"])



#: Tacked onto each child: say when the file is open, then wait to die.
READY = "import sys, time; print('ready', flush=True); time.sleep(30)"


class OpenersTest(unittest.TestCase):
    """Naming the program that has a device open.

    A grab refused is nearly always somebody else holding it, and which
    somebody is the whole of what anyone wants to know: "brltty" is
    something to act on, "resource busy" is not.  Nothing reports who
    holds a grab, but holding one means having the device open, and that
    much /proc does say.
    """

    def setUp(self):
        # Not named after btkey: the child's command line carries this
        # path, and naming a holder btkey is exactly what is under test.
        handle, self.path = tempfile.mkstemp(prefix="opened-")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def holding(self, script):
        """A process with the file open, for as long as the test needs it.

        It says when it has the file, rather than being polled for it:
        waiting by asking walks every process in /proc each time round,
        and there is nothing to learn from how long it takes.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", script % self.path + READY],
            stdout=subprocess.PIPE)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        self.assertEqual(child.stdout.readline(), b"ready\n")
        return child

    def test_nobody_holding_it_is_nobody(self):
        self.assertEqual(evdev.openers([self.path])[self.path], [])

    def test_a_holder_is_named(self):
        self.holding("f = open(%r); ")
        self.assertEqual(evdev.openers([self.path])[self.path], ["python3"])

    def test_btkey_is_named_btkey(self):
        """Its comm is python3, which would tell nobody anything.

        The one answer somebody reading this listing most needs is that
        the btkey they are already running has the keyboard, and all is
        well.
        """
        self.holding("btkey = open(%r); ")
        # The command line mentions btkey, as a real one's would.
        self.assertEqual(evdev.openers([self.path])[self.path], ["btkey"])

    def test_we_do_not_count_ourselves(self):
        # list_devices has the device open to try the grab.
        with open(self.path):
            self.assertEqual(evdev.openers([self.path])[self.path], [])

    def test_a_process_can_be_left_out(self):
        child = self.holding("f = open(%r); ")
        self.assertEqual(evdev.openers([self.path], ignore=[child.pid])[self.path], [])

    def test_one_process_holding_it_twice_is_named_once(self):
        """A program with the device open twice is still one program.

        btkey itself opens a device read-write and falls back to
        read-only, and a listing that said "brltty, brltty" would read
        as two of them.
        """
        self.holding("a = open(%r); b = open(a.name); ")
        self.assertEqual(evdev.openers([self.path])[self.path], ["python3"])

    def test_two_holders_are_both_named(self):
        self.holding("f = open(%r); ")
        self.holding("f = open(%r); ")
        self.assertEqual(evdev.openers([self.path])[self.path], ["python3", "python3"])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
