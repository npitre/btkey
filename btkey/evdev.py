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
EV_REP = 0x14

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


# Where the kernel puts event devices, and what to watch for one arriving.
DEVICE_DIRECTORY = "/dev/input"
DEVICE_GLOB = DEVICE_DIRECTORY + "/event*"

EVIOCGRAB = _ioc(_IOC_WRITE, "E", 0x90, 4)
EVIOCGNAME = _ioc(_IOC_READ, "E", 0x06, 256)
EVIOCGPHYS = _ioc(_IOC_READ, "E", 0x07, 256)
EVIOCGKEY = _ioc(_IOC_READ, "E", 0x18, KEY_BYTES)
EVIOCGLED = _ioc(_IOC_READ, "E", 0x19, LED_BYTES)
EVIOCGBIT_EV = _ioc(_IOC_READ, "E", 0x20, 4)
EVIOCGBIT_KEY = _ioc(_IOC_READ, "E", 0x20 + EV_KEY, KEY_BYTES)
EVIOCGREP = _ioc(_IOC_READ, "E", 0x03, 8)
EVIOCSREP = _ioc(_IOC_WRITE, "E", 0x03, 8)

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
        self.fd = None
        self.writable = False
        self._open()
        self.name = self._name()
        self.phys = self._phys()
        # Set when a grab was refused, so that the refusal is reported once
        # rather than on every console switch, and so is coming free again.
        self.refused = False
        self.grab_error = None
        self.keybits = self._keybits()
        self.has_leds = self._has_leds()
        self.grabbed = False
        self.saved_leds = None
        self.saved_repeat = None
        # Whether btkey has ever held this one.  Losing a keyboard it
        # had is news; being refused one it never had is not, and the
        # difference is what stops the two from chasing each other.
        self.was_held = False
        self.has_repeat = self._has_repeat()
        self._buffer = b""

    def _open(self):
        """Open the node.  Raises OSError if it cannot be opened at all.

        Read-write so LED reports from the host can be written back as
        EV_LED events.  Not every device permits it, and it is not worth
        failing over: fall back to reading only.
        """
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            self.writable = True
        except OSError:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            self.writable = False

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        # The grab belongs to the open file description, so it went with
        # it.  Saying so here is what lets ungrab_all skip the ioctl.
        self.grabbed = False

    def reopen(self):
        """Open it again after a spell closed.  False if it is gone.

        What was learned about the device the first time - its name, its
        keys, whether it has LEDs - is a property of the device and not of
        the descriptor, so it is kept rather than asked again.
        """
        if self.fd is not None:
            return True
        try:
            self._open()
        except OSError:
            return False       # _open leaves fd None unless it succeeds
        self._buffer = b""
        return True

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

    def _has_repeat(self):
        buf = bytearray(4)
        try:
            fcntl.ioctl(self.fd, EVIOCGBIT_EV, buf)
        except OSError:
            return False
        return bool(struct.unpack("I", buf)[0] & (1 << EV_REP))

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
        """Take the device exclusively.  False, and why, if it will not come.

        The kernel keeps one grab per device: `input_grab_device` refuses
        with EBUSY when `dev->grab` is already set.  So a refusal is nearly
        always somebody else holding it rather than anything about us, but
        not always - the device can also have gone away - and saying the
        wrong one of those sends whoever reads it looking in the wrong
        place.
        """
        if self.grabbed:
            return True
        if self.fd is None:
            self.grab_error = errno.ENODEV
            return False
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        except OSError as exc:
            self.grab_error = exc.errno
            return False
        self.grab_error = None
        self.grabbed = True
        return True

    def ungrab(self):
        if not self.grabbed:
            return
        if self.fd is None:
            self.grabbed = False
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
        if self.fd is None:
            return set()
        buf = bytearray(KEY_BYTES)
        try:
            fcntl.ioctl(self.fd, EVIOCGKEY, buf)
        except OSError:
            return set()
        # Walking all KEY_MAX+1 codes costs thirty times as much as
        # skipping the empty bytes, and this runs per device on every
        # switch back to our console.
        return {index * 8 + bit
                for index, byte in enumerate(buf) if byte
                for bit in range(8) if byte & (1 << bit)}

    def leds(self):
        """Current LED state as a bitmask in HID report order."""
        if not self.has_leds or self.fd is None:
            return 0
        buf = bytearray(LED_BYTES)
        try:
            fcntl.ioctl(self.fd, EVIOCGLED, buf)
        except OSError:
            return 0
        return buf[0] & ((1 << LED_COUNT) - 1)

    def set_leds(self, mask):
        """Drive the physical LEDs from a HID keyboard LED report."""
        if not (self.has_leds and self.writable) or self.fd is None:
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

    def repeat(self):
        """How the kernel repeats a held key: (delay, period) in ms."""
        if self.fd is None or not self.has_repeat:
            return None
        buf = bytearray(8)
        try:
            fcntl.ioctl(self.fd, EVIOCGREP, buf)
        except OSError:
            return None
        return struct.unpack("II", bytes(buf))

    def set_repeat(self, delay, period):
        if self.fd is None or not self.has_repeat:
            return False
        try:
            fcntl.ioctl(self.fd, EVIOCSREP, struct.pack("II", delay, period))
        except OSError:
            return False
        return True

    def hush_repeat(self):
        """Stop the kernel repeating a held key at us.

        A HID keyboard reports which keys are down and lets the host do
        the repeating, so every autorepeat the kernel generates here is
        read and thrown away - thirty wakeups a second for as long as a
        key is held, and holding a modifier is what VoiceOver's chords
        are made of.

        The setting belongs to the device rather than to this
        descriptor, so it has to be given back, and it outlives btkey if
        btkey dies badly.  Both are handled where the LEDs are.
        """
        if self.saved_repeat is not None:
            return None                  # already hushed
        self.saved_repeat = self.repeat()
        if self.saved_repeat is None:
            return None
        self.set_repeat(0, 0)
        return self.saved_repeat

    def restore_repeat(self):
        """Put the repeat back.  Returns what it was, or None if nothing."""
        if self.saved_repeat is None:
            return None
        was, self.saved_repeat = self.saved_repeat, None
        self.set_repeat(*was)
        return was

    def read_keys(self):
        """Drain the device, returning [(keycode, is_press), ...].

        Autorepeat and non-key events are dropped.  Returns None if the
        device has gone away, so the caller can forget it.
        """
        if self.fd is None:
            return []
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


def openers(paths, ignore=()):
    """Which processes have each of these devices open, by name.

    Nothing says who holds a *grab*, but holding one means having the
    device open, so this narrows a bare EBUSY to something worth acting
    on: "brltty" is an answer, "resource busy" is a question.  A process
    whose command line mentions btkey is reported as btkey, since its
    comm is python3 and that would tell nobody anything.

    Every device at once, in one pass over /proc: asking per device
    would walk every process's descriptors once for each of a dozen
    nodes.  Needs to see other users' descriptors, so it answers what it
    can and says nothing about the rest rather than failing.
    """
    wanted = {os.path.realpath(path): path for path in paths}
    found = {path: [] for path in paths}
    ignore = set(ignore) | {os.getpid()}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in ignore:
            continue
        try:
            handles = os.listdir("/proc/%s/fd" % entry)
        except OSError:
            continue            # gone, or not ours to look at
        name, seen = None, set()
        for handle in handles:
            try:
                target = os.readlink("/proc/%s/fd/%s" % (entry, handle))
            except OSError:
                continue
            path = wanted.get(target)
            if path is None or path in seen:
                continue        # a process with one open twice is one
            seen.add(path)
            if name is None:
                name = _process_name(entry)
            found[path].append(name)
    return found


def _process_name(pid):
    try:
        with open("/proc/%s/cmdline" % pid) as handle:
            command = handle.read()
        if "btkey" in command:
            return "btkey"
        with open("/proc/%s/comm" % pid) as handle:
            return handle.read().strip()
    except OSError:
        return "pid %s" % pid


def discover(extra_paths=(), known=()):
    """Open every keyboard-like device, plus any explicitly named ones.

    Devices already held are skipped rather than opened afresh: what the
    open is for - the name, the physical path, which keys and LEDs it has
    - was learned the first time and does not change.  A rescan otherwise
    opens every node in the directory, asks it four questions it has
    already answered, and closes it again, and rescans happen on every
    switch back to our console.

    Returns the devices that are new, and the paths of everything that is
    there, which is how the caller tells one that has gone from one it
    already had.
    """
    # Resolve the explicit ones first.  They are usually /dev/input/by-id
    # symlinks - the stable form, and the reason the option exists - and
    # deduping on the real path would otherwise let the glob claim the node
    # first, fail the keyboard test, and drop the request silently.
    wanted = {os.path.realpath(path) for path in extra_paths}
    held = {os.path.realpath(device.path): device for device in known}
    devices, spare, present, seen = [], [], [], set()
    for path in list(extra_paths) + sorted(glob.glob(DEVICE_GLOB)):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if real in held:
            present.append(held[real].path)
            continue
        try:
            device = InputDevice(path)
        except OSError:
            continue
        present.append(path)
        if device.is_keyboard() or real in wanted:
            devices.append(device)
        else:
            # Closed straight away rather than held open until the
            # second pass decides: nothing here needs its descriptor,
            # and on a machine with a dozen nodes that is a dozen open
            # at once to end up with three.  What was learned about it
            # is kept, so the few that come back cost an open and no
            # ioctls at all.
            device.close()
            spare.append(device)

    # A second pass, because whether one of these belongs to us depends on
    # the whole first pass having happened: it is the media keys of a
    # keyboard we are already taking.  The ones already held count as
    # keyboards we are taking, or a companion arriving on its own would
    # find no root to belong to.
    roots = {device.phys for device in list(held.values()) + devices
             if device.phys}
    for device in spare:
        if device.is_companion_of(roots) and device.reopen():
            devices.append(device)
    return devices, present


class KeyboardSet:
    """Every keyboard on the machine, grabbed together or not at all.

    Grabbing is all-or-nothing on purpose: modifiers and letters routinely
    live on different devices - and BRLTTY's injector is a device of its
    own - so holding some but not others would produce reports with half
    the state missing.
    """

    def __init__(self, extra_paths=(), on_event=None, on_debug=None,
                 on_repeat_debt=None):
        self.extra_paths = list(extra_paths)
        # Told when a device's key repeat has been turned off, so that
        # someone can put it back if btkey never gets the chance - and
        # told again with None once that debt is settled or void, so
        # nobody puts back a setting that is no longer ours to restore.
        self.repeat_debt = on_repeat_debt or (lambda path, repeat: None)
        self.event = on_event or (lambda message: None)
        # Which keyboards came and which did not is worth saying only when
        # it is a problem.  Where another program deliberately holds a
        # keyboard and hands the keys back through one of its own - which
        # is what BRLTTY does when braille commands go on the ordinary
        # keyboard - a refusal at every start reads like a fault and is
        # not one.
        self.debug = on_debug or (lambda message: None)
        self.devices = {}
        self.grabbed = False
        self.asleep = False
        self.empty_handed = False

    def refresh(self):
        """Rescan for hotplugged keyboards.  Returns (added, removed)."""
        found, present = discover(self.extra_paths, list(self.devices.values()))

        added = []
        for device in found:
            path = device.path
            self.devices[path] = device
            if self.asleep:
                # Discovery opens what it finds, and a set that has given
                # its descriptors up wants this one closed as well.
                device.close()
            # Nothing else here: taking a keyboard is grab_all's job, and
            # both callers run it straight after this.
            added.append(device)

        here = set(present)
        removed = [self.devices.pop(path)
                   for path in list(self.devices) if path not in here]
        return added, removed

    def forget(self, device):
        """Drop a device from the set for good, releasing it on the way.

        Usually it has gone, and then putting its state back fails
        harmlessly.  Not always, though: read_keys reports a device lost
        on any error that is not EAGAIN, and a flaky USB keyboard can
        give EIO while still being perfectly present.  That one would
        keep the repeat we turned off, for good.
        """
        self.devices.pop(device.path, None)
        self.release(device)

    def grab_all(self):
        """Take every keyboard, and say which ones would not come.

        A refusal is reported once, and so is a later success: another
        program holding a device may let go between one console switch and
        the next, and a keyboard that quietly starts or stops reaching the
        phone is the whole of what "flaky" means from the outside.

        Returns whether a keyboard btkey had is now somebody else's,
        which is worth looking around after: whatever took it may have
        published a loopback for the keys it does not want.
        """
        self.grabbed = True
        held, lost = 0, False
        for device in self.devices.values():
            if device.grab():
                held += 1
                self.take(device)
                if device.refused:
                    device.refused = False
                    self.debug("%s came free; btkey has it now"
                               % device.name)
            else:
                lost = lost or device.was_held
                # Not ours, so hold nothing of it: the descriptor buys
                # nothing either way, since a device somebody else has
                # delivers us nothing, and one nobody has reaches the
                # console too and comes back as text.  It stays in the
                # set to be tried again on the next switch, which is
                # cheaper than discovering it afresh and is what lets
                # the refusal be reported once instead of every time.
                self.release(device)
            if not device.grabbed and not device.refused:
                device.refused = True
                if device.grab_error == errno.EBUSY:
                    self.debug("could not grab %s; another program holds "
                               "it, so nothing from it reaches btkey at all "
                               "and whatever holds it decides what its keys "
                               "do" % device.name)
                else:
                    self.debug("could not grab %s: %s"
                               % (device.name,
                                  os.strerror(device.grab_error or 0)))

        # One keyboard of several being somebody else's is ordinary and is
        # left to --debug.  Not one of them coming is not ordinary: btkey
        # is then running with no keyboard at all, and saying nothing about
        # it would leave a dead keyboard looking like a dead phone.
        if self.devices and not held:
            if not self.empty_handed:
                self.empty_handed = True
                self.event("no keyboard could be grabbed; nothing typed "
                           "will reach the phone (--debug says why)")
        elif self.empty_handed:
            self.empty_handed = False
            self.event("a keyboard came free; typing reaches the phone again")
        return lost

    def open_all(self):
        """Take them back.  Returns the ones that are no longer there."""
        self.asleep = False
        return [device for device in self.devices.values()
                if not device.reopen()]

    def discard_refusals(self):
        """Forget the keyboards we are not holding.

        They are remembered between switches so a refusal is reported
        once and the keyboard is retried without being discovered
        afresh.  That is only worth having while nothing has changed;
        once something has, the honest answer is to look again.
        """
        for device in [held for held in self.devices.values()
                       if not held.grabbed]:
            self.forget(device)

    def take(self, device):
        """Everything that follows from holding a keyboard.

        All of it after the grab, none of it before.  A device that will
        not come is not ours to read state from or to reconfigure, and a
        moment later it is closed and forgotten with nothing undone -
        so anything done to it here would be done for good.
        """
        device.was_held = True
        if device.saved_leds is None:
            device.saved_leds = device.leds()
        was = device.hush_repeat()
        if was is not None:
            self.repeat_debt(device.path, was)

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

    def release(self, device):
        """Let one keyboard go: take() undone, then closed.

        Every way of letting go arrives here - a switch to another
        console, the device being unplugged, a grab that would not come,
        btkey stopping - so that none of them can be the one that
        forgets a step.  Safe to run twice, and on a device that was
        never taken, because both of those happen.

        Order matters and is the reason this cannot simply be a close:
        the LED and repeat writes need the descriptor, and the grab goes
        with it.
        """
        # Hand the LEDs back the way the console had them; the phone's
        # caps state is no business of a console we no longer own.
        if device.saved_leds is not None:
            device.set_leds(device.saved_leds)
            device.saved_leds = None
        if device.restore_repeat() is not None:
            self.repeat_debt(device.path, None)
        device.close()

    def release_all(self):
        """Give every keyboard back.

        There is no EVIOCGRAB(0) anywhere in this: the kernel drops a
        grab when the file description goes, so closing is the ungrab.
        """
        self.grabbed = False
        self.asleep = True
        for device in self.devices.values():
            self.release(device)

    def close(self):
        self.release_all()
        self.devices = {}
