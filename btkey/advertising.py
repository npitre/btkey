# SPDX-License-Identifier: GPL-2.0-only
"""What this machine tells the world it is, and keeping it that way.

Two facts drive everything here, both learned the hard way.

bluetoothd derives the class of device's service class bits from the
registered profile UUIDs, with a fixed table that maps Headset and
Handsfree to Audio while A2DP Sink and Source map only to Rendering and
Capturing.  So dropping HSP and HFP to be rid of call audio also drops the
Audio bit - the one a phone looks at when deciding whether this is
somewhere sound can go.  Nothing in the D-Bus API exposes those bits, and
main.conf's Class is not honoured either: a run with Class = 0x000540 left
the adapter at the default 0x0c0104.  So the class is written with the HCI
command directly, and put back whenever bluetoothd recomputes it - which it
does on every change to the UUID set, three times in one second during a
WirePlumber restart.

And iOS caches what a device advertises at bond time - the class *and* the
profile set - and never looks again.  A change to either is invisible to an
already-paired phone until it is forgotten and paired afresh.  That cost
three separate rounds of confusion, each looking exactly like a fix that
had not worked, so btkey now notices when the advertised set has moved
since its last run and says so.
"""

import os

from gi.repository import GLib

from . import btd, hidspec

# How often to check that bluetoothd has not overwritten the class.  The
# PropertiesChanged watch is the real mechanism; this is a backstop for a
# change that arrives without a signal.
RECHECK_SECONDS = 5
# Long enough for bluetoothd and WirePlumber to have finished registering
# everything, so the snapshot compared against the last run is the settled
# set rather than whatever existed a moment after startup.
SETTLE_SECONDS = 8
STATE_FILE = "/var/lib/btkey/advertised"


def service_bits(cod):
    """Spell out the class of device's service bits, which are the point."""
    names = {18: "Rendering", 19: "Capturing", 20: "ObjectTransfer",
             21: "Audio", 22: "Telephony", 23: "Information"}
    present = [name for bit, name in sorted(names.items()) if cod & (1 << bit)]
    return " [%s]" % (", ".join(present) or "no service bits")


class Advertising:
    def __init__(self, link, options, log, announce, record):
        self.link = link
        self.options = options
        self.log = log
        self.announce = announce
        self.record = record
        self.cod = None
        self.class_watch = None
        self.recheck_id = 0

    def wanted_class(self):
        """The class we want advertised, service bits included."""
        cod = self.options.device_class
        if self.options.audio:
            cod |= (hidspec.SERVICE_AUDIO | hidspec.SERVICE_RENDERING
                    | hidspec.SERVICE_CAPTURING)
        return cod

    def start(self):
        """Put the wanted class in place, and watch for it being undone."""
        self.report("before")
        if not self.options.audio:
            return
        wanted = self.wanted_class()
        self.cod = btd.ClassOfDevice(on_event=self.log)
        if self.cod.write(wanted):
            self.log("HCI_Write_Class_of_Device 0x%06x sent" % wanted)
        else:
            self.log("could not write the class of device; audio will very "
                     "likely not be offered")
        # The controller and bluetoothd both have to catch up, so look again
        # in a moment rather than reading back immediately.
        GLib.timeout_add_seconds(1, self.report, "after")
        GLib.timeout_add_seconds(SETTLE_SECONDS, self.check_advertised)
        # Watch rather than poll, so the window in which the wrong class is
        # being advertised stays as short as possible.
        self.class_watch = self.link.watch_class(self.class_changed)
        self.recheck_id = GLib.timeout_add_seconds(RECHECK_SECONDS,
                                                   self.recheck)

    def stop(self):
        """Stop putting the class back, and stop watching for it moving."""
        self.cod = None
        if self.recheck_id:
            GLib.source_remove(self.recheck_id)
            self.recheck_id = 0
        if self.class_watch is not None:
            self.class_watch.remove()
            self.class_watch = None

    def class_changed(self, current):
        wanted = self.wanted_class()
        if current & wanted == wanted or self.cod is None:
            return
        self.log("class of device changed to 0x%06x%s; putting it back"
                 % (current, service_bits(current)))
        self.cod.write(wanted)

    def recheck(self):
        current = self.link.class_of_device()
        if current is not None:
            self.class_changed(current)
        return True

    def report(self, when=""):
        current = self.link.class_of_device()
        if current is None:
            return False
        label = " (%s)" % when if when else ""
        self.log("class of device%s 0x%06x%s"
                 % (label, current, service_bits(current)))
        self.log("audio profiles: %s"
                 % (", ".join(self.link.audio_profiles()) or "none"))
        if when == "after":
            self.record("adapter UUIDs: %s"
                        % ", ".join(self.link.all_uuids()))
        return False

    def check_advertised(self):
        """Warn when what we advertise has moved since the last run."""
        current = "0x%06x %s" % (self.link.class_of_device() or 0,
                                 " ".join(sorted(self.link.all_uuids())))
        try:
            with open(STATE_FILE) as handle:
                previous = handle.read().strip()
        except OSError:
            previous = ""

        if previous and previous != current:
            self.announce("what this machine advertises has changed since "
                          "the last run; a phone already paired will not "
                          "see it until you forget and re-pair")
            # To the console as well as the file.  The announcement above
            # says a re-pair is needed; what changed is the next question,
            # and an installed btkey writes no file to answer it from.
            self.log("advertised was: " + previous)
            self.log("advertised now: " + current)
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as handle:
                handle.write(current + "\n")
        except OSError:
            pass
        return False
