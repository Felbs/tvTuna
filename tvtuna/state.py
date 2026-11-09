# SPDX-License-Identifier: GPL-3.0-or-later
"""What tvTuna remembers between runs: the channel table (what locked, which standard, how
strong) and the gains a search proved per channel and antenna port.  One JSON file in the
state directory; every write is whole-file and atomic."""
import json
import os
import time
from typing import Optional

from . import config

PATH = os.path.join(config.STATE_DIR, "channels.json")


def load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"channels": {}, "scans": []}


def save(d: dict) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def channel(rf: int) -> Optional[dict]:
    return load()["channels"].get(str(rf))


def remember_scan(rows: list, antenna1: str, antenna3: str) -> None:
    d = load()
    for r in rows:
        ch = d["channels"].setdefault(str(r["rf"]), {})
        ch["last_scan"] = {**r, "when": time.time()}
        if r.get("standard"):
            ch["standard"] = r["standard"]
            ch["antenna"] = antenna1 if r["standard"] == "atsc1" else antenna3
    d["scans"].append({"when": time.time(), "rfs": [r["rf"] for r in rows],
                       "locked": [r["rf"] for r in rows if r.get("standard")]})
    d["scans"] = d["scans"][-20:]
    save(d)


def remember_atsc3_gains(rf: int, antenna: str, verdict: dict) -> None:
    d = load()
    ch = d["channels"].setdefault(str(rf), {})
    ch.setdefault("atsc3_gains", {})[antenna] = verdict
    save(d)


def remember_atsc1_hold(rf: int, antenna: str, held: dict, mer_db, proven: bool) -> None:
    d = load()
    ch = d["channels"].setdefault(str(rf), {})
    ch.setdefault("atsc1_hold", {})[antenna] = {"held": held, "mer_db": mer_db, "proven": proven, "when": time.time()}
    save(d)


def atsc1_start(rf: int, antenna: str, default=(4, 40)) -> tuple:
    """Where the ATSC 1.0 chain starts: the last proven hold on this channel and port, else the default."""
    ch = channel(rf) or {}
    h = ch.get("atsc1_hold", {}).get(antenna)
    if h and h.get("proven") and h.get("held"):
        held = h["held"]
        try:
            return (int(held["gain:RFGR"]), int(held["gain:IFGR"]))
        except (KeyError, TypeError, ValueError):
            pass
    return default
