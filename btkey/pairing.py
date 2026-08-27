# SPDX-License-Identifier: GPL-2.0-only
"""The passkey state machine, driven by BlueZ's agent.

This lives outside btlink because pairing is a console interaction as much
as a Bluetooth one: entering a passkey means reading the keyboard, and the
console has to keep working throughout - a pairing that never completes
must not be able to trap the keyboard with no way out.  The session checks
the console chords before handing a key here, and this only ever sees what
is left.

Which style of pairing happens is not btkey's choice directly.  It falls
out of the SSP association model, which the two devices derive from their
declared IO capabilities and whether either end demands MITM protection.
Declaring DisplayYesNo gets numeric comparison, where the same six digits
appear on both ends and nothing has to be typed - which is the default
because the alternative is a race against a window iOS closes in about
eight seconds.
"""

from gi.repository import GLib

from . import keycodes

# BlueZ agent IO capabilities.  A phone that requires MITM protection for a
# keyboard - iOS does - will refuse "justworks" outright.
CAPABILITIES = {
    "keyboard": "KeyboardOnly",
    "display": "DisplayOnly",
    "confirm": "DisplayYesNo",
    "both": "KeyboardDisplay",
    "justworks": "NoInputNoOutput",
}

# If the phone neither completes nor cancels, do not sit in passkey mode
# swallowing digits forever.
ENTRY_TIMEOUT = 90


class Pairing:
    def __init__(self, link, display, log, announce):
        self.link = link
        self.display = display
        self.log = log
        self.announce = announce
        self.digits = None       # not None while entering a passkey
        self.timeout = None
        self.started = 0
        self.length = 6

    @property
    def active(self):
        return self.digits is not None

    # -- driven by the agent ----------------------------------------------

    def on_request(self, peer, legacy):
        # A phone that retries an aborted pairing sends a second request;
        # without this the first timeout stays alive and later cancels the
        # second, live, entry.
        self.clear()
        self.digits = ""
        self.started = GLib.get_monotonic_time()
        self.length = 4 if legacy else 6
        # The phone gives you only a few seconds, so lead with a bell.
        # BRLTTY monitors the console bell, so it lands immediately, before
        # the reader has had to notice anything on the display.
        self.display.bell()
        self.announce("Passkey. Type the %d digits now." % self.length)
        self.log("pairing with %s; no Enter needed" % peer)
        self.timeout = GLib.timeout_add_seconds(ENTRY_TIMEOUT, self.abandon)

    def on_cancelled(self):
        """The phone gave up.  Say so - silence here is the worst outcome."""
        if not self.active:
            return
        self.clear(answer_the_agent=False)          # Agent.Cancel did it
        self.announce("The phone cancelled the pairing. Try again.")

    def on_display(self, passkey):
        self.announce("Passkey: %06u" % passkey)

    def on_confirm(self, peer, passkey):
        """Numeric comparison - nothing to type, just check and accept."""
        self.display.bell()
        self.announce("Pairing code %06u. Confirm on the phone." % passkey)
        self.log("accepted numeric comparison with %s" % peer)
        return True

    # -- driven by the keyboard -------------------------------------------

    def handle_key(self, keycode, is_press):
        if not is_press:
            return
        digit = keycodes.digit_for(keycode)
        if digit is not None:
            self.digits += str(digit)
            self.log("passkey: %d digit%s"
                     % (len(self.digits),
                        "" if len(self.digits) == 1 else "s"))
            # Submitting on the last digit rather than on Enter: against
            # the phone's clock, one more keystroke is one too many.
            if len(self.digits) >= self.length:
                self.submit()
        elif keycode == keycodes.KEY_BACKSPACE:
            self.digits = self.digits[:-1]
        elif keycode in keycodes.ENTER_KEYS:
            self.submit()
        elif keycode == keycodes.KEY_ESC:
            self.clear()
            self.announce("pairing cancelled")

    def submit(self):
        value = int(self.digits or "0")
        elapsed = (GLib.get_monotonic_time() - self.started) / 1e6
        self.clear(answer_the_agent=False)          # about to answer it
        self.announce("Passkey sent.")
        self.log("passkey answered after %.1f seconds" % elapsed)
        self.link.supply_passkey(value)

    def clear(self, answer_the_agent=True):
        """Leave passkey mode.

        BlueZ is still waiting on an asynchronous RequestPasskey unless
        submit() has answered it, so giving up has to say so.
        """
        was_active = self.active
        self.digits = None
        if self.timeout is not None:
            GLib.source_remove(self.timeout)
            self.timeout = None
        if was_active and answer_the_agent:
            self.link.abandon_passkey()

    def abandon(self):
        """Never stay in passkey mode forever waiting for a Cancel."""
        self.clear()
        self.announce("Passkey entry timed out.")
        return False
