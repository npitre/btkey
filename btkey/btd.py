# SPDX-License-Identifier: GPL-2.0-only
"""Run a private bluetoothd for the lifetime of btkey.

bluetoothd's `input` plugin implements the HID *host* role and claims UUID
0x1124 plus L2CAP PSM 17 and 19 at startup.  btkey needs to be the HID
*device* on the same controller, so the two cannot coexist.

Rather than permanently reconfiguring the system daemon - which would leave
the machine unable to use Bluetooth keyboards, mice or a braille display of
its own even when btkey is not running - we stop the system unit, run our
own bluetoothd without that plugin, and hand the name back on exit.  It
reads the same /etc/bluetooth/main.conf as the system one and nothing there
is touched: the only thing btkey needs to change is the class of device,
and main.conf turns out not to be honoured for that anyway - a run with
Class = 0x000540 in it left the adapter at the default.  See advertising.py
for what does work.

Two independent belts hold this together: the child gets PR_SET_PDEATHSIG so
it dies with us even if we are killed outright, and the guardian restarts
the system unit afterwards.
"""

import ctypes
import os
import shutil
import signal
import subprocess
import time

import dbus

BLUETOOTHD = "/usr/libexec/bluetooth/bluetoothd"
UNIT = "bluetooth.service"
SYSTEM_CONFIG = "/etc/bluetooth/main.conf"

PR_SET_PDEATHSIG = 1


class BluetoothdError(Exception):
    pass


class ManagedBluetoothd:
    """Owns a bluetoothd child process for as long as btkey is running."""

    #: Always disabled - it owns the HID UUID and PSMs we need.
    REQUIRED_NOPLUGIN = ("input",)
    #: Dropped unless audio was asked for.  With these loaded the adapter
    #: advertises A2DP Sink, and a phone that has just bonded with us for
    #: the keyboard will happily start routing its audio here as well.
    AUDIO_PLUGINS = ("a2dp", "avrcp")

    def __init__(self, class_of_device, on_event=None, guardian=None,
                 audio=True):
        self.class_of_device = class_of_device
        self.audio = audio
        self.event = on_event or (lambda message: None)
        self.guardian = guardian
        self.process = None
        self.unit_was_active = False

    # -- lifecycle -------------------------------------------------------

    def start(self):
        if not os.path.exists(BLUETOOTHD):
            raise BluetoothdError("%s not found" % BLUETOOTHD)

        # "enabled but not running" is what an earlier crash of ours looks
        # like, so treat it as something to restore rather than leaving the
        # machine with no bluetoothd.
        self.unit_was_active = _unit_is_active() or _unit_is_enabled()
        if self.unit_was_active:
            self.event("stopping the system %s" % UNIT)
            # Ask the guardian to put it back before we take it away, so
            # there is no window where a crash leaves the system without it.
            if self.guardian is not None:
                self.guardian.start_unit_on_death(UNIT)
            _systemctl("stop", UNIT)

        try:
            self._spawn()
            self._wait_for_adapter()
        except Exception:
            self.stop()
            raise
        self.event("private bluetoothd running (pid %d, without: %s)"
                   % (self.process.pid, self._noplugin()))

    def stop(self):
        if self.process is not None:
            self.event("stopping private bluetoothd")
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    # Unreapable - stuck in the kernel on a wedged
                    # controller, say.  Nothing more can be done about it,
                    # and giving up here would skip the restart below and
                    # leave the machine with no bluetoothd at all, which is
                    # the outcome this whole arrangement exists to prevent.
                    self.event("could not stop private bluetoothd: %s" % exc)
            except OSError:
                pass
            self.process = None
        if self.unit_was_active:
            self.event("restarting the system %s" % UNIT)
            _systemctl("start", UNIT)
            self.unit_was_active = False

    # -- internals -------------------------------------------------------

    def _noplugin(self):
        """The --noplugin= list: always input, plus audio when asked."""
        names = list(self.REQUIRED_NOPLUGIN)
        if not self.audio:
            names += list(self.AUDIO_PLUGINS)
        return ",".join(names)

    def _spawn(self):
        def set_pdeathsig():
            # If btkey dies by any means, take bluetoothd down with it
            # rather than leaving a daemon nobody owns holding org.bluez.
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(
                PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)

        self.process = subprocess.Popen(
            [BLUETOOTHD, "--nodetach", "--noplugin=" + self._noplugin()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=set_pdeathsig)
        if self.guardian is not None:
            self.guardian.kill_on_death(self.process.pid,
                                        os.path.basename(BLUETOOTHD))

    def _wait_for_adapter(self, timeout=25.0):
        """Wait for org.bluez to come back with a usable, powered adapter.

        Two separate waits, because bluetoothd exports the Adapter1 object
        as soon as it knows the controller exists - well before it has
        finished bringing it up.  Setting any property in that window comes
        back as org.bluez.Error.Busy.
        """
        deadline = time.monotonic() + timeout
        path = self._wait_for_object(deadline)
        self._wait_for_power(path, deadline)

    def _wait_for_object(self, deadline):
        last_error = "timed out"
        while time.monotonic() < deadline:
            self._check_alive()
            try:
                bus = dbus.SystemBus()
                manager = dbus.Interface(bus.get_object("org.bluez", "/"),
                                         "org.freedesktop.DBus.ObjectManager")
                for path, interfaces in manager.GetManagedObjects().items():
                    if "org.bluez.Adapter1" in interfaces:
                        return str(path)
                last_error = "no adapter exposed"
            except dbus.DBusException as exc:
                last_error = exc.get_dbus_message()
            time.sleep(0.2)
        raise BluetoothdError("private bluetoothd never came up (%s)"
                              % last_error)

    def _wait_for_power(self, path, deadline):
        last_error = "timed out"
        while time.monotonic() < deadline:
            self._check_alive()
            try:
                props = dbus.Interface(
                    dbus.SystemBus().get_object("org.bluez", path),
                    "org.freedesktop.DBus.Properties")
                if bool(props.Get("org.bluez.Adapter1", "Powered")):
                    return
                # AutoEnable normally does this for us; ask anyway in case
                # it is off, and tolerate Busy while the controller settles.
                props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
            except dbus.DBusException as exc:
                last_error = exc.get_dbus_message() or exc.get_dbus_name()
            time.sleep(0.2)
        raise BluetoothdError("adapter never powered on (%s)" % last_error)

    def _check_alive(self):
        if self.process.poll() is not None:
            raise BluetoothdError(
                "bluetoothd exited immediately with status %d; try running "
                "it by hand to see why" % self.process.returncode)


def _unit_is_active():
    return _systemctl("is-active", UNIT).returncode == 0


def _unit_is_enabled():
    return _systemctl("is-enabled", UNIT).returncode == 0


def _systemctl(*args):
    if shutil.which("systemctl") is None:
        return subprocess.CompletedProcess(args, 1)
    return subprocess.run(["systemctl"] + list(args),
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
