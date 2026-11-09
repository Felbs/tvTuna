# SPDX-License-Identifier: GPL-3.0-or-later
"""tvTuna as a network tuner: our receivers behind any IPTV player, OnAir's included.

  GET /playlist.m3u     the channel table as an M3U playlist (#EXTINF with tvg-id, group-title)
  GET /ch/<rf>.ts       live MPEG-TS over HTTP at the live edge of that channel
  GET /guide.xml        XMLTV: virtual channels and programmes from the decoded PSIP
  GET /status.json      what is tuned, who holds the radio, the stream numbers

One radio, so one channel at a time: the first client for a channel tunes it; a client for
another channel retunes; when the last client leaves, the chain stays up for `idle_s` and
then stops and releases the radio.

ATSC 1.0: tvTuna holds the radio, runs the STVT chain once, searches and servos the gain on
decoder truth, and tails the chain's live.ts.  ATSC 3.0: the atsc3 receiver's orchestrator
(chain + supervisor + audio and caption workers + its muxer with no window) is launched with
the remembered gains; the chain takes the site warden itself, and the muxer's transport-stream
sink (HEVC copied, audio as MP2 or AC-3, captions as a soft track) is what the client gets.
IPTV players take MPEG-TS; the fragmented-MP4 lanes themselves they do not."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import atsc1, atsc3, config, guide, radio, state
from .player import NULL_PKT, TS_PKT, _aligned

BACK_BYTES = 500_000
ATSC3_SINK_WAIT_S = 240.0        # LDM pool warm-up, lane discovery, first muxed chunk


def standard_of(ch: dict) -> Optional[str]:
    return ch.get("standard") or ("atsc1" if ch.get("atsc1_hold") else "atsc3" if ch.get("atsc3_gains") else None)


class Engine:
    """The one tuned channel, shared by every client."""

    def __init__(self, cfg: config.Config, log: Callable[[str], None], idle_s: float = 30.0):
        self.cfg, self.log, self.idle_s = cfg, log, idle_s
        self.lock_obj = threading.Lock()
        self.rf: Optional[int] = None
        self.standard: Optional[str] = None
        self.ts_path: Optional[str] = None
        self.clients = 0
        self.last_client = time.time()
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.last_event: dict = {}
        self.ready = threading.Event()
        self.error: Optional[str] = None
        self.up = False

    def want(self, rf: int) -> bool:
        """A client wants `rf`.  Returns once a stream file for it exists (or the tune failed)."""
        with self.lock_obj:
            self.clients += 1
            self.last_client = time.time()
            if not (self.rf == rf and self.worker and self.worker.is_alive()):
                self._stop_locked()
                ch = state.channel(rf) or {}
                self.rf, self.standard = rf, standard_of(ch)
                self.ready.clear()
                self.error, self.up, self.ts_path = None, False, None
                self.stop_flag.clear()
                target = self._run_atsc3 if self.standard == "atsc3" else self._run_atsc1
                self.worker = threading.Thread(target=target, args=(rf, ch), daemon=True)
                self.worker.start()
        self.ready.wait(ATSC3_SINK_WAIT_S + 30)
        return self.error is None and self.up

    def leave(self) -> None:
        with self.lock_obj:
            self.clients = max(0, self.clients - 1)
            self.last_client = time.time()

    def idle(self) -> bool:
        return self.stop_flag.is_set() or (self.clients == 0 and time.time() - self.last_client > self.idle_s)

    def _stop_locked(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.worker.join(90)
        self.up, self.ts_path = False, None

    # ---- ATSC 1.0 -------------------------------------------------------------------------
    def _run_atsc1(self, rf: int, ch: dict) -> None:
        ant = ch.get("antenna") or self.cfg.antenna_atsc1
        start = state.atsc1_start(rf, ant)
        known = start != (4, 40)                 # a proven hold on record: a short confirmation search
        coarse, max_cells = (2, 6) if known else (4, 30)
        live_dir = os.path.join(config.STATE_DIR, "live", f"rf{rf}")
        try:
            with radio.site_lock(self.cfg, wait_s=120) as lk:
                chain = atsc1.Atsc1Chain(self.cfg, rf, ant, live_dir, start=start, log=self.log)
                if not chain.start():
                    self.error = "chain did not start"
                    return
                self.ts_path, self.up = chain.ts_path, True
                self.ready.set()

                def on_event(st_, setting, reading):
                    snap = chain.meter.snapshot()
                    self.last_event = {"standard": "atsc1", "state": st_, "setting": setting, "mer_db": reading.value,
                                       "video": reading.alive, "pictures_per_s": reading.live_rate, **snap, "t": time.time()}
                try:
                    rep = chain.serve(10 * 365 * 86400.0, lk, on_event=on_event, until=self.idle,
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
            self._done(rf)

    # ---- ATSC 3.0 -------------------------------------------------------------------------
    def _run_atsc3(self, rf: int, ch: dict) -> None:
        ant = ch.get("antenna") or self.cfg.antenna_atsc3
        live_dir = os.path.join(config.STATE_DIR, "live", f"rf{rf}")
        os.makedirs(live_dir, exist_ok=True)
        sink = os.path.join(live_dir, "tv.ts")
        try:
            os.unlink(sink)
        except OSError:
            pass
        gains = atsc3.verdict_for(self.cfg, rf, ant)
        extra = f"--rfgain {gains['rfgain_sel']} --ifgr {gains['ifgr']}" if gains and gains.get("rfgain_sel") is not None else ""
        cmd = [self.cfg.python, os.path.join(self.cfg.atsc3_root, "tools", "atsc3_watch_av.py"), "--rf", str(rf),
               "--ant", ant, "--live-dir", live_dir, "--python", self.cfg.python, "--cc",
               "--tv-extra", f"--player none --out {sink}"]
        if extra:
            cmd += ["--chain-extra", extra]
        mod = atsc3.supervisor_module(self.cfg)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=self.cfg.atsc3_root, env=self.cfg.chain_env(), creationflags=flags,
                                    stdout=open(os.path.join(live_dir, "orchestrator.log"), "ab", buffering=0),
                                    stderr=subprocess.STDOUT)
            self.log(f"ATSC 3.0 up on RF {rf} ({ant}); orchestrator pid {proc.pid}; waiting for the muxed stream")
            t0 = time.time()
            while proc.poll() is None and not self.idle():
                h = atsc3.health(mod, live_dir)
                try:
                    size = os.path.getsize(sink)
                except OSError:
                    size = 0
                self.last_event = {"standard": "atsc3", "t": time.time(), "sink_bytes": size, **h}
                if size > 188 * 200 and not self.up:
                    self.ts_path, self.up = sink, True
                    self.ready.set()
                    self.log(f"RF {rf}: muxed stream flowing after {time.time() - t0:.0f} s "
                             f"(frames {h.get('frames')}, FEC {h.get('fec_pct')} %)")
                if not self.up and time.time() - t0 > ATSC3_SINK_WAIT_S:
                    self.error = f"no muxed stream within {ATSC3_SINK_WAIT_S:.0f} s (frames {h.get('frames')}, FEC {h.get('fec_pct')} %)"
                    break
                time.sleep(1.0)
            if proc.poll() is not None and not self.up:
                self.error = f"orchestrator exited rc {proc.returncode}"
        except Exception as e:                            # noqa: BLE001
            self.error = str(e)
            self.log(f"engine RF {rf}: {e}")
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        import signal
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        proc.terminate()
                    proc.wait(timeout=60)
                except Exception:                         # noqa: BLE001
                    pass
            self._done(rf)

    def _done(self, rf: int) -> None:
        self.ready.set()
        with self.lock_obj:
            if self.rf == rf:
                self.up, self.ts_path = False, None
        self.log(f"RF {rf} released")


def playlist(base: str) -> str:
    d = state.load()
    lines = ['#EXTM3U url-tvg="%s/guide.xml"' % base]
    for rf in sorted(d.get("channels", {}), key=int):
        ch = d["channels"][rf]
        std = standard_of(ch)
        if std is None:
            continue
        if std == "atsc1":
            holds = ch.get("atsc1_hold", {})
            q = ch.get("last_scan", {}).get("mer_db") or next((h.get("mer_db") for h in holds.values() if h.get("mer_db")), None)
            name = f"RF {rf}" + (f" ({q:.0f} dB)" if q else "")
            group = "ATSC 1.0"
        else:
            gains = ch.get("atsc3_gains", {})
            head = next((g.get("headline") for g in gains.values() if g.get("headline")), None)
            name = f"RF {rf}" + (f" ({head.split(',')[0]})" if head else "")
            group = "ATSC 3.0"
        lines.append(f'#EXTINF:-1 tvg-id="rf{rf}" tvg-name="{name}" group-title="{group}",{name}')
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
                return self._send(200, guide.xmltv(engine.cfg, log).encode(), "application/xml")
            if self.path == "/status.json":
                d = {"tuned_rf": engine.rf, "standard": engine.standard, "up": engine.up, "clients": engine.clients,
                     "radio": radio.holder(engine.cfg), "last": engine.last_event, "error": engine.error,
                     "channels": state.load().get("channels", {})}
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
            path = engine.ts_path
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            pos = None
            quiet = None
            try:
                while engine.rf == rf and engine.up:
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
