# SPDX-License-Identifier: GPL-2.0-only
"""btkey - turn a Linux console VT into a Bluetooth HID keyboard."""

import os

__version__ = "0.1.1"


def from_checkout():
    """Is this the source tree, rather than something installed?

    Worked out from the layout rather than stamped in at build time: an
    installation puts the package on its own under a library directory,
    while a checkout has it beside the things only a checkout has.  So
    nothing has to be rewritten on the way in, and a copied-out tree is
    still recognised for what it is.

    It decides only the defaults a developer wants and a user does not,
    which so far is the log file.
    """
    beside = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return all(os.path.exists(os.path.join(beside, name))
               for name in ("Makefile", "tests", os.path.join("bin", "btkey")))
