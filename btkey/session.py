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
import time

import dbus
from gi.repository import Gio, GLib

from . import (advertising, btd, btlink, display, evdev, fifo, journal,
               kbmap, keycodes, pairing, probe, typist, __version__)
from .typist import INTERVAL_MS as TYPE_INTERVAL_MS

# How often to check whether our console is still in front.  We notice our
# own switches immediately; this is only for a chvt from somewhere else, and
# for coming back.  Cheap enough to run often, and a slow poll would leak
# the first keystrokes after a switch back into the local shell.
FOREGROUND_POLL_MS = 40

# How long to let the keyboard connection settle before asking the phone
# for its audio channel too.  Asking in the same breath comes back busy.
AUDIO_CONNECT_DELAY = 3

# A keyboard arriving shows up as a node in /dev/input, which we are told
# about rather than going to look.  The node appears before udev has given
# it its ownership and mode, though, so a look the instant it is created
# finds something we are not allowed to open yet: wait out a short quiet
# spell first, which also folds the burst a single keyboard arrives as
# (several nodes, each created and then chmodded) into one look.
DEVICE_SETTLE_MS = 250

# Only for when the kernel will not let us watch the directory at all.
DEVICE_RESCAN_MS = 2000

# How often to recompute how far a sweep has got.  The display only
# repaints when the number actually changes, so a brisk poll costs nothing:
# a minute of typing moves the percentage about sixty times either way.
SWEEP_PROGRESS_MS = 500

# The guardian kills us if the main loop stops running for this long, which
# releases the keyboard grabs.  Generous enough that a stalled outbound
# Bluetooth connect - the only thing here that blocks for seconds - cannot
# trip it.
HEARTBEAT_MS = 2000
WATCHDOG_SECONDS = 15

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
                                           on_debug=self.note)
        self.watches = {}
        self.device_monitor = None
        self.device_settle = None
        self.control_fd = None
        self.sweep_name = None      # not None while a sweep is being typed
        self.sweep_queued = 0
        self.sweep_started = None
        self.sweep_reports = 0
        self.sweep_waiting = 0.0

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

    # -- output ----------------------------------------------------------

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

    def offer_audio(self, peer):
        """Ask the phone to open its audio channel, once, per connection."""
        if self.link.connected and peer == self.link.peer:
            self.log(self.link.connect_audio(peer))
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
        # Read even when backgrounded, or the descriptor stays readable and
        # spins the loop; just do not act on any of it.
        #
        if not self.foreground:
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
        except OSError:
            return True
        if not data:
            return False
        for line in data.decode("utf-8", "replace").split("\n"):
            command = line.strip()
            if command == "learn-layout":
                self.learn_layout()
            elif command.startswith("learn-accents"):
                self.learn_accents(command.split()[1:])
            elif command == "cancel":
                self.cancel_learning()
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

    def type_batch(self, name, steps):
        """Type a labelled probe sequence, reporting how far it has got.

        A sweep takes a minute, during which the instruction is not to
        touch the keyboard - so it has to say when it is finished, or there
        is nothing to do but guess.  The bell is the part that matters:
        BRLTTY monitors it, so the end arrives without having to watch for
        it.  The running percentage is for reassurance in between.
        """
        # Every keystroke here is a position, never text.  Going through
        # the console keymap would put the one mapping this exists to
        # measure in the middle of measuring it, and garble the capture on
        # any machine whose console does not match the phone.
        self.typist.enqueue(steps)

        self.sweep_name = name
        self.sweep_queued = len(self.typist.queue)
        self.sweep_started = time.monotonic()
        self.sweep_reports = self.link.sent_reports
        self.sweep_waiting = self.link.send_seconds
        self.announce("%s: about %d seconds; do not type until the bell"
                      % (name,
                         max(1, len(self.typist.queue) * TYPE_INTERVAL_MS
                             // 1000)))
        GLib.timeout_add(SWEEP_PROGRESS_MS, self.poll_sweep)

    def poll_sweep(self):
        if self.sweep_name is None:
            return False
        if not self.link.connected:
            # drain() empties the queue on a disconnect, which would
            # otherwise read as completion - bell and all - and send
            # someone off to mail a capture that stops halfway.
            self.typist.clear()
            self.finish_sweep("%s stopped: the phone disconnected"
                              % self.sweep_name)
            return False
        remaining = len(self.typist.queue)
        if remaining:
            done = self.sweep_queued - remaining
            self.display.set_indicator(
                "%d%%" % (100 * done // max(self.sweep_queued, 1)))
            return True
        self.finish_sweep("%s: done. The text is on the phone; send it to "
                          "yourself." % self.sweep_name)
        return False

    def cancel_learning(self):
        if self.sweep_name is None:
            # Nothing to cancel is not the same as cancel everything: a
            # paste may well be draining.
            self.log("nothing to cancel")
            return
        dropped = self.typist.clear()
        self.finish_sweep("%s cancelled, %d keystrokes dropped"
                          % (self.sweep_name, dropped))

    def finish_sweep(self, message):
        self.log(self.sweep_timing())
        self.sweep_name = None
        self.sweep_queued = 0
        self.sweep_started = None
        # Put the lock indicator back; the percentage was borrowing its slot.
        self.display.set_indicator(btlink.led_indicator(self.leds))
        self.display.bell()
        self.announce(message)

    def sweep_timing(self):
        """How long the probe took, and how much of it was the radio.

        A probe that runs slower than the estimate has two possible
        reasons and they call for different things.  If the time went into
        send(), the link is the limit: the socket blocks, so a phone that
        cannot absorb reports as fast as btkey produces them stops the main
        loop for as long as it takes, and sharing the link with A2DP audio
        is enough to do it.  If it did not, the limit is here.
        """
        if self.sweep_started is None:
            return "sweep finished"
        elapsed = time.monotonic() - self.sweep_started
        reports = self.link.sent_reports - self.sweep_reports
        waiting = self.link.send_seconds - self.sweep_waiting
        estimate = self.sweep_queued * TYPE_INTERVAL_MS / 1000.0
        return ("%d reports in %.1fs (estimated %.1fs); %.1fs of that "
                "waiting on the link" % (reports, elapsed, estimate, waiting))

    def learn_accents(self, specs):
        """Type the second probe: every candidate accent key, composed.

        A single-keystroke probe cannot see a composition, because a dead
        key followed by the space it types looks exactly like a literal
        accent.  So a second pass is unavoidable - and which keys it should
        try is decided by the *first* pass's results, which live on the
        phone.  The client works that out from the capture and sends the
        list, which is why this takes one rather than reading a file.
        """
        if not self.link.connected:
            self.log("not connected; nothing to learn from")
            return
        candidates = []
        for spec in specs:
            keycode, _, mods = spec.partition(":")
            try:
                keycode, mods = int(keycode), int(mods or 0)
            except ValueError:
                self.log("ignoring malformed accent key %r" % spec)
                continue
            # Straight off the control FIFO and into a HID report.
            if not 0 <= keycode <= 0xFFFF or not 0 <= mods <= 0xFF:
                self.log("ignoring out-of-range accent key %r" % spec)
                continue
            candidates.append((keycode, mods, ""))
        if not candidates:
            self.log("no accent keys given; run btkey --learn-accents "
                     "with the capture from --learn-layout")
            return
        self.type_batch("learning accent keys",
                        probe.compose_strokes(candidates))

    def learn_layout(self):
        """Type the first probe: every key position, at every level."""
        if not self.link.connected:
            self.log("not connected; nothing to learn from")
            return
        self.type_batch("learning the keyboard layout",
                        probe.capture_strokes())

    # -- devices -----------------------------------------------------------

    def watch_device(self, device):
        self.watches[device.path] = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, device.fd,
            GLib.IOCondition.IN, self.on_device_input, device)

    def drop_device(self, device):
        source = self.watches.pop(device.path, None)
        if source is not None:
            try:
                GLib.source_remove(source)
            except (ValueError, GLib.Error):
                pass
        self.keyboards.forget(device)

    def watch_for_devices(self):
        """Ask to be told when a keyboard is plugged in or pulled out.

        A keyboard arrives or leaves perhaps once in a session, and
        finding out by looking costs a directory listing and a fresh
        look at every node in it, every time.
        """
        try:
            directory = Gio.File.new_for_path(evdev.DEVICE_DIRECTORY)
            self.device_monitor = directory.monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
        except GLib.Error as exc:
            self.log("cannot watch %s (%s); looking every %d ms instead"
                     % (evdev.DEVICE_DIRECTORY, exc.message,
                        DEVICE_RESCAN_MS))
            GLib.timeout_add(DEVICE_RESCAN_MS, self.rescan_devices)
            return
        self.device_monitor.connect("changed", self.device_directory_changed)

    def device_directory_changed(self, monitor, node, other, event):
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
        self.rescan_devices()
        if self.foreground:
            # It arrived while we have the screen, so it is ours to take;
            # otherwise the switch back does this.
            self.keyboards.grab_all()
        return False

    def rescan_devices(self):
        added, removed = self.keyboards.refresh()
        for device in removed:
            self.drop_device(device)
        for device in added:
            self.log("keyboard appeared: %s" % device.name)
            self.watch_device(device)
        if added and self.foreground:
            self.push_leds()
        return True

    # -- console/VT --------------------------------------------------------

    def set_foreground(self, foreground):
        if foreground == self.foreground:
            return
        self.foreground = foreground
        if foreground:
            # What is plugged in can change while another console has
            # the screen, and a keyboard that arrived then was left alone
            # rather than taken away from whoever was using it.
            self.rescan_devices()
            self.keyboards.grab_all()
            self.sync_modifiers()
            self.push_leds()
        else:
            # Release before letting go, or the phone keeps holding Alt.
            self.release_all()
            self.keyboards.ungrab_all()

    def sync_modifiers(self):
        """Adopt the modifiers that are physically held, on taking the grab.

        Holding Alt and walking through consoles with F2, F3, F4 lands back
        here with Alt still down - but its press went to the kernel while we
        were ungrabbed, so we never saw it.  Without asking the devices what
        is held, the next Alt+Fn looks like a bare Fn and goes to the phone
        instead of switching console.
        """
        modifiers = 0
        for keycode in self.keyboards.held_keys():
            modifiers |= keycodes.MODIFIERS.get(keycode, 0)
        self.modifiers = modifiers

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
                self.watch_device(device)
            self.announce("btkey ready on VT %d. Alt+F1 to F12 switches "
                          "console, Alt+Escape quits."
                          % self.consoles.vt)
            if self.link.last_host():
                self.log("paired host on record: %s" % self.link.last_host())

            self.typist.load_keymap(self.consoles.fd)
            self.typist.watch_stdin()
            if self.options.control_fifo:
                self.open_control_fifo(self.options.control_fifo)

            self.poll_foreground()
            GLib.timeout_add(FOREGROUND_POLL_MS, self.poll_foreground)
            self.watch_for_devices()
            if self.guardian is not None:
                GLib.timeout_add(HEARTBEAT_MS, self.heartbeat)
                self.guardian.watch_me(WATCHDOG_SECONDS)
            self.install_signals()
            self.loop.run()
        except (btlink.ProfileNotAvailable, btd.BluetoothdError,
                dbus.DBusException) as exc:
            self.startup_error = describe(exc)
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


def describe(exc):
    if isinstance(exc, dbus.DBusException):
        return "%s: %s" % (exc.get_dbus_name(),
                           exc.get_dbus_message() or "no detail given")
    return str(exc) or exc.__class__.__name__
