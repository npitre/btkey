# SPDX-License-Identifier: GPL-2.0-only
"""Reading the keyboard through evdev rather than through the console.

The obvious way to capture keystrokes on a VT is K_MEDIUMRAW on the tty,
and it does not work here.  Fedora's sudoers sets use_pty, so `sudo btkey`
runs with a pty as stdin while sudo's parent reads the real VT to relay
into that pty; opening /dev/ttyN directly would mean two readers fighting
over each keystroke.

evdev sidesteps all of it.  EVIOCGRAB takes the device away from every
other in-kernel handler including the VT layer, so nothing else - not the
console, not sudo's relay - sees a key while we hold it.  It also fails
safe: the grab is a property of the open file description, so the kernel
drops it when the process dies, by any means, with nothing to clean up.

Grabbing is tied to the foreground VT, which is what preserves the model of
"this console is the on/off switch".
"""

import errno
import fcntl
import glob
import os
import struct

from . import keycodes

EV_SYN = 0x00
EV_KEY = 0x01
EV_LED = 0x11

# LED codes happen to run in the same order as the HID keyboard LED report's
# bits - NumLock, CapsLock, ScrollLock, Compose, Kana - so the host's report
# maps onto them one for one.
LED_MAX = 0x0F
LED_BYTES = (LED_MAX + 8) // 8
LED_COUNT = 5

KEY_MAX = 0x2FF
KEY_BYTES = (KEY_MAX + 8) // 8

# struct input_event on a 64-bit kernel: two longs of timeval, then
# __u16 type, __u16 code, __s32 value.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# Key states in the value field.  2 is autorepeat, which we drop: a HID
# host generates its own repeat, and forwarding ours would double it.
KEY_RELEASE, KEY_PRESS, KEY_REPEAT = 0, 1, 2

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, letter, number, size):
    return (direction << 30) | (size << 16) | (ord(letter) << 8) | number


EVIOCGRAB = _ioc(_IOC_WRITE, "E", 0x90, 4)
EVIOCGNAME = _ioc(_IOC_READ, "E", 0x06, 256)
EVIOCGPHYS = _ioc(_IOC_READ, "E", 0x07, 256)
EVIOCGKEY = _ioc(_IOC_READ, "E", 0x18, KEY_BYTES)
EVIOCGLED = _ioc(_IOC_READ, "E", 0x19, LED_BYTES)
EVIOCGBIT_EV = _ioc(_IOC_READ, "E", 0x20, 4)
EVIOCGBIT_KEY = _ioc(_IOC_READ, "E", 0x20 + EV_KEY, KEY_BYTES)

# A device counts as a keyboard only if it can produce all of these.  That
# admits real keyboards and BRLTTY's uinput injector - which is what makes
# pasting from BRLTTY reach the phone - while excluding the power button,
# the ACPI video bus, and the lid switch, none of which should be taken
# away from the local machine.
KEYBOARD_SIGNATURE = (
    16, 17, 18,    # q w e
    30, 31, 32,    # a s d
    44, 45, 46,    # z x c
    28, 57,        # enter, space
)

# A keyboard often presents more than one device: the letters on one, the
# volume and media keys on another that cannot pass the test above.  Those
# would otherwise go on working on this machine while btkey has the rest of
# the keyboard.  They are recognised by sharing a physical path with a
# device that did pass, and this one is why they are not all taken.
KEY_POWER = 116


class InputDevice:
    def __init__(self, path):
        self.path = path
        # Read-write so LED reports from the host can be written back as
        # EV_LED events.  Not every device permits it, and it is not worth
        # failing over: fall back to reading only.
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.writable = True
        except OSError:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            self.writable = False
        self.name = self._name()
        self.phys = self._phys()
        self.keybits = self._keybits()
        self.has_leds = self._has_leds()
        self.grabbed = False
        self.saved_leds = None
        self._buffer = b""

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _phys(self):
        """Where the device is attached, as the kernel spells it.

        The interfaces of one physical keyboard differ only after the last
        `/input`: `usb-0000:c5:00.3-2.2.4/input0` and `.../input1`.  What
        comes before that is the keyboard.
        """
        buf = bytearray(256)
        try:
            fcntl.ioctl(self.fd, EVIOCGPHYS, buf)
        except OSError:
            return ""
        text = buf.split(b"\0", 1)[0].decode("utf-8", "replace")
        return text.rsplit("/input", 1)[0] if "/input" in text else text

    def _name(self):
        buf = bytearray(256)
        try:
            fcntl.ioctl(self.fd, EVIOCGNAME, buf)
        except OSError:
            return "?"
        return buf.split(b"\0", 1)[0].decode("utf-8", "replace")

    def _keybits(self):
        buf = bytearray(KEY_BYTES)
        try:
            fcntl.ioctl(self.fd, EVIOCGBIT_KEY, buf)
        except OSError:
            return bytes(KEY_BYTES)
        return bytes(buf)

    def _has_leds(self):
        buf = bytearray(4)
        try:
            fcntl.ioctl(self.fd, EVIOCGBIT_EV, buf)
        except OSError:
            return False
        return bool(struct.unpack("I", buf)[0] & (1 << EV_LED))

    def has_key(self, code):
        index = code // 8
        return index < len(self.keybits) and bool(self.keybits[index]
                                                  & (1 << (code % 8)))

    def is_keyboard(self):
        return all(self.has_key(code) for code in KEYBOARD_SIGNATURE)

    def is_companion_of(self, roots):
        """Another interface of a keyboard that is already being grabbed.

        Worth taking only for keys btkey can forward, which leaves out a
        trackpad on the same device.  And never if it can power the machine
        off: a keyboard's System Control interface carries Power, Sleep and
        Wake, which belong to this machine rather than to the phone, and
        taking it would leave the power key doing nothing at all.
        """
        if not self.phys or self.phys not in roots:
            return False
        if self.has_key(KEY_POWER):
            return False
        return any(self.has_key(code) for code in keycodes.CONSUMER)

    def grab(self):
        if self.grabbed:
            return True
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        except OSError:
            return False
        self.grabbed = True
        return True

    def ungrab(self):
        if not self.grabbed:
            return
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
        except OSError:
            pass
        self.grabbed = False

    def pressed_keys(self):
        """Which keys are physically down right now.

        Needed because a grab only shows us transitions.  Keys pressed
        while we were ungrabbed - during a spell on another console - went
        to the kernel and never to us, so on taking the keyboard back we
        have to ask what is already held rather than assume nothing is.
        """
        buf = bytearray(KEY_BYTES)
        try:
            fcntl.ioctl(self.fd, EVIOCGKEY, buf)
        except OSError:
            return set()
        return {code for code in range(KEY_MAX + 1)
                if buf[code // 8] & (1 << (code % 8))}

    def leds(self):
        """Current LED state as a bitmask in HID report order."""
        if not self.has_leds:
            return 0
        buf = bytearray(LED_BYTES)
        try:
            fcntl.ioctl(self.fd, EVIOCGLED, buf)
        except OSError:
            return 0
        return buf[0] & ((1 << LED_COUNT) - 1)

    def set_leds(self, mask):
        """Drive the physical LEDs from a HID keyboard LED report."""
        if not (self.has_leds and self.writable):
            return False
        events = b"".join(
            struct.pack(EVENT_FORMAT, 0, 0, EV_LED, code,
                        1 if mask & (1 << code) else 0)
            for code in range(LED_COUNT))
        events += struct.pack(EVENT_FORMAT, 0, 0, EV_SYN, 0, 0)
        try:
            os.write(self.fd, events)
        except OSError:
            return False
        return True

    def read_keys(self):
        """Drain the device, returning [(keycode, is_press), ...].

        Autorepeat and non-key events are dropped.  Returns None if the
        device has gone away, so the caller can forget it.
        """
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return []
            return None
        if not data:
            return None

        self._buffer += data
        events = []
        while len(self._buffer) >= EVENT_SIZE:
            chunk, self._buffer = (self._buffer[:EVENT_SIZE],
                                   self._buffer[EVENT_SIZE:])
            _, _, kind, code, value = struct.unpack(EVENT_FORMAT, chunk)
            if kind == EV_KEY and value in (KEY_PRESS, KEY_RELEASE):
                events.append((code, value == KEY_PRESS))
        return events


def discover(extra_paths=()):
    """Open every keyboard-like device, plus any explicitly named ones."""
    # Resolve the explicit ones first.  They are usually /dev/input/by-id
    # symlinks - the stable form, and the reason the option exists - and
    # deduping on the real path would otherwise let the glob claim the node
    # first, fail the keyboard test, and drop the request silently.
    wanted = {os.path.realpath(path) for path in extra_paths}
    devices, spare, seen = [], [], set()
    for path in list(extra_paths) + sorted(glob.glob("/dev/input/event*")):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            device = InputDevice(path)
        except OSError:
            continue
        if device.is_keyboard() or real in wanted:
            devices.append(device)
        else:
            spare.append(device)

    # A second pass, because whether one of these belongs to us depends on
    # the whole first pass having happened: it is the media keys of a
    # keyboard we are already taking.
    roots = {device.phys for device in devices if device.phys}
    for device in spare:
        if device.is_companion_of(roots):
            devices.append(device)
        else:
            device.close()
    return devices


class KeyboardSet:
    """Every keyboard on the machine, grabbed together or not at all.

    Grabbing is all-or-nothing on purpose: modifiers and letters routinely
    live on different devices - and BRLTTY's injector is a device of its
    own - so holding some but not others would produce reports with half
    the state missing.
    """

    def __init__(self, extra_paths=(), on_event=None):
        self.extra_paths = list(extra_paths)
        self.event = on_event or (lambda message: None)
        self.devices = {}
        self.grabbed = False

    def refresh(self):
        """Rescan for hotplugged keyboards.  Returns (added, removed)."""
        found = {device.path: device for device in discover(self.extra_paths)}

        added = []
        for path, device in found.items():
            if path in self.devices:
                device.close()
                continue
            self.devices[path] = device
            if self.grabbed:
                # Snapshot before grabbing, as grab_all does, or this one
                # keeps the phone's lock state when the grab is released.
                device.saved_leds = device.leds()
                if not device.grab():
                    self.event("could not grab %s (%s); another program "
                               "holds it" % (device.name, path))
            added.append(device)

        removed = [self.devices.pop(path)
                   for path in list(self.devices) if path not in found]
        return added, removed

    def forget(self, device):
        self.devices.pop(device.path, None)
        device.close()

    def grab_all(self):
        self.grabbed = True
        for device in self.devices.values():
            if device.saved_leds is None:
                device.saved_leds = device.leds()
            if not device.grab():
                self.event("could not grab %s; another program holds it"
                           % device.name)

    def held_keys(self):
        """Every key physically down across all keyboards."""
        held = set()
        for device in self.devices.values():
            held |= device.pressed_keys()
        return held

    def set_leds(self, mask):
        """Push a host LED report onto every keyboard that has LEDs."""
        return [device.name for device in self.devices.values()
                if device.set_leds(mask)]

    def restore_leds(self):
        """Back to the state the console had, without giving up the grab."""
        for device in self.devices.values():
            if device.saved_leds is not None:
                device.set_leds(device.saved_leds)

    def ungrab_all(self):
        self.grabbed = False
        for device in self.devices.values():
            # Hand the LEDs back the way the console had them; the phone's
            # caps state is no business of a console we no longer own.
            if device.saved_leds is not None:
                device.set_leds(device.saved_leds)
                device.saved_leds = None
            device.ungrab()

    def close(self):
        self.ungrab_all()
        for device in self.devices.values():
            device.close()
        self.devices = {}
