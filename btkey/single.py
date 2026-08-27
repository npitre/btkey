# SPDX-License-Identifier: GPL-2.0-only
"""One btkey at a time.

A second btkey does not merely fail, it takes the first one down with it.
Its private bluetoothd cannot start, since the first already owns
`org.bluez`; failing to start, it undoes itself, and undoing itself means
starting `bluetooth.service` underneath the instance that is still running.
On the way out it resets the scrolling region of a console it does not own.
None of that is reachable if it never gets that far.

A lock file rather than a pid file: the kernel releases it however the
holder dies, so there is no stale lock to reason about and no pid to check
for having been recycled.  The guardian inherits it and holds it until its
own cleanup is finished, which is right - the moment to allow another btkey
is when the last one has finished putting things back, not when it died.
"""

import errno
import fcntl
import os

LOCK_FILE = "/run/btkey/lock"


def hold(who="", path=LOCK_FILE):
    """Take the lock.

    Returns (descriptor, None) having taken it, or (None, description) when
    another btkey holds it, where the description is what that one wrote
    about itself and may be empty if it had not got that far.

    The descriptor has to be kept for as long as btkey runs: closing it
    drops the lock.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o755, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            os.close(handle)
            raise
        try:
            held = os.read(handle, 256).decode("utf-8", "replace").strip()
        except OSError:
            held = ""
        os.close(handle)
        return None, held
    os.ftruncate(handle, 0)
    os.write(handle, ("pid %d%s\n" % (os.getpid(),
                                      ", started by " + who if who else ""))
             .encode("utf-8"))
    return handle, None
