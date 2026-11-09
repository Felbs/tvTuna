# SPDX-License-Identifier: GPL-3.0-or-later
"""The channel scan: which RF channels carry a decodable signal, and in which standard.

Two passes.  First every channel gets the ATSC 1.0 lock test (one chain each, the equaliser's
MER and the picture count decide) with tvTuna holding the radio.  Then the channels that did
not lock get the ATSC 3.0 probe (the receiver for 20 s, its own report: Frames and FEC blocks),
with the radio released because that chain takes the warden itself.  A channel that locks in
neither is listed as empty, with what was seen."""
import os
import time
from typing import Callable, Iterable, List

from . import atsc1, atsc3, config, radio, state

UHF_DEFAULT = list(range(14, 37))


def run(cfg: config.Config, rfs: Iterable[int], antenna1: str, antenna3: str,
        log: Callable[[str], None] = print, atsc1_secs: float = 25.0, atsc3_secs: float = 20.0,
        skip_atsc3: bool = False) -> List[dict]:
    rfs = list(rfs)
    rows = {}
    probe_dir = os.path.join(config.STATE_DIR, "probe")
    log(f"scan: {len(rfs)} channels; ATSC 1.0 lock test {atsc1_secs:.0f} s each on {antenna1}, "
        f"then ATSC 3.0 {atsc3_secs:.0f} s each on {antenna3}")
    t0 = time.time()
    with radio.site_lock(cfg) as lk:
        for rf in rfs:
            reason = lk.should_yield()
            if reason:
                log(f"scan stopped: {reason}")
                break
            r = atsc1.probe(cfg, rf, antenna1, os.path.join(probe_dir, f"rf{rf}"), secs=atsc1_secs)
            rows[rf] = r
            if r.get("standard") == "atsc1":
                log(f"  RF {rf:2d}  ATSC 1.0  MER {r['mer_db']:.1f} dB  {r['pictures']} pictures")
            else:
                mer = r.get("mer_db")
                log(f"  RF {rf:2d}  no ATSC 1.0 lock" + (f" (MER {mer:.1f} dB, {r.get('pictures', 0)} pictures)" if mer else ""))
    if not skip_atsc3:
        for rf in rfs:
            if rows.get(rf, {}).get("standard"):
                continue
            r3 = atsc3.probe(cfg, rf, antenna3, secs=atsc3_secs)
            if r3.get("standard") == "atsc3":
                rows[rf] = {**rows.get(rf, {}), **r3, "standard": "atsc3"}
                log(f"  RF {rf:2d}  ATSC 3.0  {r3['frames']} frames  FEC {r3['fec_pct']} %")
            else:
                rows[rf] = {**rows.get(rf, {}), "atsc3": r3}
                log(f"  RF {rf:2d}  no ATSC 3.0 carrier")
    table = [rows[rf] for rf in rfs if rf in rows]
    state.remember_scan(table, antenna1, antenna3)
    locked = [r for r in table if r.get("standard")]
    log(f"scan done in {(time.time() - t0) / 60:.0f} min: {len(locked)} of {len(table)} channels carry TV")
    return table


def table_text(d: dict) -> str:
    chans = d.get("channels", {})
    if not chans:
        return "no scan yet (tvtuna scan)"
    lines = ["RF   standard  antenna     last seen"]
    for rf in sorted(chans, key=int):
        ch = chans[rf]
        std = ch.get("standard") or "-"
        ls = ch.get("last_scan", {})
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ls.get("when", 0))) if ls else ""
        q = ""
        if std == "atsc1" and ls.get("mer_db") is not None:
            q = f"MER {ls['mer_db']:.1f} dB"
        elif std == "atsc3" and ls.get("fec_pct") is not None:
            q = f"FEC {ls['fec_pct']} %"
        lines.append(f"{rf:>3}  {std:8s}  {ch.get('antenna', '-'):10s}  {when}  {q}")
    return "\n".join(lines)
