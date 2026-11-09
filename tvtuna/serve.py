# SPDX-License-Identifier: GPL-3.0-or-later
"""tvTuna as a network tuner: our receivers behind any IPTV player, OnAir's included.

  GET /playlist.m3u     the channel table as an M3U playlist (#EXTINF with tvg-id, group-title)
  GET /ch/<rf>.ts       live MPEG-TS over HTTP at the live edge of that channel
  GET /status.json      what is tuned, who holds the radio, the stream numbers
  GET /guide.xml        XMLTV (empty skeleton until the guide parsers are wired)

One radio, so one channel at a time: the first client for a channel tunes it (chain up, gain
search on decoder truth, servo hold); a client for another channel retunes; when the last
client leaves, the chain stays up for `idle_s` and then stops and releases the radio.
ATSC 1.0 channels stream today.  ATSC 3.0 lanes are fragmented MP4, which OnAir's IPTV player
does not take yet; they are listed in /status.json but not in the playlist until the remux lands."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import atsc1, config, radio, state
from .player import NULL_PKT, TS_PKT, _aligned

BACK_BYTES = 500_000


class Engine:
    """The one tuned channel, shared by every client."""

    def __init__(self, cfg: config.Config, log: Callable[[str], None], idle_s: float = 30.0):
        self.cfg, self.log, self.idle_s = cfg, log, idle_s
        self.lock_obj = threading.Lock()
        self.chain: Optional[atsc1.Atsc1Chain] = None
        self.rf: Optional[int] = None
        self.clients = 0
        self.last_client = time.time()
        self.site = None
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.last_event = {}
        self.ready = threading.Event()
        self.error: Optional[str] = None

    def ts_path(self) -> Optional[str]:
        return self.chain.ts_path if self.chain else None

    def want(self, rf: int) -> bool:
        """A client wants `rf`.  Returns once the chain for it is up (or failed)."""
        with self.lock_obj:
            self.clients += 1
            self.last_client = time.time()
            if self.rf == rf and self.worker and self.worker.is_alive():
                pass
            else:
                self._stop_locked()
                self.rf = rf
                self.ready.clear()
                self.error = None
                self.stop_flag.clear()
                self.worker = threading.Thread(target=self._run, args=(rf,), daemon=True)
                self.worker.start()
        self.ready.wait(200)
        return self.error is None and self.chain is not None

    def leave(self) -> None:
        with self.lock_obj:
            self.clients = max(0, self.clients - 1)
            self.last_client = time.time()

    def _stop_locked(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.worker.join(60)
        self.chain = None

    def _run(self, rf: int) -> None:
        ch = state.channel(rf) or {}
        ant = ch.get("antenna") or self.cfg.antenna_atsc1
        start = state.atsc1_start(rf, ant)
        # A channel with a proven hold on record gets a short confirmation search around it
        # (a viewer wants a picture, not a survey); an unknown one gets the full search.
        known = start != (4, 40)
        coarse, max_cells = (2, 6) if known else (4, 30)
        live_dir = os.path.join(config.STATE_DIR, "live", f"rf{rf}")
        try:
            with radio.site_lock(self.cfg, wait_s=120) as lk:
                chain = atsc1.Atsc1Chain(self.cfg, rf, ant, live_dir, start=start, log=self.log)
                if not chain.start():
                    self.error = "chain did not start"
                    self.ready.set()
                    return
                self.chain = chain
                self.ready.set()

                def on_event(st_, setting, reading):
                    snap = chain.meter.snapshot()
                    self.last_event = {"state": st_, "setting": setting, "mer_db": reading.value,
                                       "video": reading.alive, "pictures_per_s": reading.live_rate, **snap,
                                       "t": time.time()}

                def idle():
                    return self.stop_flag.is_set() or (self.clients == 0 and time.time() - self.last_client > self.idle_s)

                try:
                    rep = chain.serve(10 * 365 * 86400.0, lk, on_event=on_event, until=idle,
                                      coarse=coarse, max_cells=max_cells,
                                      start={"gain:RFGR": [start[0]], "gain:IFGR": [start[1]]} if known else None)
                    if rep.held:
                        dial = rep.verdicts[-1].dial if rep.verdicts else None
                        state.remember_atsc1_hold(rf, ant, rep.held, dial, rep.proven)
                finally:
                    chain.stop()
        except Exception as e:                            # noqa: BLE001
            self.error = str(e)
            self.log(f"engine RF {rf}: {e}")
        finally:
            self.ready.set()
            with self.lock_obj:
                if self.rf == rf:
                    self.chain = None
            self.log(f"RF {rf} released")


def playlist(base: str) -> str:
    d = state.load()
    lines = ["#EXTM3U url-tvg=\"%s/guide.xml\"" % base]
    for rf in sorted(d.get("channels", {}), key=int):
        ch = d["channels"][rf]
        std = ch.get("standard") or ("atsc1" if ch.get("atsc1_hold") else None)
        if std != "atsc1":
            continue
        holds = ch.get("atsc1_hold", {})
        q = ch.get("last_scan", {}).get("mer_db") or next((h.get("mer_db") for h in holds.values() if h.get("mer_db")), None)
        name = f"RF {rf}" + (f" ({q:.0f} dB)" if q else "")
        lines.append(f'#EXTINF:-1 tvg-id="rf{rf}" tvg-name="{name}" group-title="ATSC 1.0",{name}')
        lines.append(f"{base}/ch/{rf}.ts")
    return "\n".join(lines) + "\n"


def make_handler(engine: Engine, log):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # quiet; tvTuna's own log speaks
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = self.headers.get("Host", "localhost")
            base = f"http://{host}"
            if self.path in ("/", "/playlist.m3u", "/playlist.m3u8"):
                return self._send(200, playlist(base).encode(), "audio/x-mpegurl")
            if self.path == "/guide.xml":
                return self._send(200, b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="tvtuna"></tv>\n',
                                  "application/xml")
            if self.path == "/status.json":
                d = {"tuned_rf": engine.rf, "clients": engine.clients, "radio": radio.holder(engine.cfg),
                     "last": engine.last_event, "error": engine.error, "channels": state.load().get("channels", {})}
                return self._send(200, json.dumps(d, indent=1, default=str).encode(), "application/json")
            if self.path.startswith("/ch/") and self.path.endswith(".ts"):
                try:
                    rf = int(self.path[4:-3])
                except ValueError:
                    return self._send(404, b"no such channel", "text/plain")
                return self._stream(rf)
            self._send(404, b"not found", "text/plain")

        def _stream(self, rf: int):
            log(f"client {self.client_address[0]} wants RF {rf}")
            if not engine.want(rf):
                engine.leave()
                return self._send(503, f"RF {rf}: {engine.error or 'not available'}".encode(), "text/plain")
            path = engine.ts_path()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            pos = None
            quiet = None
            try:
                while engine.rf == rf and engine.chain is not None:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        time.sleep(0.3)
                        continue
                    if pos is None or size < pos:
                        pos = max(0, size - BACK_BYTES) if pos is None else 0
                        with open(path, "rb") as f:
                            f.seek(pos)
                            pos += _aligned(f.read(3 * TS_PKT))
                    if size > pos:
                        with open(path, "rb") as f:
                            f.seek(pos)
                            data = f.read(min(size - pos, 1_000_000))
                        pos += len(data)
                        self.wfile.write(data)
                        quiet = None
                    else:
                        quiet = quiet or time.time()
                        if time.time() - quiet > 0.5:
                            self.wfile.write(NULL_PKT * 20)
                            quiet = time.time()
                        time.sleep(0.1)
            except (ConnectionError, OSError):
                pass
            finally:
                engine.leave()
                log(f"client {self.client_address[0]} left RF {rf}")

    return Handler


def run(cfg: config.Config, host: str, port: int, log: Callable[[str], None], idle_s: float = 30.0) -> int:
    engine = Engine(cfg, log, idle_s=idle_s)
    srv = ThreadingHTTPServer((host, port), make_handler(engine, log))
    srv.daemon_threads = True
    log(f"tvtuna serving on http://{host}:{port}/playlist.m3u  (status: /status.json)")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        with engine.lock_obj:
            engine._stop_locked()
        srv.server_close()
    return 0
