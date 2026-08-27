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
        found = evdev.discover([self.link])
        self.assertEqual([d.path for d in found], [self.link])

    def test_the_real_node_is_honoured(self):
        found = evdev.discover([self.node])
        self.assertEqual([d.path for d in found], [self.node])

    def test_it_is_opened_once_not_twice(self):
        evdev.discover([self.link])
        self.assertEqual(len(self.opened), 1)

    def test_a_device_nobody_asked_for_is_still_filtered(self):
        self.assertEqual(evdev.discover([]), [])


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

        saved_device, evdev.InputDevice = evdev.InputDevice, Stub
        self.addCleanup(setattr, evdev, "InputDevice", saved_device)
        saved_glob = evdev.glob.glob
        evdev.glob.glob = lambda pattern: sorted(
            os.path.join(self.directory, name)
            for name in os.listdir(self.directory))
        self.addCleanup(setattr, evdev.glob, "glob", saved_glob)

    def test_the_companion_is_taken(self):
        found = [d.name for d in evdev.discover()]
        self.assertEqual(sorted(found), ["event0", "event1"])

    def test_the_stranger_is_closed_rather_than_left_open(self):
        evdev.discover()
        self.assertEqual(self.closed, ["event2"])

    def test_it_needs_the_whole_first_pass_to_decide(self):
        # event1 is only worth taking because event0 was found, and event0
        # is listed first here; the answer must not depend on that.
        evdev.glob.glob = lambda pattern: [
            os.path.join(self.directory, name)
            for name in ("event1", "event2", "event0")]
        found = [d.name for d in evdev.discover()]
        self.assertEqual(sorted(found), ["event0", "event1"])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
