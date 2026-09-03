# SPDX-License-Identifier: GPL-2.0-only
"""Bluetooth addresses, formed here rather than by the socket module.

CPython compiles in AF_BLUETOOTH, and with it everything that turns an
address into a struct sockaddr_l2, only when bluetooth/bluetooth.h was
present where it was built.  Nothing is linked against, so this is pure
autodetection at build time and nothing in the packaging records it: an
interpreter built on a machine that happened not to have the headers
cannot bind an L2CAP socket at all, and says so as an AttributeError.
Distributions that build their own Python land there.

What that support does for btkey is fourteen bytes of struct, so this
does it instead.  The family goes into socket() as the number it is, and
bind, connect and accept go through libc with the address packed here.
Everything else about these sockets - setsockopt, listen, send, recv,
poll - never looks at the family and is left to the socket module.

It is done this way whether or not this Python has Bluetooth support, so
every run exercises it rather than only the runs on machines nobody
develops on.  tests/test_btsock.py holds the packing against what the
socket module produces, wherever there is one to compare with.
"""

import ctypes
import errno
import os
import socket
import struct

# From the kernel, which is where they come from anyway.  Address
# families and Bluetooth protocol numbers are the same on every Linux
# architecture.
AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
BTPROTO_HCI = 1

# struct sockaddr_l2: the family in host order, the PSM little endian
# whatever the host is, the address with its bytes reversed, the channel
# id little endian, and the kind of address.  Thirteen bytes, which the
# compiler pads to fourteen.
SOCKADDR_L2_SIZE = 14
BDADDR_ANY = "00:00:00:00:00:00"
BDADDR_BREDR = 0

# struct sockaddr_hci: family, adapter index, channel, all host order.
HCI_CHANNEL_RAW = 0

_libc = ctypes.CDLL(None, use_errno=True)
_libc.bind.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_libc.connect.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_libc.accept4.argtypes = [ctypes.c_int, ctypes.c_char_p,
                          ctypes.POINTER(ctypes.c_uint), ctypes.c_int]


def _complain():
    """The OSError the socket module would have raised.

    Same errno and same strerror, and OSError picks its own subclass from
    the errno, so a connect in progress still arrives as BlockingIOError.
    """
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code))


def l2cap_address(addr, psm, cid=0, kind=BDADDR_BREDR):
    """One L2CAP address as the kernel wants it."""
    return (struct.pack("=H", AF_BLUETOOTH)
            + struct.pack("<H", psm)
            + bdaddr(addr)
            + struct.pack("<H", cid)
            + struct.pack("BB", kind, 0))


def hci_address(index, channel=HCI_CHANNEL_RAW):
    """One HCI address, for talking to a controller rather than a host."""
    return struct.pack("=HHH", AF_BLUETOOTH, index, channel)


def bdaddr(addr):
    """Six bytes, least significant first, from AA:BB:CC:DD:EE:FF."""
    parts = addr.split(":")
    if len(parts) != 6:
        raise ValueError("not a Bluetooth address: %r" % (addr,))
    return bytes(int(part, 16) for part in reversed(parts))


def address_in(blob):
    """The address out of a packed sockaddr_l2, as BlueZ spells it."""
    if len(blob) < 10:
        raise ValueError("not a Bluetooth address: %d bytes" % len(blob))
    return ":".join("%02X" % byte for byte in reversed(blob[4:10]))


def l2cap_socket():
    return socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)


def hci_socket():
    return socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)


def bind(sock, where):
    if _libc.bind(sock.fileno(), where, len(where)) < 0:
        _complain()


def connect(sock, where):
    if _libc.connect(sock.fileno(), where, len(where)) < 0:
        _complain()


def accept(sock):
    """The next connection on an L2CAP listener, and where it came from."""
    where = ctypes.create_string_buffer(SOCKADDR_L2_SIZE)
    while True:
        # Sized afresh each time round: accept4 writes back how much of
        # the address it filled in.  SOCK_CLOEXEC is what the socket
        # module's own accept asks for.
        size = ctypes.c_uint(SOCKADDR_L2_SIZE)
        handle = _libc.accept4(sock.fileno(), where, ctypes.byref(size),
                               socket.SOCK_CLOEXEC)
        if handle >= 0:
            break
        # PEP 475: an interrupted call is retried rather than reported.
        if ctypes.get_errno() != errno.EINTR:
            _complain()
    conn = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP,
                         fileno=handle)
    return conn, address_in(where.raw[:size.value])
