# SPDX-License-Identifier: GPL-3.0-or-later
"""Liveness and quality from the transport stream a chain writes.  count() is the liveness
the tuning loop uses (MPEG-2 sequence headers = pictures are arriving); the snapshot carries
packets, TEI flags, continuity errors and nulls, the numbers a decision is judged on.  Reads
only the bytes written since the last call and survives the chain's rotation of the file.

What the numbers mean on an ATSC 1.0 stream (measured 2026-11-09): nulls at a flat ~2 % are
the multiplex's own stuffing; RS-uncorrectable packets that a scrubber turned into nulls show
as long null runs; continuity errors in bursts across every PID are output gaps."""
import os
import threading

SEQ_HEADER = b"\x00\x00\x01\xb3"
TS_PKT = 188


class TsMeter:
    def __init__(self, path: str):
        self.path = path
        self._pos, self._tail = 0, b""
        self.headers = self.packets = self.tei = self.cc_err = self.nulls = 0
        self._cc = {}
        self._lock = threading.Lock()

    def _ingest(self, data: bytes) -> None:
        blob = self._tail + data
        self.headers += blob.count(SEQ_HEADER)
        i, n = 0, len(blob)
        while i + TS_PKT <= n:
            if blob[i] != 0x47:
                i += 1
                continue
            if i + TS_PKT < n and blob[i + TS_PKT] != 0x47 and i + 2 * TS_PKT <= n:
                i += 1                       # a 0x47 inside a payload, not a sync byte
                continue
            pkt = blob[i:i + TS_PKT]
            self.packets += 1
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pkt[1] & 0x80:
                self.tei += 1
            if pid == 0x1FFF:
                self.nulls += 1
            elif pkt[3] & 0x10:
                cc = pkt[3] & 0x0F
                last = self._cc.get(pid)
                if last is not None and cc != (last + 1) & 0x0F and not (pkt[1] & 0x80):
                    self.cc_err += 1
                self._cc[pid] = cc
            i += TS_PKT
        self._tail = blob[max(0, i - 3):] if i < n else b""
        if len(self._tail) > 2 * TS_PKT:
            self._tail = self._tail[-2 * TS_PKT:]

    def count(self) -> int:
        with self._lock:
            try:
                size = os.path.getsize(self.path)
                if size < self._pos:                   # rotated / recreated
                    self._pos, self._tail, self._cc = 0, b"", {}
                with open(self.path, "rb") as f:
                    f.seek(self._pos)
                    data = f.read()
            except OSError:
                return self.headers
            self._pos += len(data)
            if data:
                self._ingest(data)
            return self.headers

    def snapshot(self) -> dict:
        with self._lock:
            return {"packets": self.packets, "tei": self.tei, "cc_errors": self.cc_err,
                    "nulls": self.nulls, "seq_headers": self.headers}
