# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the receivers live.  Nothing here is site-specific: every path comes from
~/.tvtuna/config.json (written by `tvtuna init`) or a TVTUNA_* environment variable."""
import dataclasses
import json
import os
import sys
from typing import Optional

STATE_DIR = os.path.expanduser(os.environ.get("TVTUNA_STATE", "~/.tvtuna"))
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

KEYS = {
    # key: (env var, what it is)
    "python": ("TVTUNA_PYTHON", "interpreter that can import SoapySDR and GNU Radio (the receivers run in it)"),
    "stvt_tv_live": ("TVTUNA_STVT", "Software-TV-Tuner tools/tv_live.py (ATSC 1.0 chain)"),
    "atsc3_root": ("TVTUNA_ATSC3", "root of the atsc3 receiver tree (ATSC 3.0 chain, viewer, supervisor)"),
    "rxtune_src": ("TVTUNA_RXTUNE", "gr-rxtune's src/ directory (search + servo)"),
    "radio_lock": ("TVTUNA_RADIO_LOCK", "the site's radio warden module (radio_lock.py); empty = no warden"),
    "antenna_atsc1": ("TVTUNA_ANT1", "SDR antenna port for ATSC 1.0"),
    "antenna_atsc3": ("TVTUNA_ANT3", "SDR antenna port for ATSC 3.0"),
    "player": ("TVTUNA_PLAYER", "ffplay | mpv | none"),
    "sdrplay_api": ("TVTUNA_SDRPLAY_API", "directory of the SDRplay API dll, added to PATH for the chains"),
}


@dataclasses.dataclass
class Config:
    python: str = sys.executable
    stvt_tv_live: str = ""
    atsc3_root: str = ""
    rxtune_src: str = ""
    radio_lock: str = ""
    antenna_atsc1: str = "Antenna A"
    antenna_atsc3: str = "Antenna B"
    player: str = "ffplay"
    sdrplay_api: str = ""

    def missing(self, *keys) -> list:
        """Which of `keys` are unset or point nowhere."""
        out = []
        for k in keys:
            v = getattr(self, k)
            if not v or (k in ("stvt_tv_live", "atsc3_root", "rxtune_src", "radio_lock") and not os.path.exists(v)):
                out.append(k)
        return out

    def chain_env(self) -> dict:
        """Environment for a receiver process: PYTHONPATH dropped (a foreign SoapySDR binding on it
        breaks the interpreter's own), the SDRplay API on PATH when configured."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        if self.sdrplay_api:
            env["PATH"] = self.sdrplay_api + os.pathsep + env.get("PATH", "")
        if self.radio_lock:
            # the atsc3 chain takes the warden itself, if it can import the module from here
            env["ATSC3_SIBLING_TOOLS"] = os.path.dirname(os.path.abspath(self.radio_lock))
            env.setdefault("RXTUNE_LOCK", self.radio_lock)
        return env


def load() -> Config:
    cfg = Config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k in KEYS:
                    setattr(cfg, k, v)
    except (OSError, ValueError):
        pass
    for k, (env, _) in KEYS.items():
        if os.environ.get(env):
            setattr(cfg, k, os.environ[env])
    return cfg


def save(cfg: Config) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=1)
    return CONFIG_PATH


def rxtune_on_path(cfg: Config) -> None:
    if cfg.rxtune_src and cfg.rxtune_src not in sys.path:
        sys.path.insert(0, cfg.rxtune_src)


def explain_missing(keys) -> str:
    lines = ["tvTuna is not configured for this machine. Set these (tvtuna init, or the env var):"]
    for k in keys:
        env, what = KEYS[k]
        lines.append(f"  {k:14s} {env:22s} {what}")
    return "\n".join(lines)
