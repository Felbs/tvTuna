# SPDX-License-Identifier: GPL-3.0-or-later
"""tvTuna as an engine for another program's receiver: a line protocol on stdin / stdout and
the transport stream on a raw local TCP socket (or read from the file the "up" event names).

  stdin  -> {"cmd": "tune", "rf": 34}     tune a channel (the table knows its standard and port)
            {"cmd": "status"}             one status line now
            {"cmd": "stop"}               release the radio
            {"cmd": "quit"}               stop and exit
  stdout <- {"ev": "hello", ...}          once, with the channel table
            {"ev": "up", "rf": 34, "standard": "atsc1", "ts": "<path>", "tcp": 8555}
            {"ev": "status", "rf": 34, "up": true, "radio": {...}, "last": {...}}   every 2 s while tuned
            {"ev": "error", "rf": 34, "msg": "..."}
            {"ev": "released", "rf": 34}

This is the seam for OnAir's external-engine receiver mode (atsc3-private docs/ONAIR_FORK.md,
route 2): their core spawns this, writes tune, reads the TS from the socket into its player,
and maps the status fields onto its panels.  One client on the socket at a time; a second
connection replaces the first."""
import json
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

from . import config, radio, serve, state
from .player import NULL_PKT, TS_PKT, _aligned


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


class TsSocket:
    """A raw TCP server that pushes the tuned channel's TS at the live edge to one client."""

    def __init__(self, engine: serve.Engine, host: str, port: int, log: Callable[[str], None]):
        self.engine, self.log = engine, log
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
                conn, addr = self.srv.accept()
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
        pos = None
        quiet = None
        rf = self.engine.rf
        try:
            while not self._stop.is_set():
                path = self.engine.ts_path
                if not self.engine.up or path is None or self.engine.rf != rf:
                    if self.engine.up and self.engine.rf != rf:           # retuned under us: follow
                        rf, pos = self.engine.rf, None
                        continue
                    conn.sendall(NULL_PKT * 20)
                    time.sleep(0.5)
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    time.sleep(0.3)
                    continue
                if pos is None or size < pos:
                    pos = max(0, size - serve.BACK_BYTES) if pos is None else 0
                    with open(path, "rb") as f:
                        f.seek(pos)
                        pos += _aligned(f.read(3 * TS_PKT))
                if size > pos:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        data = f.read(min(size - pos, 1_000_000))
                    pos += len(data)
                    conn.sendall(data)
                    quiet = None
                else:
                    quiet = quiet or time.time()
                    if time.time() - quiet > 0.5:
                        conn.sendall(NULL_PKT * 20)
                        quiet = time.time()
                    time.sleep(0.1)
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


def run(cfg: config.Config, host: str, port: int, log: Callable[[str], None]) -> int:
    engine = serve.Engine(cfg, log, idle_s=5.0)
    ts = TsSocket(engine, host, port, log)
    _emit({"ev": "hello", "version": __import__("tvtuna").__version__, "tcp": ts.port,
           "channels": state.load().get("channels", {}), "radio": radio.holder(cfg)})
    tuned: Optional[int] = None
    stop_all = threading.Event()

    def status_line() -> dict:
        return {"ev": "status", "rf": engine.rf, "standard": engine.standard, "up": engine.up,
                "radio": radio.holder(cfg), "last": engine.last_event, "error": engine.error, "t": time.time()}

    def ticker():
        while not stop_all.is_set():
            if tuned is not None:
                _emit(status_line())
            stop_all.wait(2.0)
    threading.Thread(target=ticker, daemon=True).start()

    def do_tune(rf: int):
        nonlocal tuned
        if tuned is not None and tuned != rf:
            engine.leave()
        tuned = rf
        ok = engine.want(rf)
        if ok:
            _emit({"ev": "up", "rf": rf, "standard": engine.standard, "ts": engine.ts_path, "tcp": ts.port})
        else:
            _emit({"ev": "error", "rf": rf, "msg": engine.error or "not available"})
            engine.leave()
            tuned = None

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
                threading.Thread(target=do_tune, args=(int(cmd["rf"]),), daemon=True).start()
            elif c == "status":
                _emit(status_line())
            elif c in ("stop", "quit"):
                if tuned is not None:
                    engine.leave()
                    rf_was, tuned = tuned, None
                    with engine.lock_obj:
                        engine._stop_locked()
                    _emit({"ev": "released", "rf": rf_was})
                if c == "quit":
                    break
            else:
                _emit({"ev": "error", "msg": f"unknown cmd {c!r}"})
    except KeyboardInterrupt:
        pass
    finally:
        stop_all.set()
        with engine.lock_obj:
            engine._stop_locked()
        ts.close()
    return 0
