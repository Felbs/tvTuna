# SPDX-License-Identifier: GPL-3.0-or-later
"""tvTuna: one command for live over-the-air television on an SDR, ATSC 1.0 and ATSC 3.0.

The receivers are not here.  ATSC 1.0 is the Software TV Tuner's 8-VSB chain; ATSC 3.0 is the
atsc3 receiver.  What this package adds is the part a television needs around a receiver:
one radio shared by a site (the warden), a gain search and a servo judged on decoder truth
(gr-rxtune), a supervisor that tells a fade from a wedge, a player at the live edge, a channel
table that remembers what locked and at which gain, and a status line a human can read."""
__version__ = "0.1.0"
