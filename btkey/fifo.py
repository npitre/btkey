# SPDX-License-Identifier: GPL-2.0-only
"""Creating the FIFOs btkey listens on.

Two things here are less obvious than they look.

btkey runs as root, so a FIFO it creates is owned by root and mode 0600 -
which locks out the very person who started it.  Anything able to write to
one types into a phone, so it is handed to exactly one user and to nobody
else, and that is verified after the fact rather than assumed.  Writing to the control
FIFO would then need a second sudo for every command, which is absurd for
something whose whole job is to save typing.  So the FIFO is handed to
whoever invoked the sudo, who is the only person who could sensibly drive
it anyway.

And a path that already exists is not necessarily a FIFO.  `echo
learn-layout > /run/btkey/control` before btkey has ever created it leaves
an ordinary
file behind, and an ordinary file is always ready to read: the GLib watch
fires immediately, reads end-of-file, and removes itself, leaving a dead
channel that had logged itself as working.  So anything in the way that is
not a FIFO gets replaced.
"""

import os
import stat


def invoking_user():
    """(uid, gid) of whoever ran sudo, or None if that is not known."""
    try:
        return (int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    except (KeyError, ValueError):
        return None


def _is_private(handle, owner):
    """Is this descriptor reachable only by the user who should have it?

    Checked rather than assumed.  Anything that can write here types into
    somebody's phone, so "it should be 0600 because that is what we asked
    for" is not the standard to hold it to - the mode of a FIFO left behind
    by an earlier run is whatever that run left, and a wrong answer here is
    silent.
    """
    info = os.fstat(handle)
    # Whoever ran the sudo, or failing that whoever we are - not root by
    # assumption, since btkey need not have got here through sudo.
    expected = owner[0] if owner is not None else os.geteuid()
    return info.st_uid == expected and not info.st_mode & 0o077


def make(path, log):
    """Create the FIFO and return a descriptor, or None.

    Opened O_RDWR so the descriptor never reports end-of-file when the last
    writer closes - otherwise the watch would remove itself after the first
    command.
    """
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o755, exist_ok=True)

        if os.path.lexists(path) and not stat.S_ISFIFO(os.lstat(path).st_mode):
            log("%s was not a FIFO; replacing it" % path)
            os.unlink(path)
        if not os.path.exists(path):
            os.mkfifo(path, 0o600)

        # Unconditionally, not just on the ones we create: a FIFO left
        # behind by an earlier run keeps whatever mode it had.
        os.chmod(path, 0o600)
        owner = invoking_user()
        if owner is not None:
            try:
                os.chown(path, *owner)
            except OSError as exc:
                log("could not hand %s to uid %d: %s"
                    % (path, owner[0], exc.strerror))

        handle = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        if not _is_private(handle, owner):
            log("%s is reachable by other users; not listening on it" % path)
            os.close(handle)
            return None
        return handle
    except OSError as exc:
        log("no FIFO at %s: %s" % (path, exc.strerror))
        return None
