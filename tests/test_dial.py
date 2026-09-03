#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Reconnecting to a sleeping phone, without stopping everything else.

An outbound connect to a phone that is asleep is not refused, it is
ignored: the connect sits there until it times out.  Doing that on the
main loop costs the whole timeout twice over, one channel after the other,
and for that stretch there is no status line, no console polling and no
keystroke forwarded - on exactly the keypress meant to wake the phone.

So the dial runs on the loop rather than in front of it.  These tests
drive that state machine over real pipe file descriptors, which the poll
loop treats as sockets that are, or never become, writable.
"""

import errno
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib

from btkey import btlink, btsock, hidspec


class FakeSocket:
    """An L2CAP socket whose connect never completes on its own.

    Backed by one end of a real pipe so that GLib can poll it: the write
    end is always ready, the read end of an empty pipe never is - which is
    the difference between a phone that answers and one that is asleep.
    """

    made = []

    def __init__(self, ready=True, error=0):
        read_fd, write_fd = os.pipe()
        self.fd = write_fd if ready else read_fd
        self.spare = read_fd if ready else write_fd
        self.error = error
        self.blocking = True
        self.closed = False
        self.connected_to = None
        self.blocking_at_connect = None
        FakeSocket.made.append(self)

    def setsockopt(self, level, option, value):
        pass

    def getsockopt(self, level, option):
        return self.error

    def setblocking(self, flag):
        self.blocking = flag

    def connect(self, where):
        self.connected_to = where
        self.blocking_at_connect = self.blocking
        raise BlockingIOError(errno.EINPROGRESS, "in progress")

    def fileno(self):
        return self.fd

    def close(self):
        self.closed = True

    def release(self):
        for fd in (self.fd, self.spare):
            try:
                os.close(fd)
            except OSError:
                pass


class DialTest(unittest.TestCase):
    def setUp(self):
        FakeSocket.made = []
        self.plan = []                  # one FakeSocket per dial, in order
        self.events = []
        self.link = object.__new__(btlink.BluetoothHID)
        self.link.event = self.events.append
        self.link.peer = None
        self.link.control = None
        self.link.interrupt = None
        self.link._connecting = False
        self.link._last_dial = 0
        self.link.adopted = []
        self.link._adopt_control = lambda sock: self.link.adopted.append(sock)
        self.link._adopt_interrupt = lambda sock: self.link.adopted.append(sock)
        self.link.last_host = lambda: "AA:BB:CC:DD:EE:FF"

        self.saved_socket = btlink.btsock.l2cap_socket
        btlink.btsock.l2cap_socket = self.next_socket
        # The real one goes to libc, which will have nothing to do with a
        # pipe.  The fake raises what a dial to a sleeping phone raises.
        self.saved_connect = btlink.btsock.connect
        btlink.btsock.connect = lambda sock, where: sock.connect(where)
        self.saved_timeout = btlink.DIAL_TIMEOUT_MS
        btlink.DIAL_TIMEOUT_MS = 30

    def tearDown(self):
        btlink.btsock.l2cap_socket = self.saved_socket
        btlink.btsock.connect = self.saved_connect
        btlink.DIAL_TIMEOUT_MS = self.saved_timeout
        for sock in FakeSocket.made:
            sock.release()

    def next_socket(self, *args, **kwargs):
        return self.plan.pop(0)

    def spin(self, rounds=200):
        """Let the loop run until the dial has settled."""
        context = GLib.MainContext.default()
        for _ in range(rounds):
            if not self.link._connecting:
                return
            context.iteration(False)
            if not context.pending():
                GLib.usleep(1000)
        self.fail("the dial never settled")

    # -- the point of the exercise ---------------------------------------

    def test_reconnect_returns_before_the_dial_finishes(self):
        # The blocking version adopted both channels before returning.
        self.plan = [FakeSocket(), FakeSocket()]
        self.link.reconnect()
        self.assertTrue(self.link._connecting)
        self.assertEqual(self.link.adopted, [])
        self.spin()
        self.assertEqual(len(self.link.adopted), 2)

    def test_a_dial_that_never_answers_gives_up_on_its_own(self):
        self.plan = [FakeSocket(ready=False)]
        self.link.reconnect()
        self.spin()
        self.assertFalse(self.link._connecting)
        self.assertTrue(self.plan == [])
        self.assertIn("timed out", " ".join(self.events))

    def test_both_channels_are_dialled_in_order(self):
        control, interrupt = FakeSocket(), FakeSocket()
        self.plan = [control, interrupt]
        self.link.reconnect()
        self.spin()
        # The packed address itself, which is what the dial hands to the
        # kernel; test_btsock.py is where the packing is held to account.
        self.assertEqual(control.connected_to,
                         btsock.l2cap_address("AA:BB:CC:DD:EE:FF",
                                              hidspec.PSM_CONTROL))
        self.assertEqual(interrupt.connected_to,
                         btsock.l2cap_address("AA:BB:CC:DD:EE:FF",
                                              hidspec.PSM_INTERRUPT))
        self.assertEqual(self.link.adopted, [control, interrupt])
        self.assertEqual(self.link.peer, "AA:BB:CC:DD:EE:FF")

    def test_a_refused_connection_is_reported_and_closed(self):
        refused = FakeSocket(error=errno.ECONNREFUSED)
        self.plan = [refused]
        self.link.reconnect()
        self.spin()
        self.assertTrue(refused.closed)
        self.assertFalse(self.link._connecting)
        self.assertEqual(self.link.adopted, [])
        self.assertIn("reconnect failed", " ".join(self.events))

    def test_a_failed_interrupt_channel_closes_the_control_one(self):
        # Otherwise the phone is left holding a half-open HID connection
        # and the next attempt dials into it.
        control = FakeSocket()
        interrupt = FakeSocket(error=errno.EHOSTDOWN)
        self.plan = [control, interrupt]
        self.link.reconnect()
        self.spin()
        self.assertTrue(control.closed)
        self.assertTrue(interrupt.closed)
        self.assertEqual(self.link.adopted, [])

    def test_the_socket_goes_back_to_blocking_once_it_is_up(self):
        # Everything downstream sends with plain send(); a non-blocking
        # socket would drop reports on a full buffer instead of waiting.
        self.plan = [FakeSocket(), FakeSocket()]
        self.link.reconnect()
        self.spin()
        for sock in self.link.adopted:
            self.assertTrue(sock.blocking)

    def test_the_connect_itself_is_issued_non_blocking(self):
        # The one line that decides whether the loop stops: a blocking
        # connect to a sleeping phone returns only when it times out.
        self.plan = [FakeSocket(), FakeSocket()]
        self.link.reconnect()
        self.spin()
        self.assertEqual(len(FakeSocket.made), 2)
        for sock in FakeSocket.made:
            self.assertFalse(sock.blocking_at_connect)

    def test_a_dial_already_running_is_not_started_again(self):
        self.plan = [FakeSocket(), FakeSocket()]
        self.link.reconnect()
        self.link._last_dial = 0          # the rate limit is not the guard
        self.link.reconnect()             # would raise if it dialled again
        self.spin()
        self.assertEqual(len(self.link.adopted), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
