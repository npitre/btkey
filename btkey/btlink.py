# SPDX-License-Identifier: GPL-2.0-only
"""The Bluetooth Classic HID device side.

Three things have to happen for an iPhone to accept us as a keyboard:

  1. An SDP record advertising UUID 0x1124 with our report descriptor.
     We publish it through org.bluez.ProfileManager1 rather than the
     legacy sdpd compat socket, so bluetoothd does not need --compat.

  2. L2CAP listeners on PSM 17 (control) and 19 (interrupt).  These are
     fixed by the HID profile and are below 0x1000, so binding them needs
     CAP_NET_BIND_SERVICE - hence running as root.  bluetoothd's `input`
     plugin also wants them, which is why it must be disabled.

  3. Correct answers on the control channel.  Apple's stack sends
     SET_PROTOCOL(report) and GET_REPORT during setup and will drop the
     link if they go unanswered.  That is the part most of the hobby
     implementations skip.
"""

import os
import re
import socket
import struct
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from . import btsock, fifo, hidspec

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

PROFILE_PATH = "/org/btkey/profile"
AGENT_PATH = "/org/btkey/agent"

SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_MEDIUM = 2

# How long to wait for an outbound connect before giving up.  A phone that
# is asleep never answers, so this is the common case, not the rare one.
DIAL_TIMEOUT_MS = 3000

# HIDP transaction types, in the high nibble of the first byte.
HIDP_HANDSHAKE = 0x00
HIDP_HID_CONTROL = 0x10
HIDP_GET_REPORT = 0x40
HIDP_SET_REPORT = 0x50
HIDP_GET_PROTOCOL = 0x60
HIDP_SET_PROTOCOL = 0x70
HIDP_DATA = 0xA0

HANDSHAKE_SUCCESS = 0x00
HANDSHAKE_ERR_INVALID_REPORT_ID = 0x02
HANDSHAKE_ERR_UNSUPPORTED_REQUEST = 0x03

REPORT_TYPE_INPUT = 0x01
REPORT_TYPE_OUTPUT = 0x02
REPORT_TYPE_FEATURE = 0x03

HID_CONTROL_SUSPEND = 0x03
HID_CONTROL_EXIT_SUSPEND = 0x04
HID_CONTROL_VIRTUAL_CABLE_UNPLUG = 0x05

PROTOCOL_BOOT = 0
PROTOCOL_REPORT = 1


class ProfileNotAvailable(Exception):
    """The HID UUID or its PSMs are already taken by bluetoothd."""


class LinkError(Exception):
    """Something BlueZ would not do.

    D-Bus is this module's business and nobody else's, so a
    DBusException is converted here rather than carried out to a caller
    that would have to know what one is to report it.
    """


def use_glib_mainloop():
    """Marry D-Bus to the GLib loop, which has to happen before the bus.

    dbus-python dispatches through whatever main loop was made the
    default when the connection was opened, so this has to be done
    before the first SystemBus anywhere in the program - which is btd's,
    not this module's.  It lives here because that ordering rule is
    D-Bus knowledge, and D-Bus is this module's business.
    """
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)


def describe(exc):
    """A D-Bus failure as a line someone can act on.

    The name alone ("org.bluez.Error.Failed") says nothing; the message
    alone does not say who refused.
    """
    if isinstance(exc, dbus.DBusException):
        return "%s: %s" % (exc.get_dbus_name(),
                           exc.get_dbus_message() or "no detail given")
    return str(exc) or exc.__class__.__name__


def _listener(psm):
    sock = btsock.l2cap_socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY,
                    struct.pack("BB", BT_SECURITY_MEDIUM, 0))
    try:
        btsock.bind(sock, btsock.l2cap_address(btsock.BDADDR_ANY, psm))
    except OSError as exc:
        sock.close()
        if exc.errno == 98:      # EADDRINUSE
            raise ProfileNotAvailable(
                "L2CAP PSM %d is already bound, which means bluetoothd's "
                "input plugin is still loaded; see docs/SETUP.md" % psm)
        if exc.errno == 13:      # EACCES
            raise ProfileNotAvailable(
                "binding L2CAP PSM %d needs CAP_NET_BIND_SERVICE; run "
                "btkey as root" % psm)
        raise
    sock.listen(1)
    return sock


class Agent(dbus.service.Object):
    """BlueZ's pairing agent, in whichever style --pairing asked for.

    The capability decides what the phone does: DisplayYesNo, the default,
    has both ends show the same six digits to be compared, while
    KeyboardOnly has the phone display them and expect them typed back, as
    it would with a real keyboard.  Either way the reply is deferred until
    the answer arrives from the console, so the D-Bus methods use async
    callbacks rather than blocking the main loop.
    """

    def __init__(self, bus, path, link):
        super().__init__(bus, path)
        self.link = link
        self._passkey_reply = None
        self._passkey_error = None

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        self.link.event("pairing agent released")

    @dbus.service.method("org.bluez.Agent1", in_signature="os",
                         out_signature="")
    def AuthorizeService(self, device, uuid):
        # Only ever authorise the profile we exist to serve.
        if str(uuid).lower() != HID_UUID:
            raise dbus.DBusException("org.bluez.Error.Rejected")
        self.link.event("authorised HID service for %s" % _addr(device))

    @dbus.service.method("org.bluez.Agent1", in_signature="o",
                         out_signature="u",
                         async_callbacks=("reply_cb", "error_cb"))
    def RequestPasskey(self, device, reply_cb, error_cb):
        self._passkey_reply = reply_cb
        self._passkey_error = error_cb
        self.link.begin_passkey_entry(_addr(device))

    @dbus.service.method("org.bluez.Agent1", in_signature="o",
                         out_signature="s",
                         async_callbacks=("reply_cb", "error_cb"))
    def RequestPinCode(self, device, reply_cb, error_cb):
        # Legacy pairing.  Same digits, returned as a string.
        self._passkey_reply = lambda value: reply_cb("%04d" % value)
        self._passkey_error = error_cb
        self.link.begin_passkey_entry(_addr(device), legacy=True)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq",
                         out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        if entered:
            self.link.event("passkey %06u (%u digits entered on the phone)"
                            % (passkey, entered))
        else:
            self.link.show_passkey(int(passkey))

    @dbus.service.method("org.bluez.Agent1", in_signature="os",
                         out_signature="")
    def DisplayPinCode(self, device, pincode):
        self.link.event("PIN code %s" % pincode)

    @dbus.service.method("org.bluez.Agent1", in_signature="ou",
                         out_signature="")
    def RequestConfirmation(self, device, passkey):
        """Numeric comparison: both ends show the same six digits.

        Returning normally accepts.  We announce the digits rather than
        confirming silently, so they can still be checked against what the
        phone shows - and refused there if they differ.
        """
        if not self.link.confirm(_addr(device), int(passkey)):
            raise dbus.DBusException("org.bluez.Error.Rejected")

    @dbus.service.method("org.bluez.Agent1", in_signature="o",
                         out_signature="")
    def RequestAuthorization(self, device):
        self.link.event("authorised pairing with %s" % _addr(device))

    @dbus.service.method("org.bluez.Agent1", in_signature="",
                         out_signature="")
    def Cancel(self):
        self.link.event("the phone cancelled the pairing request")
        if self._passkey_error is not None:
            self._passkey_error(
                dbus.DBusException("org.bluez.Error.Canceled"))
        self._passkey_reply = self._passkey_error = None
        self.link.cancel_passkey_entry()

    def supply_passkey(self, value):
        """Called by the session once the user has typed the digits."""
        if self._passkey_reply is None:
            return
        reply, self._passkey_reply, self._passkey_error = (
            self._passkey_reply, None, None)
        reply(dbus.UInt32(value))

    def abandon_passkey(self):
        """Tell BlueZ nobody is going to answer, rather than leaving it.

        RequestPasskey is an asynchronous D-Bus call: returning from the
        method does not answer it.  Give up without this and BlueZ waits
        for its own timeout with the pairing half open, and the stored
        reply callback points at a call that has since expired.
        """
        if self._passkey_error is None:
            return
        error, self._passkey_reply, self._passkey_error = (
            self._passkey_error, None, None)
        error(dbus.DBusException("org.bluez.Error.Canceled"))


class Profile(dbus.service.Object):
    """Present only so BlueZ has somewhere to publish our SDP record.

    Our own listeners own PSM 17 and 19, so BlueZ never routes a connection
    here; NewConnection exists to satisfy the interface.
    """

    def __init__(self, bus, path, link):
        super().__init__(bus, path)
        self.link = link

    @dbus.service.method("org.bluez.Profile1", in_signature="",
                         out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}",
                         out_signature="")
    def NewConnection(self, device, fd, properties):
        # Our own listeners own PSM 17 and 19, so this should never fire.
        os.close(fd.take() if hasattr(fd, "take") else int(fd))

    @dbus.service.method("org.bluez.Profile1", in_signature="o",
                         out_signature="")
    def RequestDisconnection(self, device):
        pass


def _set_adapter(props, key, value, attempts=25, delay=0.2):
    """Set an adapter property, riding out org.bluez.Error.Busy.

    BlueZ answers Busy while a controller operation is already in flight,
    which is routine in the moments after bluetoothd starts.
    """
    for remaining in range(attempts, 0, -1):
        try:
            props.Set(ADAPTER_IFACE, key, value)
            return
        except dbus.DBusException as exc:
            if exc.get_dbus_name() != "org.bluez.Error.Busy" or remaining == 1:
                raise
            time.sleep(delay)


def _drop_watch(source):
    """Remove a GLib source that may have already removed itself."""
    try:
        GLib.source_remove(source)
    except (ValueError, GLib.Error):
        pass


def _addr(device_path):
    """Recover a MAC from a BlueZ object path like .../dev_AA_BB_CC_DD_EE_FF."""
    tail = str(device_path).rsplit("/", 1)[-1]
    if tail.startswith("dev_"):
        return tail[4:].replace("_", ":")
    return str(device_path)


class BluetoothHID:
    """Advertises us as a HID keyboard and carries reports to the host."""

    STATE_FILE = "/var/lib/btkey/host"
    UNREAD = object()          # not "no host on record"

    #: How much of the wall clock went into waiting for the radio.  Class
    #: attributes so that a link built without __init__ - which is how the
    #: report-format tests reach send_keyboard - still counts.
    sent_reports = 0
    send_seconds = 0.0

    def __init__(self, name="btkey", description="Console keyboard bridge",
                 provider="btkey", on_event=None, on_state=None, on_leds=None,
                 on_passkey=None, on_passkey_cancel=None,
                 on_passkey_display=None, on_confirm=None,
                 capability="KeyboardOnly",
                 debug=False, adapter=None):
        self.name = name
        self.description = description
        self.provider = provider
        self._on_event = on_event or (lambda msg: None)
        self._on_state = on_state or (lambda connected, peer: None)
        self._on_leds = on_leds or (lambda mask: None)
        self.leds = 0
        self._on_passkey = on_passkey or (lambda peer, legacy: None)
        self._on_passkey_cancel = on_passkey_cancel or (lambda: None)
        self._on_passkey_display = on_passkey_display or (lambda passkey: None)
        self._on_confirm = on_confirm or (lambda peer, passkey: True)
        self.capability = capability
        self.debug = debug
        self._adapter_hint = adapter

        self.bus = dbus.SystemBus()
        self.adapter_path = None
        self.agent = None
        self.profile = None

        self._listeners = {}          # psm -> listening socket
        self._watches = []            # GLib source ids for the listeners
        self._conn_watches = {}       # "control"/"interrupt" -> source id
        self.control = None           # accepted/connected control socket
        self.interrupt = None         # accepted/connected interrupt socket
        self.peer = None              # MAC of the connected host
        self.protocol = PROTOCOL_REPORT
        self._last_keyboard = bytes(8)
        self._saved_adapter = {}
        self._connecting = False
        self._last_dial = 0
        self._known_host = self.UNREAD
        self._proxies = {}

    # -- lifecycle -------------------------------------------------------

    def event(self, message):
        self._on_event(message)

    def start(self):
        try:
            self.adapter_path = self._find_adapter()
            self._configure_adapter()
            self._register_agent()
            self._register_profile()
        except dbus.DBusException as exc:
            raise LinkError(describe(exc)) from exc
        for psm in (hidspec.PSM_CONTROL, hidspec.PSM_INTERRUPT):
            sock = _listener(psm)
            self._listeners[psm] = sock
            self._watches.append(GLib.unix_fd_add_full(
                GLib.PRIORITY_DEFAULT, sock.fileno(), GLib.IOCondition.IN,
                self._on_incoming, psm))
        self.event("listening on PSM %d and %d as \"%s\""
                   % (hidspec.PSM_CONTROL, hidspec.PSM_INTERRUPT, self.name))

    def stop(self):
        self.disconnect("shutting down")
        for source in self._watches:
            _drop_watch(source)
        self._watches = []
        for sock in self._listeners.values():
            sock.close()
        self._listeners = {}
        try:
            self._manager(PROFILE_MANAGER_IFACE).UnregisterProfile(
                PROFILE_PATH)
        except dbus.DBusException:
            pass
        try:
            self._manager(AGENT_MANAGER_IFACE).UnregisterAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
        self._restore_adapter()

    # -- BlueZ registration ----------------------------------------------

    def _find_adapter(self):
        manager = dbus.Interface(self.bus.get_object(BLUEZ, "/"),
                                 OBJECT_MANAGER_IFACE)
        for path, interfaces in manager.GetManagedObjects().items():
            if ADAPTER_IFACE not in interfaces:
                continue
            if self._adapter_hint in (None, str(path).rsplit("/", 1)[-1]):
                return str(path)
        raise ProfileNotAvailable("no Bluetooth adapter found")

    def _proxy(self, path, interface):
        """A BlueZ interface, built once and kept.

        Every dbus.Interface(bus.get_object(...)) costs a GetNameOwner to
        the bus daemon and an Introspect of the object, both blocking and
        both on the main loop, before the call anyone wanted is sent.
        Keeping them turns a property read from three round trips into
        one.  Note that the introspection cannot simply be turned off:
        Properties.Set takes a variant, and dbus-python needs the
        signature to know to wrap the value in one.
        """
        proxy = self._proxies.get((path, interface))
        if proxy is None:
            proxy = dbus.Interface(self.bus.get_object(BLUEZ, path),
                                   interface)
            self._proxies[(path, interface)] = proxy
        return proxy

    def _manager(self, interface):
        return self._proxy("/org/bluez", interface)

    def _device(self, addr, interface):
        """One paired device, by address.  BlueZ spells it dev_AA_BB_..."""
        return self._proxy("%s/dev_%s" % (self.adapter_path,
                                          addr.replace(":", "_")), interface)

    def _adapter_props(self):
        return self._proxy(self.adapter_path, PROPERTIES_IFACE)

    def _configure_adapter(self):
        props = self._adapter_props()
        for key in ("Alias", "Powered", "Discoverable", "Pairable",
                    "DiscoverableTimeout"):
            try:
                self._saved_adapter[key] = props.Get(ADAPTER_IFACE, key)
            except dbus.DBusException:
                pass
        _set_adapter(props, "Powered", dbus.Boolean(True))
        _set_adapter(props, "Alias", dbus.String(self.name))
        _set_adapter(props, "DiscoverableTimeout", dbus.UInt32(0))
        _set_adapter(props, "Discoverable", dbus.Boolean(True))
        _set_adapter(props, "Pairable", dbus.Boolean(True))

        # The class is not checked here.  main.conf's Class is not honoured
        # on this BlueZ, so it is written with an HCI command after startup
        # instead - see advertising.py - and a check at this point would
        # only ever be a false alarm.

    def _restore_adapter(self):
        try:
            props = self._adapter_props()
        except dbus.DBusException:
            return
        for key in ("Discoverable", "Pairable", "DiscoverableTimeout",
                    "Alias"):
            if key in self._saved_adapter:
                try:
                    _set_adapter(props, key, self._saved_adapter[key],
                                 attempts=3)
                except dbus.DBusException:
                    pass

    def _register_agent(self):
        self.agent = Agent(self.bus, AGENT_PATH, self)
        manager = self._manager(AGENT_MANAGER_IFACE)
        manager.RegisterAgent(AGENT_PATH, self.capability)
        manager.RequestDefaultAgent(AGENT_PATH)
        self.event("pairing agent registered with capability %s"
                   % self.capability)

    def _register_profile(self):
        self.profile = Profile(self.bus, PROFILE_PATH, self)
        manager = self._manager(PROFILE_MANAGER_IFACE)
        options = {
            "Name": self.name,
            "Role": "server",
            "RequireAuthentication": dbus.Boolean(True),
            "RequireAuthorization": dbus.Boolean(False),
            "ServiceRecord": hidspec.service_record(
                self.name, self.description, self.provider),
        }
        try:
            manager.RegisterProfile(PROFILE_PATH, HID_UUID,
                                    dbus.Dictionary(options, signature="sv"))
        except dbus.DBusException as exc:
            if "already registered" in str(exc):
                raise ProfileNotAvailable(
                    "bluetoothd already owns the HID UUID, which means its "
                    "input plugin is loaded; see docs/SETUP.md")
            raise

    def class_of_device(self):
        try:
            return int(self._adapter_props().Get(ADAPTER_IFACE, "Class"))
        except (dbus.DBusException, ValueError):
            return None

    def watch_class(self, callback):
        """Call back when the adapter's class changes underneath us.

        bluetoothd recomputes the class from the registered UUIDs whenever
        that set changes, which happens well after startup as profiles come
        and go, and it does not preserve bits it did not put there.
        """
        def changed(interface, changed_properties, invalidated, path=None):
            if interface == ADAPTER_IFACE and "Class" in changed_properties:
                callback(int(changed_properties["Class"]))

        try:
            return self.bus.add_signal_receiver(
                changed, signal_name="PropertiesChanged",
                dbus_interface=PROPERTIES_IFACE, path=self.adapter_path,
                path_keyword="path")
        except dbus.DBusException as exc:
            # Worth carrying on without: the backstop below still puts
            # the class back, just less promptly.
            self.event("cannot watch the class of device: %s" % describe(exc))
            return None

    def all_uuids(self):
        try:
            return [str(uuid) for uuid
                    in self._adapter_props().Get(ADAPTER_IFACE, "UUIDs")]
        except dbus.DBusException:
            return []

    def audio_profiles(self):
        """Audio-related UUIDs the adapter currently advertises."""
        return [AUDIO_UUIDS[uuid[:8]] for uuid in self.all_uuids()
                if uuid[:8] in AUDIO_UUIDS]

    # -- pairing ---------------------------------------------------------

    def begin_passkey_entry(self, peer, legacy=False):
        self._on_passkey(peer, legacy)

    def cancel_passkey_entry(self):
        self._on_passkey_cancel()

    def abandon_passkey(self):
        if self.agent is not None:
            self.agent.abandon_passkey()

    def supply_passkey(self, value):
        if self.agent is not None:
            self.agent.supply_passkey(value)

    def show_passkey(self, passkey):
        self._on_passkey_display(passkey)

    def confirm(self, peer, passkey):
        return self._on_confirm(peer, passkey)

    #: A2DP Source, the profile a phone offers when it is willing to send
    #: audio here.  Connecting it is asking the phone to open that channel.
    A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"

    #: What BlueZ says when it is in the middle of something else.  Worth
    #: asking again for; the rest are answers rather than delays.
    BUSY_ERRORS = ("org.bluez.Error.InProgress", "org.bluez.Error.Busy")

    def connect_audio(self, addr):
        """Ask the phone to bring up its audio channel as well as the keyboard.

        Pairing a keyboard and routing audio are separate decisions to a
        phone, and it makes the second one by connecting a second profile.
        It does not always make it: after a fresh bond it connects HID and
        stops there, so the machine advertises somewhere to send sound and
        nothing ever asks it to.  Everything is in place at that point -
        the class of device, the A2DP Sink endpoints - and the audio simply
        does not arrive, with nothing anywhere saying why.

        Returns what happened and whether it is worth asking again,
        since a failure here costs nothing but the audio and should not
        stop anything else.
        """
        try:
            self._device(addr, DEVICE_IFACE).ConnectProfile(
                self.A2DP_SOURCE_UUID)
        except dbus.DBusException as exc:
            name = exc.get_dbus_name()
            return ("no audio channel: %s" % (exc.get_dbus_message() or name),
                    name in self.BUSY_ERRORS)
        return "audio channel connected", False

    #: Apple, Inc. in the Bluetooth SIG's company identifiers.  It is what
    #: a phone puts in its own Device ID record, so this is the host saying
    #: who made it rather than a guess from its name or its address.
    APPLE_VENDOR = 0x004C

    def host_vendor(self, addr):
        """The company identifier the host publishes, or None.

        BlueZ exposes the Device ID record as a modalias,
        `bluetooth:v004Cp7510d1A60`, where the four digits after the v are
        the company.  A host that publishes no such record has no answer
        here, which is different from answering that it is not Apple.
        """
        try:
            modalias = str(self._device(addr, PROPERTIES_IFACE)
                           .Get(DEVICE_IFACE, "Modalias"))
        except dbus.DBusException:
            return None
        found = re.match(r"bluetooth:v([0-9A-Fa-f]{4})", modalias)
        return int(found.group(1), 16) if found else None

    def trust(self, addr):
        try:
            self._device(addr, PROPERTIES_IFACE).Set(
                DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except dbus.DBusException as exc:
            # A phone that never got marked trusted asks again later, and
            # nothing would say why.
            self.event("could not mark %s trusted: %s"
                       % (addr, exc.get_dbus_message() or exc.get_dbus_name()))

    # -- connection management -------------------------------------------

    def _on_incoming(self, fd, condition, psm):
        try:
            conn, peer = btsock.accept(self._listeners[psm])
        except OSError as exc:
            if not fifo.keep_watching(exc):
                self.event("listener on PSM %d failed: %s"
                           % (psm, exc.strerror))
                return False
            return True

        if self.peer is not None and peer != self.peer:
            self.event("refusing second host %s" % peer)
            conn.close()
            return True

        self.peer = peer
        if psm == hidspec.PSM_CONTROL:
            self._adopt_control(conn)
        else:
            self._adopt_interrupt(conn)
        return True

    def _adopt(self, kind, conn, handler):
        self._close_channel(kind)
        setattr(self, kind, conn)
        self._conn_watches[kind] = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, conn.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            handler)

    def _close_channel(self, kind):
        source = self._conn_watches.pop(kind, None)
        if source is not None:
            _drop_watch(source)
        sock = getattr(self, kind, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            setattr(self, kind, None)

    def _adopt_control(self, conn):
        self._adopt("control", conn, self._on_control_data)
        self.event("control channel up from %s" % self.peer)

    def _adopt_interrupt(self, conn):
        self._adopt("interrupt", conn, self._on_interrupt_data)
        self.event("interrupt channel up from %s" % self.peer)
        self._remember_host(self.peer)
        self.trust(self.peer)
        self._on_state(True, self.peer)

    @property
    def connected(self):
        return self.control is not None and self.interrupt is not None

    def disconnect(self, reason=""):
        was_connected = self.connected
        peer = self.peer
        for kind in ("interrupt", "control"):
            self._close_channel(kind)
        self.peer = None
        self.leds = 0
        self._last_keyboard = bytes(8)
        if was_connected:
            self.event("disconnected from %s%s"
                       % (peer, " (%s)" % reason if reason else ""))
            self._on_state(False, peer)

    # -- outbound reconnect ----------------------------------------------

    def _remember_host(self, addr):
        self._known_host = addr
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, "w") as handle:
                handle.write(addr + "\n")
        except OSError:
            pass

    def last_host(self):
        """The host that paired with us, or None.

        Kept in hand once read.  reconnect() asks on every key event
        while the link is down - that is the point of it, a real keyboard
        wakes its host - and it asks before the rate limit below has had
        a chance to say no, so this was three syscalls per keystroke to
        answer a question whose answer this class is the only thing that
        ever changes.
        """
        if self._known_host is self.UNREAD:
            try:
                with open(self.STATE_FILE) as handle:
                    self._known_host = handle.read().strip() or None
            except OSError:
                self._known_host = None
        return self._known_host

    def reconnect(self):
        """Best-effort outbound connect to the host that paired with us.

        A real keyboard wakes its host when a key is pressed; the HID SDP
        record claims HIDReconnectInitiate, so we should honour it.
        """
        if self.connected or self._connecting:
            return
        addr = self.last_host()
        if addr is None:
            return
        now = GLib.get_monotonic_time()
        if now - self._last_dial < 10_000_000:   # microseconds
            return
        self._last_dial = now
        self._connecting = True
        self.event("reconnecting to %s" % addr)

        def control_up(control):
            def interrupt_up(interrupt):
                self.peer = addr
                self._adopt_control(control)
                self._adopt_interrupt(interrupt)
                self._connecting = False

            self._dial(addr, hidspec.PSM_INTERRUPT, interrupt_up,
                       cleanup=control.close)

        self._dial(addr, hidspec.PSM_CONTROL, control_up)

    def _dial(self, addr, psm, done, cleanup=None):
        """One outbound L2CAP connect, without blocking the main loop.

        A phone that is asleep or out of range does not refuse the
        connection, it simply says nothing, so a blocking connect costs the
        loop the whole timeout - twice, one channel after the other.  For
        that whole stretch there is no status line, no console polling and
        no keystroke forwarded: dead silence on exactly the keypress that
        was meant to wake the phone up.
        """
        try:
            sock = btsock.l2cap_socket()
            sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY,
                            struct.pack("BB", BT_SECURITY_MEDIUM, 0))
            sock.setblocking(False)
            try:
                btsock.connect(sock, btsock.l2cap_address(addr, psm))
            except BlockingIOError:
                pass                       # the usual case: still dialling
        except OSError as exc:
            if cleanup is not None:
                cleanup()
            self._dial_failed(exc.strerror or str(exc))
            return

        pending = {}

        def settle(error, arrived):
            """Whichever of the two sources fires first; drop the other."""
            if not pending:
                return False
            other = pending.pop("timeout" if arrived == "watch" else "watch")
            pending.clear()
            GLib.source_remove(other)
            if error:
                sock.close()
                if cleanup is not None:
                    cleanup()
                self._dial_failed(error)
            else:
                sock.setblocking(True)
                done(sock)
            return False

        def writable(fd, condition):
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return settle(os.strerror(code) if code else "", "watch")

        def expired():
            return settle("timed out", "timeout")

        pending["watch"] = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, sock.fileno(),
            GLib.IOCondition.OUT | GLib.IOCondition.HUP
            | GLib.IOCondition.ERR, writable)
        pending["timeout"] = GLib.timeout_add(DIAL_TIMEOUT_MS, expired)

    def _dial_failed(self, detail):
        self._connecting = False
        self.event("reconnect failed: %s" % detail)

    # -- HIDP control channel --------------------------------------------

    def _read_channel(self, kind, condition):
        """One channel's incoming data, or None once it has gone.

        The two channels are read the same way and torn down the same
        way, which is worth having in one place: an error path fixed on
        one of them and not the other leaves half a link standing.
        """
        sock = getattr(self, kind)
        if sock is None:
            return None
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self.disconnect("%s channel closed" % kind)
            return None
        try:
            data = sock.recv(1024)
        except OSError:
            self.disconnect("%s channel error" % kind)
            return None
        if not data:
            self.disconnect("%s channel closed" % kind)
            return None
        return data

    def _on_control_data(self, fd, condition):
        data = self._read_channel("control", condition)
        if data is None:
            return False
        self._handle_control(data)
        return True

    def _on_interrupt_data(self, fd, condition):
        data = self._read_channel("interrupt", condition)
        if data is None:
            return False
        # Hosts push output reports here rather than on the control
        # channel - the kernel's own HIDP does.  Same shape as the control
        # path: a header byte, then the report ID and its data.
        if data[0] & 0xF0 == HIDP_DATA:
            self._note_output_report(data[1:])
        return True

    def _send_control(self, payload):
        if self.control is None:
            return
        if self.debug:
            self.event("control -> %s" % payload.hex(" "))
        try:
            self.control.send(payload)
        except OSError:
            self.disconnect("control channel write failed")

    def _handshake(self, code):
        self._send_control(bytes([HIDP_HANDSHAKE | code]))

    def _handle_control(self, data):
        if self.debug:
            self.event("control <- %s" % data.hex(" "))
        header = data[0]
        transaction = header & 0xF0
        param = header & 0x0F

        if transaction == HIDP_GET_REPORT:
            self._handle_get_report(param, data[1:])
        elif transaction == HIDP_SET_REPORT:
            self._note_output_report(data[1:])
            self._handshake(HANDSHAKE_SUCCESS)
        elif transaction == HIDP_GET_PROTOCOL:
            self._send_control(bytes([HIDP_DATA, self.protocol]))
        elif transaction == HIDP_SET_PROTOCOL:
            self.protocol = PROTOCOL_REPORT if param & 0x01 else PROTOCOL_BOOT
            self.event("host selected %s protocol"
                       % ("report" if self.protocol else "boot"))
            self._handshake(HANDSHAKE_SUCCESS)
        elif transaction == HIDP_HID_CONTROL:
            self._handle_hid_control(param)
        else:
            self._handshake(HANDSHAKE_ERR_UNSUPPORTED_REQUEST)

    def _handle_get_report(self, param, payload):
        report_type = param & 0x03
        report_id = payload[0] if payload else hidspec.REPORT_ID_KEYBOARD
        if report_type != REPORT_TYPE_INPUT:
            self._handshake(HANDSHAKE_ERR_UNSUPPORTED_REQUEST)
            return
        if report_id == hidspec.REPORT_ID_KEYBOARD:
            body = bytes([hidspec.REPORT_ID_KEYBOARD]) + self._last_keyboard
        elif report_id == hidspec.REPORT_ID_CONSUMER:
            body = bytes([hidspec.REPORT_ID_CONSUMER, 0x00, 0x00])
        else:
            self._handshake(HANDSHAKE_ERR_INVALID_REPORT_ID)
            return
        self._send_control(bytes([HIDP_DATA | REPORT_TYPE_INPUT]) + body)

    def _handle_hid_control(self, param):
        if param == HID_CONTROL_VIRTUAL_CABLE_UNPLUG:
            self.event("host unplugged the virtual cable")
            self._known_host = None
            try:
                os.unlink(self.STATE_FILE)
            except OSError:
                pass
            self.disconnect("virtual cable unplug")
        elif param == HID_CONTROL_SUSPEND:
            self.event("host suspended")
        elif param == HID_CONTROL_EXIT_SUSPEND:
            self.event("host resumed")

    def _note_output_report(self, payload):
        """The host's LED report: Caps Lock and friends, as it sees them."""
        if len(payload) < 2 or payload[0] != hidspec.REPORT_ID_KEYBOARD:
            return
        mask = payload[1] & 0x1F
        if mask == self.leds:
            return          # hosts re-send the report unchanged; ignore that
        self.leds = mask
        self._on_leds(mask)

    # -- sending input reports -------------------------------------------

    def send_keyboard(self, modifiers, keys):
        """keys is up to six HID usages; short lists are zero padded."""
        slots = list(keys[:6]) + [0] * (6 - len(keys[:6]))
        report = bytes([modifiers, 0x00] + slots)
        self._last_keyboard = report
        self._send_interrupt(bytes([HIDP_DATA | REPORT_TYPE_INPUT,
                                    hidspec.REPORT_ID_KEYBOARD]) + report)

    def send_consumer(self, usage):
        self._send_interrupt(bytes([HIDP_DATA | REPORT_TYPE_INPUT,
                                    hidspec.REPORT_ID_CONSUMER])
                             + struct.pack("<H", usage))

    def _send_interrupt(self, payload):
        if self.interrupt is None:
            return
        if self.debug:
            self.event("interrupt -> %s" % payload.hex(" "))
        # The socket blocks, so this is where a link that cannot keep up
        # shows itself: the send waits and the main loop waits with it.
        # Nothing else can tell that apart from btkey being slow, so the
        # waiting is counted rather than guessed at.
        started = time.monotonic()
        try:
            self.interrupt.send(payload)
        except OSError:
            self.disconnect("interrupt channel write failed")
            return
        self.sent_reports += 1
        self.send_seconds += time.monotonic() - started


#: The audio profiles worth naming when reporting what the adapter
#: advertises, by the short form of their UUID.
AUDIO_UUIDS = {"0000110a": "A2DP Source", "0000110b": "A2DP Sink",
               "0000110c": "AVRCP Target", "0000110e": "AVRCP",
               "0000111e": "Handsfree", "0000111f": "Handsfree AG",
               "00001108": "Headset", "00001112": "Headset AG"}

#: Short forms for the standing status-line indicator.  Only the locks are
#: worth the width; Compose and Kana never change on the phones this sees.
LED_INDICATORS = ("NUM", "CAPS", "SCROLL")


def led_indicator(bits):
    """Compact lock-key indicator, e.g. "CAPS" or "NUM CAPS"."""
    return " ".join(name for index, name in enumerate(LED_INDICATORS)
                    if bits & (1 << index))


def led_names(bits):
    names = ["NumLock", "CapsLock", "ScrollLock", "Compose", "Kana"]
    return " ".join(name for index, name in enumerate(names)
                    if bits & (1 << index))
