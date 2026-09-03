#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bluetooth addresses packed by hand, against the ones Python packs.

btkey forms its own sockaddr rather than asking the socket module to,
because the socket module cannot everywhere: the support is compiled in
only where the Bluetooth headers were present at build time.  That makes
the packing btkey's to get right, and the way to know it is right is to
put it beside the one the socket module produces on a machine that has
it.

Three ways of comparing, since no single one covers it:

  - the bytes the kernel ends up holding, read back with getsockname
    after a bind each way,
  - the two binds colliding, which is the kernel itself saying the
    addresses name the same thing,
  - what the socket module makes of an address this packed, and what
    this makes of the socket module's.

All of that needs a kernel with Bluetooth in it, and the comparisons
need a Python with it too, so they skip when there is nothing to compare
against.  The packing tests below them need neither.

What is not covered here is accept(), which wants a phone on the other
end.  Its error path is compared with the socket module's, and the
address it decodes is the same function the round trip covers.
"""

import ctypes
import errno
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import btsock

HAS_PYTHON_BLUETOOTH = hasattr(socket, "AF_BLUETOOTH")

# Above 0x1000, so binding one needs no privilege, and odd, as every
# L2CAP PSM must be.
FREE_PSM = 0x1001
OTHER_PSM = 0x1003

_libc = ctypes.CDLL(None, use_errno=True)
_libc.getsockname.argtypes = [ctypes.c_int, ctypes.c_char_p,
                              ctypes.POINTER(ctypes.c_uint)]


def raw_name(sock, size=64):
    """The sockaddr the kernel holds for a socket, byte for byte."""
    where = ctypes.create_string_buffer(size)
    length = ctypes.c_uint(size)
    if _libc.getsockname(sock.fileno(), where, ctypes.byref(length)) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return where.raw[:length.value]


def l2cap_socket():
    """One L2CAP socket, or a skip if this kernel has no Bluetooth."""
    try:
        return btsock.l2cap_socket()
    except OSError as exc:
        raise unittest.SkipTest("no L2CAP socket here: %s" % exc.strerror)


class PackingTest(unittest.TestCase):
    """The layout itself, which needs neither a kernel nor a phone."""

    def test_an_address_is_six_bytes_least_significant_first(self):
        self.assertEqual(btsock.bdaddr("11:22:33:44:55:66"),
                         b"\x66\x55\x44\x33\x22\x11")

    def test_the_whole_thing_is_fourteen_bytes(self):
        self.assertEqual(len(btsock.l2cap_address(btsock.BDADDR_ANY, 17)),
                         btsock.SOCKADDR_L2_SIZE)

    def test_the_family_is_in_the_host_s_own_order(self):
        packed = btsock.l2cap_address(btsock.BDADDR_ANY, 17)
        self.assertEqual(struct.unpack("=H", packed[:2])[0],
                         btsock.AF_BLUETOOTH)

    def test_the_psm_is_little_endian_whatever_the_host_is(self):
        # sa_family_t is host order and the PSM beside it is not, which
        # is the one thing about this struct that can be got wrong on a
        # machine nobody here has.
        packed = btsock.l2cap_address(btsock.BDADDR_ANY, 0x1234)
        self.assertEqual(packed[2:4], b"\x34\x12")

    def test_an_address_survives_the_round_trip(self):
        packed = btsock.l2cap_address("A0:B1:C2:D3:E4:F5", 19)
        self.assertEqual(btsock.address_in(packed), "A0:B1:C2:D3:E4:F5")

    def test_an_address_comes_back_the_way_bluez_writes_one(self):
        # Upper case hex, since that is what BlueZ hands us to compare
        # against, and a host that came in lower case would never match.
        packed = btsock.l2cap_address("a0:b1:c2:d3:e4:f5", 19)
        self.assertEqual(btsock.address_in(packed), "A0:B1:C2:D3:E4:F5")

    def test_something_that_is_not_an_address_is_refused(self):
        for bad in ("", "11:22:33", "11:22:33:44:55:66:77", "nonsense"):
            with self.assertRaises(ValueError):
                btsock.bdaddr(bad)

    def test_a_truncated_sockaddr_is_refused(self):
        with self.assertRaises(ValueError):
            btsock.address_in(b"\x1f\x00\x11\x00")

    def test_an_hci_address_is_six_bytes_of_host_order(self):
        self.assertEqual(btsock.hci_address(3),
                         struct.pack("=HHH", btsock.AF_BLUETOOTH, 3,
                                     btsock.HCI_CHANNEL_RAW))


@unittest.skipUnless(HAS_PYTHON_BLUETOOTH,
                     "this Python has no Bluetooth support to compare with")
class SameAsPythonTest(unittest.TestCase):
    """The comparison the packing exists to survive."""

    def test_the_numbers_are_the_ones_python_has(self):
        self.assertEqual(btsock.AF_BLUETOOTH, socket.AF_BLUETOOTH)
        self.assertEqual(btsock.BTPROTO_L2CAP, socket.BTPROTO_L2CAP)
        self.assertEqual(btsock.BTPROTO_HCI, socket.BTPROTO_HCI)

    def test_the_kernel_holds_the_same_bytes_either_way(self):
        native = l2cap_socket()
        self.addCleanup(native.close)
        try:
            native.bind(("11:22:33:44:55:66", FREE_PSM))
        except OSError as exc:
            self.skipTest("cannot bind that address here: %s" % exc.strerror)
        self.assertEqual(raw_name(native),
                         btsock.l2cap_address("11:22:33:44:55:66", FREE_PSM))

    def test_python_reads_back_what_we_bound(self):
        ours = l2cap_socket()
        self.addCleanup(ours.close)
        btsock.bind(ours, btsock.l2cap_address(btsock.BDADDR_ANY, OTHER_PSM))
        self.assertEqual(ours.getsockname(),
                         (btsock.BDADDR_ANY, OTHER_PSM))

    def test_the_kernel_says_the_two_name_the_same_address(self):
        # Nothing above proves the kernel agrees; a second bind refused as
        # already in use is the kernel itself saying so.
        ours = l2cap_socket()
        self.addCleanup(ours.close)
        btsock.bind(ours, btsock.l2cap_address(btsock.BDADDR_ANY, OTHER_PSM))
        native = l2cap_socket()
        self.addCleanup(native.close)
        with self.assertRaises(OSError) as caught:
            native.bind((btsock.BDADDR_ANY, OTHER_PSM))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_a_refused_bind_reads_exactly_like_pythons(self):
        ours, native = l2cap_socket(), l2cap_socket()
        self.addCleanup(ours.close)
        self.addCleanup(native.close)
        taken = l2cap_socket()
        self.addCleanup(taken.close)
        btsock.bind(taken, btsock.l2cap_address(btsock.BDADDR_ANY, FREE_PSM))

        with self.assertRaises(OSError) as theirs:
            native.bind((btsock.BDADDR_ANY, FREE_PSM))
        with self.assertRaises(OSError) as mine:
            btsock.bind(ours, btsock.l2cap_address(btsock.BDADDR_ANY,
                                                   FREE_PSM))
        self.assertEqual(mine.exception.errno, theirs.exception.errno)
        self.assertEqual(mine.exception.strerror, theirs.exception.strerror)

    def test_a_failed_accept_reads_exactly_like_pythons(self):
        # Not a listener, so both are refused, which is as far as accept
        # can be compared without a phone.
        ours, native = l2cap_socket(), l2cap_socket()
        self.addCleanup(ours.close)
        self.addCleanup(native.close)
        with self.assertRaises(OSError) as theirs:
            native.accept()
        with self.assertRaises(OSError) as mine:
            btsock.accept(ours)
        self.assertEqual(mine.exception.errno, theirs.exception.errno)
        self.assertEqual(mine.exception.strerror, theirs.exception.strerror)

    def test_an_hci_address_binds_where_pythons_does(self):
        try:
            native, ours = btsock.hci_socket(), btsock.hci_socket()
        except OSError as exc:
            self.skipTest("no HCI socket here: %s" % exc.strerror)
        self.addCleanup(native.close)
        self.addCleanup(ours.close)
        try:
            native.bind((0,))
        except OSError as exc:
            self.skipTest("cannot bind adapter 0: %s" % exc.strerror)
        self.assertEqual(raw_name(native), btsock.hci_address(0))
        btsock.bind(ours, btsock.hci_address(0))
        self.assertEqual(ours.getsockname(), native.getsockname())


class NobodyElseAsksTest(unittest.TestCase):
    """One socket.AF_BLUETOOTH anywhere else undoes the whole point.

    It would run perfectly well here and on every machine that builds
    Python with the headers, and fail only where btkey has no other way
    to be tested: on somebody else's.
    """

    NAMES = ("socket.AF_BLUETOOTH", "socket.BTPROTO", "socket.BDADDR")

    def test_no_module_asks_python_for_bluetooth(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in sorted(os.listdir(os.path.join(here, "btkey"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(here, "btkey", name)) as handle:
                lines = handle.read().splitlines()
            # A line at a time: the failure has to be readable, and
            # assertNotIn against a whole file prints the whole file.
            for number, line in enumerate(lines, 1):
                for wanted in self.NAMES:
                    if wanted in line:
                        self.fail("btkey/%s:%d names %s, which not every "
                                  "Python has" % (name, number, wanted))


class SocketTest(unittest.TestCase):
    """The sockets themselves, which the socket module still makes."""

    def test_an_l2cap_socket_is_seqpacket(self):
        sock = l2cap_socket()
        self.addCleanup(sock.close)
        self.assertEqual(sock.type, socket.SOCK_SEQPACKET)
        self.assertEqual(sock.proto, btsock.BTPROTO_L2CAP)

    def test_a_failed_bind_raises_what_the_errno_calls_for(self):
        sock = l2cap_socket()
        self.addCleanup(sock.close)
        sock.close()
        with self.assertRaises(OSError) as caught:
            btsock.bind(sock, btsock.l2cap_address(btsock.BDADDR_ANY,
                                                   FREE_PSM))
        self.assertEqual(caught.exception.errno, errno.EBADF)
        self.assertEqual(caught.exception.strerror,
                         os.strerror(errno.EBADF))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
