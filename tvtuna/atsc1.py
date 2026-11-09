# SPDX-License-Identifier: GPL-3.0-or-later
"""ATSC 1.0 through the Software TV Tuner's 8-VSB chain, run ONCE and driven live.

The chain owns the SDR and writes live.ts; with its persistent-retune control it takes gain
changes at run time without re-acquiring (a same-channel retune is gain-only on its side).
Dial = MER from the equaliser's field-sync training error (20 log10(5 / fs_err_rms), data
cliff 15.2 dB).  Liveness = pictures arriving in live.ts (TsMeter).  gr-rxtune searches the
two gain knobs on that truth and then holds, probes a neighbour only when the margin is thin,
and re-searches only when the decode is lost -- OnAir's three servo ideas, judged on the
decoder's own output rather than on a signal meter."""
import dataclasses
import json
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from . import config
from .tsmeter import TsMeter

CHAIN_ENV = {"STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1", "STVT_RS": "stock",
             "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
             "STVT_EQ_LKG_RMS": "1.0", "STVT_EQ_TELEM": "1", "STVT_EQ_TELEM_EVERY": "1",
             "STVT_PERSIST_RETUNE": "1"}
RFGR_SPAN = (0, 12)          # RSPdx LNA states worth searching for TV
IFGR_SPAN = (20, 59)


class RetuneFrontend:
    """rxtune Frontend: the two gain knobs written through the running chain's retune.cmd."""

    def __init__(self, out_dir: str, rf: int, scraper, ack_timeout: float = 30.0):
        config  # noqa: B018  (module import kept explicit for readers)
        from rxtune.knob import FunctionKnob, KnobSpec
        self.cmd_path = os.path.join(out_dir, "retune.cmd")
        self.ack_path = os.path.join(out_dir, "retune.ack")
        self.rf, self.scraper, self.ack_timeout = rf, scraper, ack_timeout
        self.state = {"rfgain_sel": None, "ifgr": None}
        self.writes = 0
        self._FunctionKnob = FunctionKnob
        self._specs = [
            KnobSpec("gain:RFGR", "range", RFGR_SPAN[0], RFGR_SPAN[1], 1, sense="reduction", role="regime",
                     settle_s=4.0, note="rfgain_sel (LNA state) via retune.cmd, gain-only"),
            KnobSpec("gain:IFGR", "range", IFGR_SPAN[0], IFGR_SPAN[1], 1, sense="reduction", role="level",
                     settle_s=4.0, note="IFGR via retune.cmd, gain-only")]
        self._key = {"gain:RFGR": "rfgain_sel", "gain:IFGR": "ifgr"}

    def knobs(self):
        return {s.name: self._FunctionKnob(s, (lambda v, n=s.name: self._set(n, v)),
                                           (lambda n=s.name: self.state[self._key[n]]))
                for s in self._specs}

    def level(self):
        return None

    def _set(self, name: str, value) -> None:
        from rxtune.knob import KnobWriteError
        key = self._key[name]
        want = dict(self.state)
        want[key] = int(value)
        cmd = {k: v for k, v in {"rf": self.rf, "rfgain_sel": want["rfgain_sel"], "ifgr": want["ifgr"]}.items()
               if v is not None}
        t0 = time.time()
        try:
            os.unlink(self.ack_path)
        except OSError:
            pass
        tmp = self.cmd_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(cmd))
        os.replace(tmp, self.cmd_path)
        self.writes += 1
        ack = None
        while time.time() - t0 < self.ack_timeout:
            try:
                with open(self.ack_path, encoding="utf-8") as f:
                    ack = json.loads(f.read())
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        if ack is None:
            raise KnobWriteError(f"{name}: no retune.ack within {self.ack_timeout:.0f} s")
        if not ack.get("ok"):
            raise KnobWriteError(f"{name}: retune failed: {ack.get('error')}")
        time.sleep(0.3)
        bad = [ln for ln in self.scraper.tail[-12:] if "write failed" in ln]
        if bad and key == "rfgain_sel" and any("rfgain_sel" in ln for ln in bad):
            raise KnobWriteError(f"{name}: the chain logged: {bad[-1].strip()}")
        self.state[key] = int(value)


class Atsc1Chain:
    """One running chain: process, log pump, MER scraper, TS meter, retune frontend."""

    def __init__(self, cfg: config.Config, rf: int, antenna: str, out_dir: str,
                 start=(4, 40), settle_s: float = 6.0, window_s: float = 12.0,
                 log: Callable[[str], None] = print):
        config.rxtune_on_path(cfg)
        from rxtune.adapters import ATSC_MER, atsc_scraper
        self.cfg, self.rf, self.antenna, self.out_dir, self.start_gains, self.log = cfg, rf, antenna, out_dir, start, log
        os.makedirs(out_dir, exist_ok=True)
        self.ts_path = os.path.join(out_dir, "live.ts")
        for stale in ("retune.cmd", "retune.ack", "live.ts"):
            try:
                os.unlink(os.path.join(out_dir, stale))
            except OSError:
                pass
        self.scraper = atsc_scraper()
        self.scraper.spec = dataclasses.replace(ATSC_MER, settle_s=settle_s, window_s=window_s, min_samples=8)
        self.meter = TsMeter(self.ts_path)
        self.fe = RetuneFrontend(out_dir, rf, self.scraper)
        self.fe.state = {"rfgain_sel": start[0], "ifgr": start[1]}
        self.proc: Optional[subprocess.Popen] = None
        self._log_fh = None

    def start(self, startup_s: float = 150.0) -> bool:
        from rxtune.attach import popen_group
        env = self.cfg.chain_env()
        env.update(CHAIN_ENV)
        env.update({"STVT_ANTENNA": self.antenna, "STVT_RFGAIN_SEL": str(self.start_gains[0]),
                    "STVT_IFGR": str(self.start_gains[1]), "STVT_DABNOTCH": "0" if self.rf < 14 else "1"})
        self._log_fh = open(os.path.join(self.out_dir, "chain.log"), "ab", buffering=0)
        self.proc = popen_group([self.cfg.python, "-u", self.cfg.stvt_tv_live, "--rf", str(self.rf),
                                 "--out", self.ts_path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._pump, daemon=True).start()
        t0 = time.time()
        armed = seen = False
        while time.time() - t0 < startup_s:
            if self.proc.poll() is not None:
                self.log("the chain exited during start-up; see chain.log")
                return False
            armed = armed or any("PERSIST-RETUNE: watcher armed" in ln for ln in self.scraper.tail)
            seen = seen or self.scraper.read() is not None
            if armed and seen:
                break
            time.sleep(0.5)
        self.log(f"ATSC 1.0 chain up in {time.time() - t0:.0f} s on RF {self.rf} ({self.antenna}); "
                 f"retune {'armed' if armed else 'NOT armed'}, equaliser {'reporting' if seen else 'silent'}")
        return armed

    def _pump(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                self._log_fh.write(raw)
            except (OSError, ValueError):
                pass
            self.scraper.feed(raw.decode("utf-8", "replace"))

    def exited(self) -> Optional[str]:
        return "decoder exited" if self.proc is None or self.proc.poll() is not None else None

    def serve(self, duration_s: float, lock, on_event=None, *, autonomous: bool = True,
              comfort: float = 3.0, unlock_s: float = 10.0, reprobe_s: float = 30.0,
              coarse: int = 4, max_cells: int = 30, min_live: int = 10, start: Optional[dict] = None,
              until: Optional[Callable[[], bool]] = None):
        """Search the gains on decoder truth, then hold / probe / re-search for duration_s
        (or until `until()` says the viewer is done)."""
        from rxtune.measure import Measurer
        from rxtune.servo import ServoPolicy, serve
        policy = ServoPolicy(autonomous=autonomous, comfort=comfort, unlock_s=unlock_s, reprobe_s=reprobe_s)
        measurer = Measurer(self.scraper, self.fe, self.meter, min_live=min_live)
        return serve(self.fe, self.scraper, self.meter, duration_s=duration_s, policy=policy, lock=lock,
                     measurer=measurer, search=["gain:RFGR", "gain:IFGR"], coarse_points=coarse,
                     max_cells=max_cells, start=start, health=self.exited, on_event=on_event, until=until)

    def stop(self, grace_s: float = 15.0) -> bool:
        from rxtune.attach import graceful_stop
        closed = True
        if self.proc is not None:
            closed = graceful_stop(self.proc, grace_s=grace_s)
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
        return closed


def probe(cfg: config.Config, rf: int, antenna: str, out_dir: str, secs: float = 25.0,
          start=(4, 40), log: Callable[[str], None] = print) -> dict:
    """Is there a decodable ATSC 1.0 signal on this RF at the start gains?  One chain, `secs`
    of air, then MER percentiles and the picture count.  (The channel scan's lock test.)"""
    chain = Atsc1Chain(cfg, rf, antenna, out_dir, start=start, log=lambda m: None)
    t0 = time.time()
    try:
        if not chain.start(startup_s=90.0):
            return {"rf": rf, "standard": None, "error": "chain did not start"}
        vals = []
        t1 = time.time()
        while time.time() - t1 < secs:
            v = chain.scraper.read()
            if v is not None:
                vals.append(v)
            chain.meter.count()
            time.sleep(0.2)
        vals.sort()
        snap = chain.meter.snapshot()
        mer = vals[len(vals) // 2] if vals else None
        locked = mer is not None and snap["seq_headers"] >= 10
        return {"rf": rf, "standard": "atsc1" if locked else None, "mer_db": mer,
                "mer_p10": vals[len(vals) // 10] if vals else None, "lines": len(vals),
                "pictures": snap["seq_headers"], "packets": snap["packets"], "secs": round(time.time() - t0)}
    finally:
        chain.stop()
