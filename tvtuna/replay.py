# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine protocol with no radio and no receiver behind it: a recorded transport stream
played at its own rate, in a loop.

    tvtuna engine --replay recording.ts

This is the first thing to try from another program (OnAir's External engine mode): it proves
the process, the line protocol, the socket and the player with nothing installed but this
package.  Every `tune` is answered with the same file.  The status lines carry what was counted
in the bytes sent (packets, continuity errors, TEI, nulls) and say plainly that this is a replay:
there is no servo, no MER and no radio holder, and those fields are null.  `video` is true while
the file's packets are going out to a connected reader (nothing here decodes them)."""
import json
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

from .tsmeter import TsMeter

TS_PKT = 188
SYNC = 0x47
NULL_PKT = bytes([SYNC, 0x1F, 0xFF, 0x10]) + bytes([0xFF]) * (TS_PKT - 4)
DEFAULT_RATE = 19_392_658 / 8.0          # bytes per second of an ATSC 1.0 multiplex, the fallback


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def first_sync(blob: bytes) -> int:
    """Offset of the first byte that starts three packets in a row, or 0."""
    for i in range(0, min(len(blob), 4 * TS_PKT)):
        if all(i + k * TS_PKT < len(blob) and blob[i + k * TS_PKT] == SYNC for k in range(3)):
            return i
    return 0


def byte_rate(path: str, scan_bytes: int = 40_000_000) -> Optional[float]:
    """Bytes per second from the stream's own clock: the byte distance between the first and the
    last PCR of one PID over their time distance.  The PID with the most PCRs is the one read (a
    recording that starts before the receiver locked opens with junk packets, some with a PCR flag
    set by chance).  None when the file carries no usable PCR."""
    with open(path, "rb") as f:
        blob = f.read(scan_bytes)
    i = first_sync(blob)
    seen = {}
    while i + TS_PKT <= len(blob):
        if blob[i] != SYNC:
            i += 1
            continue
        b1, b2, b3 = blob[i + 1], blob[i + 2], blob[i + 3]
        if (b3 & 0x20) and blob[i + 4] >= 7 and (blob[i + 5] & 0x10) and not (b1 & 0x80):
            p = blob[i + 6:i + 12]
            base = (p[0] << 25) | (p[1] << 17) | (p[2] << 9) | (p[3] << 1) | (p[4] >> 7)
            seen.setdefault(((b1 & 0x1F) << 8) | b2, []).append((i, base * 300 + (((p[4] & 1) << 8) | p[5])))
        i += TS_PKT
    if not seen:
        return None
    marks = max(seen.values(), key=len)
    first = last = marks[0]
    for m in marks[1:]:
        if m[1] <= last[1]:                         # the clock went back: a splice; measure up to it
            break
        last = m
    dt = (last[1] - first[1]) / 27e6
    if dt < 0.5:
        return None
    return (last[0] - first[0]) / dt


class Replay:
    """Sends the file to one client at `rate` bytes per second, from the start, again at the end."""

    def __init__(self, path: str, host: str, port: int, rate: float, log: Callable[[str], None]):
        self.path, self.rate, self.log = path, rate, log
        self.meter = TsMeter(path)
        self.loops = 0
        self.playing = False
        self.sent_at = 0.0                           # time.monotonic() of the last file bytes sent
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        current: Optional[socket.socket] = None
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                break
            if current is not None:
                try:
                    current.close()
                except OSError:
                    pass
            current = conn
            threading.Thread(target=self._feed, args=(conn,), daemon=True).start()

    def _feed(self, conn: socket.socket) -> None:
        step = 0.05
        chunk = max(TS_PKT, int(self.rate * step) // TS_PKT * TS_PKT)
        try:
            while not self._stop.is_set():
                if not self.playing:
                    conn.sendall(NULL_PKT * 20)
                    time.sleep(0.5)
                    continue
                with open(self.path, "rb") as f:
                    f.seek(first_sync(f.read(4 * TS_PKT)))
                    due = time.monotonic()
                    while self.playing and not self._stop.is_set():
                        data = f.read(chunk)
                        if len(data) < TS_PKT:
                            break
                        data = data[:len(data) // TS_PKT * TS_PKT]
                        conn.sendall(data)
                        self.sent_at = time.monotonic()
                        with self.meter._lock:
                            self.meter._ingest(data)
                        due += len(data) / self.rate
                        wait = due - time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                        elif wait < -1.0:           # the reader stalled: do not burst to catch up
                            due = time.monotonic()
                if self.playing:
                    self.loops += 1
                    with self.meter._lock:          # the seam is the file's end, not a reception fault
                        self.meter._cc, self.meter._tail = {}, b""
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass


def run(path: str, host: str, port: int, log: Callable[[str], None], rate_mbps: float = 0.0) -> int:
    if not os.path.isfile(path):
        _emit({"ev": "error", "msg": f"no such file: {path}"})
        return 2
    measured = byte_rate(path)
    rate = rate_mbps * 1e6 / 8.0 if rate_mbps > 0 else (measured or DEFAULT_RATE)
    log(f"replay {path}: {os.path.getsize(path)} bytes at {rate * 8 / 1e6:.2f} Mb/s "
        f"({'given' if rate_mbps > 0 else 'from its PCR' if measured else 'no PCR found, ATSC 1.0 rate assumed'})")
    rp = Replay(path, host, port, rate, log)
    _emit({"ev": "hello", "version": __import__("tvtuna").__version__, "tcp": rp.port, "channels": {},
           "radio": None, "replay": os.path.basename(path)})
    tuned: Optional[int] = None
    stop_all = threading.Event()

    def status_line() -> dict:
        flowing = rp.playing and time.monotonic() - rp.sent_at < 1.0
        last = dict(rp.meter.snapshot(), standard="replay", state="replay (no radio)", setting=None, mer_db=None,
                    video=flowing, pictures_per_s=None,
                    loops=rp.loops, mbps=round(rate * 8 / 1e6, 2), t=time.time())
        return {"ev": "status", "rf": tuned, "standard": "replay", "up": rp.playing, "radio": None,
                "last": last, "error": None, "t": time.time()}

    def ticker():
        while not stop_all.is_set():
            if tuned is not None:
                _emit(status_line())
            stop_all.wait(2.0)
    threading.Thread(target=ticker, daemon=True).start()

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmd = json.loads(raw)
            except ValueError:
                _emit({"ev": "error", "msg": f"not JSON: {raw[:80]}"})
                continue
            c = cmd.get("cmd")
            if c == "tune":
                tuned, rp.playing = int(cmd["rf"]), True
                _emit({"ev": "up", "rf": tuned, "standard": "replay", "ts": path, "tcp": rp.port})
            elif c == "status":
                _emit(status_line())
            elif c in ("stop", "quit"):
                if tuned is not None:
                    rf_was, tuned, rp.playing = tuned, None, False
                    _emit({"ev": "released", "rf": rf_was})
                if c == "quit":
                    break
            else:
                _emit({"ev": "error", "msg": f"unknown cmd {c!r}"})
    except KeyboardInterrupt:
        pass
    finally:
        stop_all.set()
        rp.close()
    return 0
