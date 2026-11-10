# SPDX-License-Identifier: GPL-3.0-or-later
"""The channel scan: which RF channels carry a decodable signal, and in which standard.

Two passes.  First every channel gets the ATSC 1.0 lock test (one chain each, the equaliser's
MER and the picture count decide) with tvTuna holding the radio.  Then the channels that did
not lock get the ATSC 3.0 probe (the receiver for 20 s, its own report: Frames and FEC blocks),
with the radio released because that chain takes the warden itself.  A channel that locks in
neither is listed as empty, with what was seen.

The ATSC 1.0 pass may be given several antenna ports ("Antenna A,Antenna B").  Each channel is
then tested on each and filed under the port with the higher MER.  Measured on air 2026-11-10,
interleaved 30 s tests: one channel was 1.3 dB better on the first port, its neighbour 0.4 to
0.9 dB better on the second, and that neighbour sits about 2 dB above the point where the
picture breaks up; one port for every channel gives part of that away."""
import os
import time
from typing import Callable, Iterable, List

from . import atsc1, atsc3, config, radio, state

UHF_DEFAULT = list(range(14, 37))
RETRY_WAIT_S = 8.0


def _lock_test(cfg, rf: int, antenna: str, out_dir: str, secs: float, lk) -> dict:
    """One ATSC 1.0 lock test.  A chain that dies before it has the radio gets a second go after
    a pause (seen on air 2026-11-10: a test started straight after another ended in one second,
    its log stopping at the device search; the next one, seconds later, ran)."""
    r = atsc1.probe(cfg, rf, antenna, out_dir, secs=secs, beat=lk.heartbeat)
    if r.get("error") == "chain did not start":
        t = time.time()
        while time.time() - t < RETRY_WAIT_S:
            lk.heartbeat()
            time.sleep(0.5)
        r = atsc1.probe(cfg, rf, antenna, out_dir, secs=secs, beat=lk.heartbeat)
    return r


def _port_note(r: dict) -> str:
    if r.get("standard") == "atsc1":
        return f"{r['mer_db']:.1f} dB"
    return "no lock"


def run(cfg: config.Config, rfs: Iterable[int], antenna1: str, antenna3: str,
        log: Callable[[str], None] = print, atsc1_secs: float = 25.0, atsc3_secs: float = 20.0,
        skip_atsc3: bool = False) -> List[dict]:
    rfs = list(rfs)
    rows = {}
    probe_dir = os.path.join(config.STATE_DIR, "probe")
    ants = config.ports(antenna1)
    log(f"scan: {len(rfs)} channels; ATSC 1.0 lock test {atsc1_secs:.0f} s each on {' and '.join(ants)}, "
        f"then ATSC 3.0 {atsc3_secs:.0f} s each on {antenna3}")
    t0 = time.time()
    stopped = None
    with radio.site_lock(cfg) as lk:
        for rf in rfs:
            lk.heartbeat()
            stopped = lk.should_yield()
            if stopped:
                log(f"scan stopped before RF {rf}: {stopped}")
                break
            tried = {}
            for ant in ants:
                if tried:
                    lk.heartbeat()
                    stopped = lk.should_yield()
                    if stopped:
                        break
                tried[ant] = _lock_test(cfg, rf, ant, os.path.join(probe_dir, f"rf{rf}"), atsc1_secs, lk)
            # The port with the higher MER among those that locked; the first named wins a tie.
            locked = [a for a in tried if tried[a].get("standard") == "atsc1"]
            best = max(locked, key=lambda a: tried[a]["mer_db"]) if locked else ants[0]
            r = tried[best]
            rows[rf] = {"rf": rf, "standard": None, **r}
            if locked:
                rows[rf]["antenna"] = best
            if len(ants) > 1:
                rows[rf]["ports"] = {a: {k: v for k, v in tried[a].items()
                                         if k in ("standard", "mer_db", "mer_p10", "pictures", "error")} for a in tried}
            if r.get("standard") == "atsc1":
                others = ", ".join(f"{a} {_port_note(tried[a])}" for a in tried if a != best)
                log(f"  RF {rf:2d}  ATSC 1.0  MER {r['mer_db']:.1f} dB  {r['pictures']} pictures"
                    + (f"  on {best}" + (f" ({others})" if others else "") if len(ants) > 1 else ""))
            else:
                mer = r.get("mer_db")
                log(f"  RF {rf:2d}  no ATSC 1.0 lock" + (f" (MER {mer:.1f} dB, {r.get('pictures', 0)} pictures)" if mer
                                                    else f" ({r['error']})" if r.get("error") else ""))
            if stopped:
                log(f"scan stopped during RF {rf}: {stopped}")
                break
    # A scan that was cut short keeps what it tested and stops: a channel that never had its
    # ATSC 1.0 test must not be filed as "no ATSC 3.0 carrier", and whoever needed the radio gets it.
    if not skip_atsc3 and not stopped:
        for rf in rfs:
            if rows[rf].get("standard"):
                continue
            r3 = atsc3.probe(cfg, rf, antenna3, secs=atsc3_secs)
            if r3.get("standard") == "atsc3":
                rows[rf] = {**{k: v for k, v in rows[rf].items() if k != "error"}, **r3, "standard": "atsc3"}
                log(f"  RF {rf:2d}  ATSC 3.0  {r3['frames']} frames  FEC {r3['fec_pct']} %")
            else:
                rows[rf] = {**rows[rf], "atsc3": r3}
                log(f"  RF {rf:2d}  no ATSC 3.0 carrier")
    table = [rows[rf] for rf in rfs if rf in rows]
    state.remember_scan(table, antenna1, antenna3)
    locked = [r for r in table if r.get("standard")]
    log(f"scan {'cut short' if stopped else 'done'} in {(time.time() - t0) / 60:.0f} min: {len(locked)} of {len(table)} tested channels "
        f"carry TV" + (f"; {len(rfs) - len(table)} not tested" if stopped else ""))
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
