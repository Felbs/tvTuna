# SPDX-License-Identifier: GPL-3.0-or-later
"""A player at the live edge of a growing transport-stream file.

The chain writes live.ts; a tail thread opens it near the end, aligns to a sync byte, and
pipes new bytes to the player's stdin, sending null packets when the chain is quiet so the
demuxer never times out.  Rotation (the chain reopening the file) is a size drop and the
tail starts over.  If the player window is closed, that is the viewer's wish to stop."""
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

TS_PKT = 188
NULL_PKT = b"\x47\x1f\xff\x10" + b"\xff" * 184
BACK_BYTES = 1_000_000


def _aligned(buf: bytes) -> int:
    for i in range(min(len(buf) - 2 * TS_PKT, TS_PKT)):
        if buf[i] == 0x47 and buf[i + TS_PKT] == 0x47:
            return i
    return 0


_TABLE_PIDS = (0x0000, 0x0011, 0x1000)       # PAT, SDT, and the PMT where ffmpeg's muxer puts it


def _key(buf: bytes, o: int) -> bool:
    """A packet that starts a unit and carries the random-access flag."""
    return (buf[o] == 0x47 and bool(buf[o + 1] & 0x40) and bool(buf[o + 3] & 0x20)
            and buf[o + 4] > 0 and bool(buf[o + 5] & 0x40))


def _pid(buf: bytes, o: int) -> int:
    return ((buf[o + 1] & 0x1F) << 8) | buf[o + 2]


def join_point(path: str, size: int, scan: int = 6_000_000) -> Optional[int]:
    """Where a new client of an ATSC 3.0 sink starts: the last place a decoder can.

    The receiver's muxer writes the sink one ffmpeg run per 2 s slot, appended: the tables (SDT,
    PAT, PMT), then the slot's first picture, a keyframe, flagged random-access.  A client started
    a fixed number of bytes back lands inside a slot, and its decoder throws pictures away until
    the next slot arrives: up to 2 s of nothing after the first byte (measured 2026-11-10, a join
    1 s into the first slot: 140 decoder errors, no picture until the second).

    -> the offset of the tables in front of the last such keyframe within the last `scan` bytes,
    or None when the file is not laid out that way (a broadcast transport stream, for one); the
    caller then keeps its byte rule.  The video stream is taken to be the one whose keyframe
    opens the file."""
    try:
        with open(path, "rb") as f:
            head = f.read(40 * TS_PKT)
            start = max(0, (size - scan) // TS_PKT * TS_PKT)
            f.seek(start)
            buf = f.read(size - start)
    except OSError:
        return None
    vpid = next((_pid(head, o) for o in range(0, len(head) - TS_PKT + 1, TS_PKT) if _key(head, o)), None)
    if vpid is None:
        return None
    for o in range((len(buf) // TS_PKT - 1) * TS_PKT, -1, -TS_PKT):
        if _pid(buf, o) == vpid and _key(buf, o):
            while o >= TS_PKT and _pid(buf, o - TS_PKT) in _TABLE_PIDS:
                o -= TS_PKT
            return start + o
    return None


def player_cmd(kind: str, title: str) -> Optional[list]:
    if kind == "ffplay":
        return ["ffplay", "-hide_banner", "-loglevel", "error", "-window_title", title,
                "-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay",
                "-probesize", "4000000", "-analyzeduration", "4000000", "-i", "pipe:0"]
    if kind == "mpv":
        return ["mpv", "--title=" + title, "--cache=yes", "--demuxer-lavf-probesize=4000000",
                "--demuxer-lavf-analyzeduration=4", "--no-terminal", "-"]
    return None


class TsPlayer:
    def __init__(self, path: str, kind: str, title: str, log: Callable[[str], None] = print):
        self.path, self.kind, self.title, self.log = path, kind, title, log
        self.proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.forwarded = 0
        self.exited_by_user = False

    def start(self) -> bool:
        cmd = player_cmd(self.kind, self.title)
        if cmd is None:
            return False
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()
        return True

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _tail(self) -> None:
        pos = None
        quiet_since = None
        while not self._stop.is_set() and self.alive():
            try:
                size = os.path.getsize(self.path)
            except OSError:
                time.sleep(0.5)
                continue
            if pos is None or size < pos:
                pos = max(0, size - BACK_BYTES) if pos is None else 0
                try:
                    with open(self.path, "rb") as f:
                        f.seek(pos)
                        head = f.read(3 * TS_PKT)
                    pos += _aligned(head)
                except OSError:
                    time.sleep(0.5)
                    continue
            if size > pos:
                try:
                    with open(self.path, "rb") as f:
                        f.seek(pos)
                        data = f.read(min(size - pos, 2_000_000))
                    pos += len(data)
                    self.proc.stdin.write(data)
                    self.forwarded += len(data)
                    quiet_since = None
                except (OSError, ValueError):
                    break
            else:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 0.5:
                    try:
                        self.proc.stdin.write(NULL_PKT * 20)
                    except (OSError, ValueError):
                        break
                    quiet_since = time.time()
                time.sleep(0.1)
        if not self._stop.is_set() and not self.alive():
            self.exited_by_user = True

    def stop(self) -> None:
        self._stop.set()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
