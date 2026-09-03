# SPDX-License-Identifier: GPL-2.0-only
"""Main loop: grabbed keystrokes in, HID reports out.

Forwarding is tied to the foreground virtual terminal.  When our console is
in front we hold an EVIOCGRAB on every keyboard, so nothing else on the
machine sees a key; when it is not, we let go entirely and Linux behaves
normally.  Switching consoles is therefore the whole on/off control, with
no mode to remember.

Because a grabbed keyboard never reaches the kernel's own handler, the VT
switch chords have to be reimplemented here - Alt+Fn, and Alt+Esc to
quit.  Everything else is forwarded untouched, which is what keeps
VoiceOver's Ctrl+Option chords working.

The rest of the work lives in collaborators this owns, each split out
because it has state of its own worth keeping separate:

  journal      the log file, and stderr folded into it
  pairing      the passkey state machine
  typist       pasted text, which arrives as characters rather than keys
  advertising  the class of device, and noticing when it moves
  sweep        typing a probe at the phone, over the minute it takes

It owns the display, the link, the keyboards, the consoles, the private
bluetoothd and the guardian as well, but those are things it drives
rather than work it handed over.

Only two things stayed here that might look like they belong elsewhere.
Lock state is inferred from keys on their way out, because iOS never
reports it, so it needs the forwarding path.  And the console chords are
checked before pairing sees a key, so a pairing that never completes
cannot trap the keyboard.
"""

import os
import pwd
import signal
import sys

from gi.repository import Gio, GLib

from . import (advertising, btd, btlink, display, evdev, fifo, journal,
               keycodes, pairing, sweep, typist, vt, __version__)

# Only for a kernel without the sysfs attribute the console change is
# waited on: btkey notices its own switches without being told, but not a
# chvt from somewhere else, and until it does it still holds the keyboard
# the other console is being typed at.  Hence a brisk fallback.
FOREGROUND_POLL_MS = 40

# How long to let the keyboard connection settle before asking the phone
# for its audio channel too.  Asking in the same breath comes back busy,
# which is the only reason to wait at all - so ask soon and ask again,
# rather than guessing at a delay long enough to have been safe.  Doubling
# from one second, four times over, is attempts at 1, 3, 7 and 15 seconds.
AUDIO_CONNECT_DELAY = 1
AUDIO_RETRIES = 4

# A keyboard arriving shows up as a node in /dev/input, which we are told
# about rather than going to look.  Wait for the arrivals to stop before
# looking, restarting the wait at every one, for three reasons in
# ascending order of how long they take.
#
# The node appears before udev has given it its ownership and mode, so a
# look the instant it is created finds something we are not allowed to
# open.  One keyboard is a burst of several nodes, each created and then
# chmodded, and is worth one look rather than six.
#
# And whatever else on the machine wants this keyboard should have it
# first.  A program that takes it for its own hotkeys is not competing
# with btkey - it publishes what it does not want through uinput, and
# that loopback is the device btkey should be holding.  BRLTTY does
# exactly this.  Looking too soon means grabbing the real keyboard out
# from under it, or finding the loopback has not been created yet and
# missing it until the next switch.  A second is nothing against a
# keyboard being plugged in, and it is the loopback appearing that ends
# the wait in any case.
DEVICE_SETTLE_MS = 1000

# Only for when the kernel will not let us watch the directory at all.
DEVICE_RESCAN_MS = 2000

# The guardian kills us if the main loop stops running for this long, which
# releases the keyboard grabs.  Generous enough that a stalled outbound
# Bluetooth connect - the only thing here that blocks for seconds - cannot
# trip it.
HEARTBEAT_MS = 5000
WATCHDOG_SECONDS = 10

# Rollover: the boot report can name six keys, and the convention when more
# are down is to fill every slot with ErrorRollOver.
ERROR_ROLLOVER = 0x01

# Lock keys, and the HID LED report bit each one owns.  iOS reports the
# lock state with an LED report and that wins outright; these are for
# inferring it from the keys we forwarded, for a host that does not.
LOCK_KEYS = {keycodes.KEY_CAPSLOCK: 0x02,
             keycodes.KEY_NUMLOCK: 0x01,
             keycodes.KEY_SCROLLLOCK: 0x04}


class Session:
    def __init__(self, options, consoles, keeper=None):
        self.options = options
        self.loop = GLib.MainLoop()
        self.consoles = consoles
        self.guardian = keeper
        self.btd = None
        self.keyboards = evdev.KeyboardSet(options.device, on_event=self.log,
                                           on_debug=self.note,
                                           on_repeat_debt=self.repeat_debt)
        self.watches = {}
        self.device_monitor = None
        self.device_settle = None
        self.device_timer = None    # the fallback, where there is no watch
        self.waiting_for_release = False
        self.heartbeat_timer = None
        self.control_fd = None

        self.display = display.Display()
        self.journal = journal.Journal(options.log_file,
                                       on_error=self.display.log)

        # Filled in on connection when the setting is auto, since the
        # answer depends on which host connected.
        self.top_row = (keycodes.TOP_ROW_MEDIA
                        if options.top_row == "media" else {})

        self.pressed = []        # non-modifier keycodes, in press order
        self.modifiers = 0
        self.foreground = False
        self.leds = 0
        self.leds_from_host = False
        self.quit_requested = False
        self.exit_code = 0
        self.startup_error = None

        self.link = btlink.BluetoothHID(
            name=options.name,
            on_event=self.log,
            on_state=self.on_connection_state,
            on_leds=self.on_host_leds,
            on_passkey=self.on_passkey_request,
            on_passkey_cancel=self.on_passkey_cancelled,
            on_passkey_display=self.on_passkey_display,
            on_confirm=self.on_confirm,
            capability=pairing.CAPABILITIES[options.pairing],
            debug=options.debug,
            adapter=options.adapter,
        )
        self.pairing = pairing.Pairing(self.link, self.display,
                                       self.log, self.announce)
        self.typist = typist.Typist(self.link, self.log,
                                    lambda: self.foreground,
                                    self.send_keyboard,
                                    shift_newline=options.shift_newline,
                                    layout_path=options.phone_layout)
        self.advertising = advertising.Advertising(
            self.link, options, self.log, self.announce, self.journal.record)
        self.sweep = sweep.Sweep(self.typist, self.link, self.display,
                                 self.log, self.announce)

    # -- output ----------------------------------------------------------

    def repeat_debt(self, path, repeat):
        """A keyboard's key repeat needs putting back, or no longer does.

        The setting belongs to the device and outlives us, so a btkey
        killed rather than stopped would leave a keyboard typing one
        character however long a key is held.  The guardian is already
        there for exactly this kind of debt - and has to be told when
        one is settled, or it would put back a stale setting over
        whatever has been done since.
        """
        if self.guardian is None:
            return
        if repeat is None:
            self.guardian.forget_repeat(path)
        else:
            self.guardian.restore_repeat_on_death(path, *repeat)

    def note(self, message):
        """Detail that is only wanted when something is being chased."""
        if self.options.debug:
            self.log(message)

    def log(self, message):
        """Routine progress.  Scrolls past; nothing has to read it."""
        self.journal.record(message)
        self.display.log("btkey: " + message)

    def announce(self, message):
        """Something worth reading: the fixed status line, under the cursor.

        BRLTTY tracks the cursor, so parking it on the first character of
        the reserved line is what puts the braille display on the message.
        """
        self.journal.record("* " + message)
        self.display.status("btkey: " + message)

    # -- callbacks from the Bluetooth side --------------------------------

    def on_connection_state(self, connected, peer):
        if connected:
            self.announce("connected to %s" % peer)
            # A new link tells us nothing about the phone's lock state, so
            # start from off rather than carrying a stale guess over.
            self.leds = 0
            self.display.set_indicator("")
            self.choose_top_row(peer)
            if self.options.audio:
                # Not immediately: the phone has just finished bringing up
                # the keyboard and asking for a second profile in the same
                # breath gets refused as busy.
                GLib.timeout_add_seconds(AUDIO_CONNECT_DELAY, self.offer_audio,
                                         peer)
        else:
            self.announce("disconnected")
            self.release_all()
            # The phone's caps state stops being ours to show the moment
            # the link goes; give the console its own LEDs back.
            self.keyboards.restore_leds()
            self.display.set_indicator("")
            self.leds = 0

    def choose_top_row(self, peer):
        """Decide what the function row sends, if it was left to us.

        An Apple host acts on the consumer usages its own top row sends and
        does nothing with F1 to F12; anything else is likelier to want the
        function keys.  The host says which it is in its Device ID record,
        so this is asking rather than guessing from a name.
        """
        if self.options.top_row != "auto":
            return
        vendor = self.link.host_vendor(peer)
        apple = vendor == self.link.APPLE_VENDOR
        self.top_row = keycodes.TOP_ROW_MEDIA if apple else {}
        if vendor is None:
            self.log("host publishes no vendor; function row sends F1 to F12")
        else:
            self.log("host vendor 0x%04X%s; function row sends %s"
                     % (vendor, " (Apple)" if apple else "",
                        "media keys" if apple else "F1 to F12"))

    def offer_audio(self, peer, wait=AUDIO_CONNECT_DELAY,
                    left=AUDIO_RETRIES):
        """Ask the phone to open its audio channel, per connection.

        Being told the phone is busy is not an answer, it is a request to
        come back; the single attempt this used to make left the machine
        advertising somewhere to send sound with nothing ever asking for
        it, and nothing saying why.
        """
        if not (self.link.connected and peer == self.link.peer):
            return False
        message, again = self.link.connect_audio(peer)
        if again and left:
            self.note("%s; asking again in %ds" % (message, wait * 2))
            GLib.timeout_add_seconds(wait * 2, self.offer_audio, peer,
                                     wait * 2, left - 1)
            return False
        self.log(message)
        return False

    def on_passkey_request(self, peer, legacy):
        self.pairing.on_request(peer, legacy)

    def on_passkey_cancelled(self):
        self.pairing.on_cancelled()

    def on_passkey_display(self, passkey):
        self.pairing.on_display(passkey)

    def on_confirm(self, peer, passkey):
        return self.pairing.on_confirm(peer, passkey)

    # -- lock state --------------------------------------------------------

    def on_host_leds(self, mask):
        """A real LED report from the phone.  Authoritative from here on.

        It beats anything inferred, so inference stops for the rest of the
        session.  iOS does send these, once the LED output item in the
        report descriptor is one it can parse - see hidspec.py.
        """
        if not self.leds_from_host:
            self.leds_from_host = True
            self.log("the phone reports LED state; no longer inferring it")
        self.apply_leds(mask)

    def apply_leds(self, mask):
        self.leds = mask
        self.keyboards.set_leds(mask)
        self.display.set_indicator(btlink.led_indicator(mask))
        self.log("lock keys: %s" % (btlink.led_names(mask) or "all off"))

    def push_leds(self):
        """Put the phone's lock state back onto the keyboards.

        Releasing the grab hands the LEDs to the console, which is right
        while another console is in front: the phone's caps state is no
        business of a console we do not own.  But nothing was claiming
        them back, so returning here - or plugging a keyboard in while we
        hold the grab - left the lights showing the console's state while
        the status line showed the phone's.
        """
        self.keyboards.set_leds(self.leds)

    def note_lock_key(self, keycode):
        """Follow a lock key we just forwarded, since nothing tells us.

        A HID host owns the lock state and reports it back with an LED
        report.  For one that does not, the state is still knowable: a lock
        only changes when its key is pressed, and every one of those passes
        through here.  Wrong only if the phone was already holding a lock
        when we connected, and one press corrects that.
        """
        if self.leds_from_host or keycode not in LOCK_KEYS:
            return
        self.apply_leds(self.leds ^ LOCK_KEYS[keycode])

    # -- key handling ------------------------------------------------------

    def on_device_input(self, fd, condition, device):
        events = device.read_keys()
        if events is None:
            self.log("%s went away" % device.name)
            self.drop_device(device)
            return False
        # The descriptors are given up on the way out, so ordinarily
        # nothing arrives from the background at all.  This is for the
        # event GLib had already queued when the console changed.
        if not self.foreground:
            return True
        if not device.grabbed:
            # Not ours.  The console has these keys and is acting on
            # them, so forwarding them would send the phone the second
            # of two - the first arriving as text on stdin.  The only
            # thing worth reading from a keyboard we do not hold is
            # that the last key has come up and it can be taken.
            if self.waiting_for_release and not self.keyboards.held_keys():
                self.take_keyboards()
            return True
        for keycode, is_press in events:
            self.handle_key(keycode, is_press)
        return True

    def handle_key(self, keycode, is_press):
        # Modifier state is tracked in every mode, because the console
        # chords below have to work during passkey entry too.
        if keycode in keycodes.MODIFIERS:
            bit = keycodes.MODIFIERS[keycode]
            if is_press:
                self.modifiers |= bit
            else:
                self.modifiers &= ~bit
            if not self.pairing.active:
                self.send_keyboard()
            return

        # Alt is the console command prefix.  Checked before the pairing
        # branch on purpose: a pairing that never completes must not be
        # able to swallow the keyboard with no way out.
        alt_held = self.modifiers & (keycodes.MOD_LEFTALT
                                     | keycodes.MOD_RIGHTALT)
        ctrl_held = self.modifiers & (keycodes.MOD_LEFTCTRL
                                      | keycodes.MOD_RIGHTCTRL)
        if is_press and alt_held:
            # Ctrl+Alt+Fn is how a Linux console has always been switched,
            # so that one keeps working with Ctrl held or not.
            if keycode in keycodes.FUNCTION_KEYS:
                self.switch_vt(keycodes.FUNCTION_KEYS[keycode])
                return
            # Escape has no such tradition, and Ctrl+Option is VoiceOver's
            # own modifier, so Ctrl+Alt+Escape goes to the phone with the
            # rest of that family and quitting is Alt+Escape alone.
            if keycode == keycodes.KEY_ESC and not ctrl_held:
                self.quit("Alt+Esc")
                return

        if self.pairing.active:
            self.pairing.handle_key(keycode, is_press)
            return

        # After the console chords, so Alt+F4 still switches console, and
        # before everything else, so the rest of the way is the path a
        # keyboard's own media keys already take.
        keycode = self.top_row.get(keycode, keycode)

        if keycode in keycodes.CONSUMER:
            self.send_consumer(keycodes.CONSUMER[keycode] if is_press else 0)
            return

        if keycode not in keycodes.KEYBOARD:
            return

        if is_press:
            if keycode not in self.pressed:
                self.pressed.append(keycode)
        elif keycode in self.pressed:
            self.pressed.remove(keycode)
        self.send_keyboard()
        if is_press and self.link.connected:
            self.note_lock_key(keycode)

    # -- report generation --------------------------------------------------

    def send_keyboard(self):
        if not self.link.connected:
            self.maybe_reconnect()
            return
        if len(self.pressed) > 6:
            slots = [ERROR_ROLLOVER] * 6
        else:
            slots = [keycodes.KEYBOARD[code] for code in self.pressed]
        self.link.send_keyboard(self.modifiers, slots)

    def send_consumer(self, usage):
        if not self.link.connected:
            self.maybe_reconnect()
            return
        self.link.send_consumer(usage)

    def release_all(self):
        """Drop every held key, so nothing sticks down on the phone."""
        self.pressed = []
        self.modifiers = 0
        if self.link.connected:
            self.link.send_keyboard(0, [])
            # A consumer usage is a separate report and stays held until
            # its own zero: let go of the volume key while the console is
            # switched away and the phone goes on ramping.
            self.link.send_consumer(0)

    def maybe_reconnect(self):
        if self.options.no_reconnect:
            return
        self.link.reconnect()

    # -- the layout sweep --------------------------------------------------

    def on_control(self, fd, condition):
        """Commands, as opposed to text to type."""
        try:
            data = os.read(fd, 4096)
        except OSError as exc:
            if not fifo.keep_watching(exc):
                self.log("control channel failed: %s" % exc.strerror)
                return False
            return True
        if not data:
            return False
        for line in data.decode("utf-8", "replace").split("\n"):
            command = line.strip()
            if command == "learn-layout":
                self.sweep.learn_layout()
            elif command.startswith("learn-accents"):
                self.sweep.learn_accents(command.split()[1:])
            elif command == "cancel":
                self.sweep.cancel()
            elif command == "quit":
                self.quit("asked to")
            elif command:
                self.log("unknown control command: %s" % command)
        return True

    def open_control_fifo(self, path):
        self.control_fd = fifo.make(path, self.log)
        if self.control_fd is None:
            return
        GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.control_fd,
                              GLib.IOCondition.IN, self.on_control)
        self.log("listening for commands on %s" % path)

    # -- devices -----------------------------------------------------------

    def watch_device(self, device):
        if device.path in self.watches:
            return
        self.watches[device.path] = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, device.fd,
            GLib.IOCondition.IN, self.on_device_input, device)

    def unwatch_device(self, device):
        """Stop waking for this one.  Always before closing its fd."""
        source = self.watches.pop(device.path, None)
        if source is not None:
            try:
                GLib.source_remove(source)
            except (ValueError, GLib.Error):
                pass

    def drop_device(self, device):
        self.unwatch_device(device)
        self.keyboards.forget(device)
        self.keyboard_gone()

    def keyboard_gone(self):
        """Tell the phone what is still down, a keyboard having gone.

        Unplugging one is covered without this, by the kernel: it sends
        key-ups for everything the device was holding before it goes,
        and evdev hands a reader those buffered events before it reports
        the device missing, so they arrive here as ordinary releases and
        are forwarded like any other.

        A keyboard dropped for some other reason sends nothing at all,
        and a read that fails on one still sitting there is enough to
        drop it.  The phone would go on holding whatever was down, with
        no key left anywhere able to lift it.  So what is still held is
        asked of the keyboards that remain.

        A consumer usage cannot be asked after - it is a report rather
        than a key, and nothing here records that one is down - so it is
        simply let go.  Cutting a volume key short is the safe way to be
        wrong about it; the other way ramps until the phone is unpaired.
        """
        still = self.keyboards.held_keys()
        self.pressed = [code for code in self.pressed if code in still]
        self.sync_modifiers()
        self.send_keyboard()
        self.send_consumer(0)

    def sleep_devices(self):
        """Let the keyboards go while another console has them.

        Ungrabbing is not enough.  An open device still delivers
        everything typed on it, so btkey wakes for every keystroke meant
        for somebody else, only to throw it away.
        """
        for device in self.keyboards.devices.values():
            self.unwatch_device(device)
        self.keyboards.release_all()

    def wake_devices(self):
        """Open them again, and drop the ones that are no longer there."""
        for device in self.keyboards.open_all():
            self.log("%s went away" % device.name)
            self.drop_device(device)

    def take_keyboards(self):
        """Grab what will come, and look again if we lost one we had.

        A keyboard that was ours and is now somebody else's says the
        picture has changed since the last look: whatever took it may
        have published a loopback for the keys it does not want, which
        is the device btkey should be holding instead.  BRLTTY does
        exactly that when it is set up mid-session.

        The second pass cannot start a third.  Everything discarded is
        forgotten first, so anything refused on the way round again was
        never held by us, and it is being held that makes a refusal
        worth looking around after.
        """
        if self.keyboards.held_keys():
            # Not while anything is down.  Grabbing now takes the key
            # away before the console sees it released, and a console
            # that believes Ctrl is still held turns every letter into
            # a control character - r into ^R, and bash into reverse
            # search.  Ctrl+Alt+Fn is how this console is reached, so
            # that is the ordinary case rather than a corner.
            #
            # There is no deadline on the wait and none is wanted.
            # Until btkey grabs, the keyboard is exactly what it was:
            # the console handles it, Alt+Fn still switches, Ctrl+C
            # still interrupts, and what is typed still reaches the
            # phone, arriving here as text on stdin instead of as key
            # positions.  A key that is never released costs the exact
            # reporting of positions and nothing more.
            if not self.waiting_for_release:
                self.waiting_for_release = True
                self.note("waiting for the keys to come up before "
                          "taking the keyboard")
            self.watch_every_device()
            return
        self.waiting_for_release = False

        if self.keyboards.grab_all():
            self.keyboards.discard_refusals()
            self.rescan_devices()
            self.keyboards.grab_all()
        self.watch_held_devices()

        # Both of these are about the keyboards just taken, so they
        # belong to the taking and not to the switch that asked for it.
        # A take that waited for a key to come up happens later, from
        # the input path: run at the switch, the first would adopt the
        # very key being waited on - Alt, most of the time, that being
        # how the console was reached - and hold it down for the rest of
        # the session, while the second would write the phone's lock
        # state to nothing at all.
        self.sync_modifiers()
        self.push_leds()

    def watch_every_device(self):
        """Watch them all, held or not, to see the last key come up.

        Nothing read here is forwarded: the console has these keys and
        is acting on them, and forwarding as well would send the phone
        what stdin is about to send it anyway.
        """
        for device in self.keyboards.devices.values():
            self.watch_device(device)

    def watch_held_devices(self):
        """Wake for the keyboards we hold, and for no others.

        There is nothing in between: a keyboard is one btkey has the
        grab on, or it is one btkey has let go of and will try again
        later.  Watching anything else would wake us for keys that
        either never arrive or arrive twice.
        """
        for device in self.keyboards.devices.values():
            if device.grabbed:
                self.watch_device(device)
            else:
                self.unwatch_device(device)

    def watch_for_devices(self):
        """Ask to be told when a keyboard is plugged in or pulled out.

        A keyboard arrives or leaves perhaps once in a session, and
        finding out by looking costs a directory listing and a fresh
        look at every node in it, every time.

        Only while our console is in front.  What is plugged in while
        another console has the screen is that console's business, and we
        look afresh on the way back in any case.
        """
        if self.device_monitor is not None or self.device_timer is not None:
            return
        try:
            directory = Gio.File.new_for_path(evdev.DEVICE_DIRECTORY)
            self.device_monitor = directory.monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
        except GLib.Error as exc:
            self.log("cannot watch %s (%s); looking every %d ms instead"
                     % (evdev.DEVICE_DIRECTORY, exc.message,
                        DEVICE_RESCAN_MS))
            self.device_timer = GLib.timeout_add(DEVICE_RESCAN_MS,
                                                 self.rescan_devices)
            return
        self.device_monitor.connect("changed", self.device_directory_changed)

    def unwatch_for_devices(self):
        """Stop being told, and forget any change still settling."""
        if self.device_monitor is not None:
            self.device_monitor.cancel()
            self.device_monitor = None
        if self.device_timer is not None:
            GLib.source_remove(self.device_timer)
            self.device_timer = None
        if self.device_settle is not None:
            GLib.source_remove(self.device_settle)
            self.device_settle = None

    def device_directory_changed(self, monitor, node, other, event):
        if self.device_monitor is None:
            # Cancelled while this one was already on its way to us.
            return
        if event not in (Gio.FileMonitorEvent.CREATED,
                         Gio.FileMonitorEvent.DELETED,
                         Gio.FileMonitorEvent.ATTRIBUTE_CHANGED):
            return
        if self.device_settle is not None:
            GLib.source_remove(self.device_settle)
        self.device_settle = GLib.timeout_add(DEVICE_SETTLE_MS,
                                              self.settle_devices)

    def settle_devices(self):
        self.device_settle = None
        # Something has changed in /dev/input, so what btkey decided
        # about the keyboards it is not holding was decided about a
        # different machine.  Look at them afresh.
        self.keyboards.discard_refusals()
        self.rescan_devices()
        if self.foreground:
            # It arrived while we have the screen, so it is ours to take;
            # otherwise the switch back does this.
            self.take_keyboards()
        return False

    def rescan_devices(self):
        added, removed = self.keyboards.refresh()
        for device in removed:
            self.drop_device(device)
        for device in added:
            self.log("keyboard appeared: %s" % device.name)
        if added and self.foreground:
            self.push_leds()
        return True

    # -- console/VT --------------------------------------------------------

    def set_foreground(self, foreground):
        if foreground == self.foreground:
            return
        self.foreground = foreground
        if foreground:
            # The watch goes on before the look, never after.  A keyboard
            # plugged in between the two would otherwise fall through the
            # gap: too late for the scan, too early for a watch that did
            # not exist yet, and unnoticed until the next switch.
            self.watch_for_devices()
            self.wake_devices()
            self.rescan_devices()
            self.take_keyboards()
            self.watch_myself()
        else:
            # And comes off before the keyboards go, for the same reason
            # the other way round: an arrival reported after we have let
            # go would have us open and grab a device on a console that
            # is no longer ours.
            self.unwatch_for_devices()
            # Release before letting go, or the phone keeps holding Alt,
            # and hand the LEDs back before the descriptors go.
            self.waiting_for_release = False
            self.release_all()
            self.sleep_devices()
            self.unwatch_myself()

    def sync_modifiers(self):
        """Adopt the modifiers that are physically held, on taking the grab.

        Holding Alt and walking through consoles with F2, F3, F4 lands
        back here with Alt still down - but its press went to the kernel
        while we were ungrabbed, so we never saw it.  Without asking the
        devices what is held, the next Alt+Fn looks like a bare Fn and
        goes to the phone instead of switching console.

        Since btkey waits for the keys to come up before taking a
        keyboard, the answer at that moment is nearly always nothing,
        and this is the clearing of whatever was left over.  Asking is
        still the right way to arrive at that: a key pressed in the
        breath between the check and the grab is held, and known about
        here rather than stuck.
        """
        modifiers = 0
        for keycode in self.keyboards.held_keys():
            modifiers |= keycodes.MODIFIERS.get(keycode, 0)
        self.modifiers = modifiers

    def watch_foreground(self):
        """Ask to be told when the console in front changes.

        Asking instead costs a wakeup and an ioctl 25 times a second for
        as long as btkey runs, whatever is or is not happening.
        """
        fd = self.consoles.watch()
        if fd is None:
            self.ask_for_the_console("cannot watch %s" % vt.ACTIVE_ATTRIBUTE)
            return
        GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, fd,
            GLib.IOCondition.PRI | GLib.IOCondition.ERR,
            self.console_changed)

    def console_changed(self, fd, condition):
        if condition & GLib.IOCondition.PRI and self.consoles.rearm():
            return self.poll_foreground()

        # A descriptor in error is reported ready for ever, so saying
        # "carry on" here would spin the loop on it for the rest of the
        # run.  BRLTTY had to fix exactly this in its own monitor, twice:
        # once for the error arriving instead of the event, and once for
        # a callback that kept the monitor alive through it.  Note that
        # the ordinary notification is POLLPRI and POLLERR together, so
        # only an error without the event counts as one.
        self.ask_for_the_console("the console watch failed")
        self.poll_foreground()
        return False

    def ask_for_the_console(self, why):
        """Fall back to asking which console is in front.

        Both ways of losing the watch end here, so the interval and the
        wording are settled in one place.
        """
        self.log("%s; asking every %d ms instead" % (why, FOREGROUND_POLL_MS))
        GLib.timeout_add(FOREGROUND_POLL_MS, self.poll_foreground)

    def poll_foreground(self):
        self.set_foreground(self.consoles.is_foreground())
        return True

    def switch_vt(self, target):
        if target == self.consoles.vt:
            return
        self.set_foreground(False)
        if self.consoles.switch_to(target):
            self.log("switched to VT %d" % target)

    # -- lifecycle ---------------------------------------------------------

    def heartbeat(self):
        self.guardian.heartbeat()
        return True

    def watch_myself(self):
        """Ask the guardian to kill us if the loop stops turning.

        Only while our console is in front, because that is the only time
        we hold anybody's keyboard.  Backgrounded, a wedged btkey is a
        process that does nothing rather than a machine that cannot be
        typed at, and there is nothing for a SIGKILL to release.
        """
        if self.guardian is None or self.heartbeat_timer is not None:
            return
        # No beat here: arming is itself a message, and the guardian
        # counts from the last one it read.  The first timed beat lands
        # well inside the deadline.
        self.guardian.watch_me(WATCHDOG_SECONDS)
        self.heartbeat_timer = GLib.timeout_add(HEARTBEAT_MS, self.heartbeat)

    def unwatch_myself(self):
        if self.guardian is None or self.heartbeat_timer is None:
            return
        GLib.source_remove(self.heartbeat_timer)
        self.heartbeat_timer = None
        self.guardian.watch_me(0)      # stand down until we are back

    def quit(self, reason=""):
        if self.quit_requested:
            return True
        self.quit_requested = True
        self.announce("btkey stopping%s" % (": " + reason if reason else ""))
        self.loop.quit()
        return True

    def start_services(self):
        if not self.options.system_bluetoothd:
            self.btd = btd.ManagedBluetoothd(
                self.options.device_class, on_event=self.log,
                guardian=self.guardian, audio=self.options.audio)
            self.btd.start()
        self.link.start()
        self.advertising.start()

    def stop_services(self):
        """Undo start_services, as far as each piece can be undone.

        Guarded piece by piece: the whole point is that we get here after a
        failure, and one broken teardown must not stop the next one from
        running.  Leaving the machine without a bluetoothd is the outcome
        this exists to prevent.
        """
        try:
            self.link.stop()
        except Exception as exc:                        # noqa: BLE001
            self.log("error shutting down the Bluetooth link: %s" % exc)
        if self.btd is not None:
            try:
                self.btd.stop()
            except Exception as exc:                    # noqa: BLE001
                self.log("error restoring bluetoothd: %s" % exc)
            self.btd = None
        self.advertising.stop()

    def install_signals(self):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        # g_unix_signal_add accepts only SIGHUP, SIGINT, SIGTERM, SIGUSR1,
        # SIGUSR2 and SIGWINCH.  Anything else fails an assertion, returns a
        # NULL source, and prints five warnings straight to stderr - which,
        # with the cursor parked on the status line, is where they appear.
        # SIGQUIT is not worth chasing separately: the guardian covers what
        # we cannot catch.
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum,
                                 self.quit, "signal")

    def run(self):
        self.keyboards.refresh()
        if not self.keyboards.devices:
            sys.stderr.write("btkey: no keyboards found under /dev/input; "
                             "run with --list-devices to see what is there\n")
            return 1

        self.display.start()
        self.journal.open("--- btkey %s starting ---" % __version__)
        if self.display.started:
            self.journal.capture_stderr(
                lambda text: self.log("stderr: " + text))
        # Everything from here is inside one try/finally.  The console has
        # a scrolling region now, and stderr is redirected into a pipe that
        # only the main loop drains - so any exit that skips the teardown
        # below leaves the console mangled and says nothing about why,
        # including on the likeliest first-run failure of all: bluetoothd
        # still holding the HID profile.
        try:
            # First thing said, and said on the console rather than only in
            # a file: an installed btkey writes no file, and "which one is
            # running" is the question that costs the most to answer wrong.
            self.log("version %s, started by %s" % (__version__, started_by()))
            self.start_services()
            for device in self.keyboards.devices.values():
                self.log("using keyboard: %s" % device.name)
            self.announce("btkey ready on VT %d. Alt+F1 to F12 switches "
                          "console, Alt+Escape quits."
                          % self.consoles.vt)
            if self.link.last_host():
                self.log("paired host on record: %s" % self.link.last_host())

            self.typist.load_keymap(self.consoles.fd)
            self.typist.watch_stdin()
            if self.options.control_fifo:
                self.open_control_fifo(self.options.control_fifo)

            # These follow the foreground: poll_foreground puts them in
            # place if our console is already in front, which at startup
            # it is, since btkey was just typed at it.
            self.poll_foreground()
            self.watch_foreground()
            self.install_signals()
            self.loop.run()
        except (btlink.ProfileNotAvailable, btlink.LinkError,
                btd.BluetoothdError) as exc:
            self.startup_error = str(exc)
            self.exit_code = 1
        finally:
            # Disarm the watchdog first: heartbeats stopped when the loop
            # did, and the teardown below can outlast it - restarting
            # bluetooth.service alone may take seconds.
            if self.guardian is not None:
                self.guardian.watch_me(0)
            self.release_all()
            self.typist.close()
            if self.control_fd is not None:
                os.close(self.control_fd)
            self.keyboards.close()
            self.stop_services()
            self.journal.release_stderr()
            self.display.close()
            self.journal.close("--- btkey stopping ---")
        # After release_stderr and display.close, so it reaches a terminal
        # that is whole again rather than a pipe nobody is draining.
        if self.startup_error:
            sys.stderr.write("btkey: %s\n" % self.startup_error)
        return self.exit_code


def started_by():
    """The person who started btkey, which is not the user it runs as.

    Under sudo the process is root and the person is not, and the person is
    the useful half: it is whose configuration file was read, whose control
    FIFO the learn commands write to, and who to go and ask when a second
    instance turns out to be running.

    Taken from the same place the FIFO's ownership is, so the name shown is
    the account that can actually drive it.
    """
    owner = fifo.invoking_user()
    uid = owner[0] if owner is not None else os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return "uid %d" % uid



