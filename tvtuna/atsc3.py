# SPDX-License-Identifier: GPL-3.0-or-later
"""ATSC 3.0 through the atsc3 receiver.

The receiver's own orchestrator (atsc3_watch_av.py) runs the chain under its supervisor,
the audio and caption workers and the viewer, and the chain takes the site warden itself
(owner atsc3_watch, a human's priority).  tvTuna therefore does not hold the radio around it;
it checks the warden, picks the gains the last search proved, launches, and reads the chain's
own status (Frames, interval FEC, lane bytes) to tell a fade from a wedge in its status line.

Gains: the chain has no run-time gain control (a retune re-acquires), so the search runs as a
separate step on short captures (`tvtuna tune --rf N`, the gr-rxtune capture example) and its
verdict is remembered per channel and port."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from typing import Callable, Optional

from . import config, state


def _tool(cfg: config.Config, name: str) -> str:
    return os.path.join(cfg.atsc3_root, "tools", name)


def low_latency(cfg: config.Config) -> bool:
    """True when the receiver tree can serve a channel at its shortest delay.

    Two options make the difference between a first picture some 40 s after the pick and one
    some 13 s after it, and between a stream 25 s behind the air and one a slot behind: the audio
    worker's --follow (decode when the lane grows, not on a clock) and the orchestrator's
    --audio-extra that passes it on.  A receiver tree from before them exits on the unknown
    option, so they are asked for only when the tree's own tools name them."""
    try:
        with open(_tool(cfg, "atsc3_audio.py"), encoding="utf-8", errors="replace") as f:
            worker = f.read()
        with open(_tool(cfg, "atsc3_watch_av.py"), encoding="utf-8", errors="replace") as f:
            orchestrator = f.read()
    except OSError:
        return False
    return '"--follow"' in worker and '"--audio-extra"' in orchestrator


def supervisor_module(cfg: config.Config):
    """atsc3_run.py as a module: chain_status() and read_health() are the receiver's own readers."""
    spec = importlib.util.spec_from_file_location("atsc3_run_mod", _tool(cfg, "atsc3_run.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def health(mod, live_dir: str) -> dict:
    frames, fec = mod.chain_status(live_dir)
    h = mod.read_health(live_dir)
    out = {"frames": frames, "fec_pct": None if fec is None else round(fec, 1)}
    if h:
        out.update({"bytes": h[0], "updated": h[1], "media_s": h[2], "lanes": h[3]})
    return out


def verdict_for(cfg: config.Config, rf: int, antenna: str) -> Optional[dict]:
    """The remembered gains for this channel and port, from `tvtuna tune`."""
    ch = state.channel(rf)
    g = (ch or {}).get("atsc3_gains", {}).get(antenna)
    return g


def tune(cfg: config.Config, rf: int, antenna: str, out_dir: str, log: Callable[[str], None] = print,
         max_cells: int = 34) -> Optional[dict]:
    """Search the gains with the receiver as judge (BCH-clean share), remember the verdict."""
    ex = os.path.join(os.path.dirname(cfg.rxtune_src), "examples", "hw_atsc3_capture.py")
    os.makedirs(out_dir, exist_ok=True)
    env = cfg.chain_env()
    if cfg.radio_lock:
        env["RXTUNE_LOCK"] = cfg.radio_lock
        env.setdefault("RXTUNE_LOCK_WAIT", "600")
    cmd = [cfg.python, ex, "--atsc3", cfg.atsc3_root, "--rf", str(rf), "--antenna", antenna,
           "--python", cfg.python, "--max-cells", str(max_cells), "--out", out_dir,
           "--label", f"tvtuna tune {time.strftime('%Y-%m-%d %H:%M')}"]
    log(f"ATSC 3.0 gain search on RF {rf} ({antenna}): up to {max_cells} five-second captures ...")
    rc = subprocess.call(cmd, env=env, cwd=cfg.atsc3_root)
    best = None
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".json") and name != "measurements.jsonl":
            try:
                d = json.load(open(os.path.join(out_dir, name), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            v = d.get("verdict") or {}
            if v.get("best"):
                best = {"rfgain_sel": v["best"].get("gain:RFGR"), "ifgr": v["best"].get("gain:IFGR"),
                        "shape": v.get("shape"), "headline": v.get("headline"), "when": d.get("when")}
    if best:
        state.remember_atsc3_gains(rf, antenna, best)
        log(f"verdict {best['shape']}: {best['headline']} -> rfgain_sel {best['rfgain_sel']} ifgr {best['ifgr']} (remembered)")
    else:
        log(f"no verdict written (rc {rc}); see {out_dir}")
    return best


def watch(cfg: config.Config, rf: int, antenna: str, live_dir: str, *, cc: bool = True,
          chain_extra: str = "", log: Callable[[str], None] = print,
          status_every: float = 10.0, stop: Optional[Callable[[], bool]] = None) -> int:
    """Run the receiver's orchestrator and narrate its health until it ends or `stop()`."""
    os.makedirs(live_dir, exist_ok=True)
    gains = verdict_for(cfg, rf, antenna)
    extra = chain_extra
    if gains and gains.get("rfgain_sel") is not None:
        extra = (f"--rfgain {gains['rfgain_sel']} --ifgr {gains['ifgr']} " + extra).strip()
        log(f"gains from the remembered verdict ({gains.get('shape')}, {str(gains.get('headline'))[:40]})")
    else:
        log("no remembered gains for this channel/port: the receiver's own carrier table decides "
            "(run `tvtuna tune` once for a measured answer)")
    cmd = [cfg.python, _tool(cfg, "atsc3_watch_av.py"), "--rf", str(rf), "--ant", antenna,
           "--live-dir", live_dir, "--python", cfg.python]
    if cc:
        cmd.append("--cc")
    if extra:
        cmd += ["--chain-extra", extra]
    mod = supervisor_module(cfg)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, cwd=cfg.atsc3_root, env=cfg.chain_env(), creationflags=flags)
    log(f"ATSC 3.0 up on RF {rf} ({antenna}); orchestrator pid {proc.pid}")
    last = 0.0
    try:
        while proc.poll() is None:
            if stop and stop():
                break
            if time.time() - last >= status_every:
                h = health(mod, live_dir)
                verdict = "starting"
                if h["frames"] is not None:
                    fec = h["fec_pct"]
                    verdict = ("fade: frames advance, nothing decodes" if fec is not None and fec < 20
                               else f"decoding {fec:.0f} %" if fec is not None else "frames advance")
                lanes = h.get("lanes") or {}
                log(f"  frames {h['frames']}  {verdict}  lanes {len(lanes)}  media {h.get('media_s', 0):.0f} s")
                last = time.time()
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    import signal
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                proc.wait(timeout=30)
            except Exception:                           # noqa: BLE001
                pass
    return proc.returncode if proc.returncode is not None else 0


def probe(cfg: config.Config, rf: int, antenna: str, secs: float = 20.0, log: Callable[[str], None] = print) -> dict:
    """Is there an ATSC 3.0 carrier here?  The chain for `secs` with no player, its JSON report."""
    rep = os.path.join(config.STATE_DIR, "probe", f"rf{rf}.json")
    os.makedirs(os.path.dirname(rep), exist_ok=True)
    env = cfg.chain_env()
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    cmd = [cfg.python, "-m", "atsc3", "watch", "--rf", str(rf), "--ant", antenna, "--player", "none",
           "--accel", "cpu", "--secs", str(secs), "--wait", "120", "--json", rep]
    t0 = time.time()
    subprocess.call(cmd, cwd=cfg.atsc3_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        d = json.load(open(rep, encoding="utf-8"))
    except (OSError, ValueError):
        return {"rf": rf, "standard": None, "error": "no report", "secs": round(time.time() - t0)}
    frames, tot, conv = d.get("frames", 0), d.get("fec_total", 0), d.get("fec_converged", 0)
    return {"rf": rf, "standard": "atsc3" if frames and tot else None, "frames": frames,
            "fec_pct": round(100.0 * conv / tot, 1) if tot else None, "secs": round(time.time() - t0)}
