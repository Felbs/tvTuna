# SPDX-License-Identifier: GPL-3.0-or-later
"""The programme guide as XMLTV, from what the receivers already decode.

ATSC 1.0: the Software TV Tuner's PSIP parser reads the virtual channel table (major.minor and
the short name) and the event tables (now / next and beyond) out of the transport stream the
chain writes; this turns them into XMLTV channels and programmes.  The channel id is the one
the playlist uses (rf<N>), so OnAir's IPTV screen matches them by tvg-id; the virtual channels
of a multiplex are listed as display names of that one entry, and the programmes shown are the
first virtual channel's (the player plays the first programme of the stream).

ATSC 3.0: nothing yet (the service list and ESG parsers are the next job); the channel entry
carries the language of its audio lanes from live.json."""
import datetime
import glob
import importlib.util
import json
import os
import sys
from typing import Callable, Optional
from xml.sax.saxutils import escape

from . import config, state

_PSIP = None


def _psip(cfg: config.Config):
    """atsc_psip from the STVT tree, loaded once (it lives next to tv_live.py)."""
    global _PSIP
    if _PSIP is not None:
        return _PSIP
    if not cfg.stvt_tv_live:
        return None
    path = os.path.join(os.path.dirname(cfg.stvt_tv_live), "atsc_psip.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("tvtuna_atsc_psip", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tvtuna_atsc_psip"] = mod
    spec.loader.exec_module(mod)
    _PSIP = mod
    return mod


def _xmltv_time(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def atsc1_guide(cfg: config.Config, rf: int, live_ts: str, max_bytes: int = 60_000_000) -> dict:
    """{'channels': [...], 'events': {source_id: [...]}} from the newest bytes of a live.ts."""
    psip = _psip(cfg)
    if psip is None or not os.path.exists(live_ts):
        return {"channels": [], "events": {}}
    try:
        return psip.extract_psip(live_ts, max_bytes=max_bytes)
    except Exception:                                    # noqa: BLE001  (best effort: a guide is decoration)
        return {"channels": [], "events": {}}


def live_ts_for(rf: int) -> Optional[str]:
    p = os.path.join(config.STATE_DIR, "live", f"rf{rf}", "live.ts")
    return p if os.path.exists(p) else None


def atsc3_langs(rf: int) -> list:
    p = os.path.join(config.STATE_DIR, "live", f"rf{rf}", "live.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        return sorted({v.get("lang") for v in (d.get("lanes") or {}).values() if v.get("kind") == "audio" and v.get("lang")})
    except (OSError, ValueError):
        return []


def xmltv(cfg: config.Config, log: Callable[[str], None] = lambda m: None) -> str:
    """The whole table as XMLTV.  Channels with a stream file get names and programmes."""
    psip = _psip(cfg)
    d = state.load()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="tvtuna">']
    progs = []
    for rf in sorted(d.get("channels", {}), key=int):
        ch = d["channels"][rf]
        std = ch.get("standard") or ("atsc1" if ch.get("atsc1_hold") else "atsc3" if ch.get("atsc3_gains") else None)
        if std is None:
            continue
        names = [f"RF {rf}"]
        if std == "atsc1":
            ts = live_ts_for(int(rf))
            g = atsc1_guide(cfg, int(rf), ts) if ts else {"channels": [], "events": {}}
            vcs = sorted(g["channels"], key=lambda c: (c.get("major", 0), c.get("minor", 0)))
            for c in vcs:
                names.append(f"{c.get('major')}.{c.get('minor')} {c.get('short_name', '').strip()}".strip())
            if vcs and psip is not None:
                evs = g["events"].get(vcs[0].get("source_id"), [])
                for ev in sorted(evs, key=lambda e: e["start_gps"]):
                    try:
                        start = psip.gps_to_datetime(ev["start_gps"])
                    except Exception:                    # noqa: BLE001
                        continue
                    stop = start + datetime.timedelta(seconds=ev.get("length_sec", 0))
                    progs.append((rf, start, stop, ev.get("title", ""), ev.get("rating", "")))
        else:
            langs = atsc3_langs(int(rf))
            if langs:
                names.append("ATSC 3.0 audio: " + ", ".join(langs))
        out.append(f'  <channel id="rf{rf}">')
        for n in names:
            out.append(f"    <display-name>{escape(n)}</display-name>")
        out.append("  </channel>")
    for rf, start, stop, title, rating in progs:
        out.append(f'  <programme start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}" channel="rf{rf}">')
        out.append(f"    <title>{escape(title)}</title>")
        if rating:
            out.append(f'    <rating system="VCHIP"><value>{escape(rating)}</value></rating>')
        out.append("  </programme>")
    out.append("</tv>")
    log(f"guide: {len(progs)} programmes")
    return "\n".join(out) + "\n"
