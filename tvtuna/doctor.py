# SPDX-License-Identifier: GPL-3.0-or-later
"""`tvtuna doctor`: is every piece where the configuration says, and is it the version tvTuna
needs?  One line per check, PASS / FAIL / SKIP, and what to do about a FAIL.  Nothing here opens
the radio unless --radio is given (listing devices can disturb a receiver that is streaming)."""
import os
import shutil
import subprocess
from typing import List, Tuple

from . import config, state

Check = Tuple[str, str, str]      # verdict, name, detail


def _run(py: str, code: str, env=None, timeout: float = 60.0) -> Tuple[int, str]:
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout, env=env)
        text = p.stdout.strip() or p.stderr.strip()
        return p.returncode, text.splitlines()[-1] if text else ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _has(path: str, needle: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return needle in f.read()
    except OSError:
        return False


def checks(cfg: config.Config, with_radio: bool = False) -> List[Check]:
    out: List[Check] = []
    env = cfg.chain_env()

    def add(ok, name, detail, fix=""):
        out.append(("PASS" if ok else "FAIL", name, detail + (f"  -> {fix}" if fix and not ok else "")))

    add(os.path.isfile(config.CONFIG_PATH), "configuration", config.CONFIG_PATH, "run `tvtuna init ...`")

    py_ok = bool(cfg.python) and os.path.isfile(cfg.python)
    add(py_ok, "receiver interpreter", cfg.python or "(unset)", "tvtuna init --python <python with SoapySDR + GNU Radio>")
    if py_ok:
        rc, line = _run(cfg.python, "import SoapySDR; print('SoapySDR', SoapySDR.getAPIVersion())", env)
        add(rc == 0, "SoapySDR in that interpreter", line, "install SoapySDR's Python binding there (radioconda has it)")
        rc, line = _run(cfg.python, "from gnuradio import gr; print('GNU Radio', gr.version())", env)
        add(rc == 0, "GNU Radio in that interpreter", line, "ATSC 1.0 needs GNU Radio 3.10 (radioconda has it)")
        rc, line = _run(cfg.python, "import numpy; print('numpy', numpy.__version__)", env)
        add(rc == 0, "numpy in that interpreter", line, "pip install numpy")

    tv = cfg.stvt_tv_live
    add(bool(tv) and os.path.isfile(tv), "ATSC 1.0 receiver (Software TV Tuner tv_live.py)", tv or "(unset)",
        "clone github.com/Felbs/Software-TV-Tuner; tvtuna init --stvt-tv-live <clone>/tools/tv_live.py")
    if tv and os.path.isfile(tv):
        add(_has(tv, "STVT_PERSIST_RETUNE"), "tv_live.py takes gain changes live (STVT_PERSIST_RETUNE)", tv,
            "this tv_live.py is too old: pull the Software TV Tuner")
        add(_has(tv, "gain-only: same channel"), "tv_live.py keeps the stream through a gain step (2026-11-09 fix)", tv,
            "without it the servo's gain steps can stall the picture: pull the Software TV Tuner "
            "(if its main branch does not have the fix yet: git checkout pi-port-radiopi2)")

    rx = cfg.rxtune_src
    add(bool(rx) and os.path.isdir(os.path.join(rx, "rxtune")), "gr-rxtune src/", rx or "(unset)",
        "clone github.com/Felbs/gr-rxtune; tvtuna init --rxtune-src <clone>/src")
    if rx and os.path.isdir(os.path.join(rx, "rxtune")):
        code = f"import sys; sys.path.insert(0, r'{rx}'); import rxtune.servo as s; print('rxtune servo.serve', hasattr(s, 'serve'))"
        rc, line = _run(cfg.python if py_ok else "python", code, env)
        add(rc == 0 and line.endswith("True"), "gr-rxtune has the servo", line, "pull gr-rxtune (the servo is from 2026-11)")
        ex = os.path.join(os.path.dirname(rx), "examples", "hw_atsc3_capture.py")
        add(os.path.isfile(ex), "gr-rxtune ATSC 3.0 gain search example", ex, "pull gr-rxtune")

    a3 = cfg.atsc3_root
    if not a3:
        out.append(("SKIP", "ATSC 3.0 receiver", "atsc3_root unset: ATSC 1.0 only"))
    else:
        for name in ("atsc3_watch_av.py", "atsc3_run.py"):
            p = os.path.join(a3, "tools", name)
            add(os.path.isfile(p), f"ATSC 3.0 receiver tools/{name}", p,
                "clone github.com/Felbs/atsc3; tvtuna init --atsc3-root <clone>")
        w = os.path.join(a3, "tools", "atsc3_watch_av.py")
        if os.path.isfile(w):
            add(_has(w, "--tv-extra"), "atsc3_watch_av.py can write a transport stream (--tv-extra)", w, "pull the atsc3 receiver")

    for tool, why in (("ffmpeg", "ATSC 3.0 muxing to MPEG-TS"), ("ffplay", "the built-in player (not needed behind OnAir)")):
        found = shutil.which(tool, path=env.get("PATH"))
        if found or tool == "ffmpeg":
            add(bool(found), f"{tool} on PATH", found or f"not found; needed for {why}", f"install {tool}")
        else:
            out.append(("SKIP", f"{tool} on PATH", f"not found; only needed for {why}"))

    if cfg.radio_lock:
        add(os.path.isfile(cfg.radio_lock), "radio warden", cfg.radio_lock, "fix --radio-lock, or init with --radio-lock \"\"")
    else:
        out.append(("SKIP", "radio warden", "none configured: tvTuna assumes it is the radio's only user"))

    n = len(state.load().get("channels", {}))
    out.append(("PASS" if n else "SKIP", "channel table", f"{n} channel(s) known" + ("" if n else ": run `tvtuna scan`")))

    if with_radio and py_ok:
        rc, line = _run(cfg.python, "import SoapySDR; d = SoapySDR.Device.enumerate(); "
                                    "print(len(d), 'device(s):', '; '.join(str(dict(x).get('label', dict(x).get('driver'))) for x in d))", env, 90.0)
        add(rc == 0 and not line.startswith("0 "), "an SDR is attached", line, "plug the radio in; check its SoapySDR driver module")
    else:
        out.append(("SKIP", "an SDR is attached", "not looked for (use `tvtuna doctor --radio` when nothing is streaming)"))
    return out


def run(cfg: config.Config, with_radio: bool = False) -> int:
    rows = checks(cfg, with_radio)
    for verdict, name, detail in rows:
        print(f"{verdict:4s}  {name}: {detail}")
    fails = sum(1 for r in rows if r[0] == "FAIL")
    print(f"doctor: {fails} failure(s), {sum(1 for r in rows if r[0] == 'PASS')} passed, {sum(1 for r in rows if r[0] == 'SKIP')} skipped")
    return 1 if fails else 0
