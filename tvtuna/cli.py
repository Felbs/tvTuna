# SPDX-License-Identifier: GPL-3.0-or-later
"""tvtuna: init | scan | watch | tune | status | report"""
import argparse
import json
import os
import sys
import time
import zipfile

from . import __version__, atsc1, atsc3, config, player, radio, scan, state


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cmd_init(a, cfg):
    for k, (env, what) in config.KEYS.items():
        v = getattr(a, k, None)
        if v is not None:
            setattr(cfg, k, v)
    path = config.save(cfg)
    print(f"wrote {path}")
    missing = cfg.missing("stvt_tv_live", "atsc3_root", "rxtune_src")
    if missing:
        print(config.explain_missing(missing))
    return 0


def cmd_engine(a, cfg):
    say = lambda m: print(f"# {m}", file=sys.stderr, flush=True)
    if a.replay:
        from . import replay
        return replay.run(a.replay, a.host, a.tcp, say, a.mbps)
    from . import engine
    return engine.run(cfg, a.host, a.tcp, say)


def cmd_status(a, cfg):
    print(f"tvtuna {__version__}")
    print(radio.describe(radio.holder(cfg)))
    print(scan.table_text(state.load()))
    return 0


def cmd_scan(a, cfg):
    missing = cfg.missing("stvt_tv_live", "rxtune_src") + ([] if a.skip_atsc3 else cfg.missing("atsc3_root"))
    if missing:
        print(config.explain_missing(missing))
        return 2
    rfs = a.rf or scan.UHF_DEFAULT
    table = scan.run(cfg, rfs, a.antenna1 or cfg.antenna_atsc1, a.antenna3 or cfg.antenna_atsc3, log=log,
                     atsc1_secs=a.secs, skip_atsc3=a.skip_atsc3)
    print(scan.table_text(state.load()))
    return 0 if len(table) == len(rfs) else 4          # 4: cut short (the radio was needed elsewhere)


def cmd_tune(a, cfg):
    missing = cfg.missing("atsc3_root", "rxtune_src")
    if missing:
        print(config.explain_missing(missing))
        return 2
    ant = a.antenna or cfg.antenna_atsc3
    out = os.path.join(config.STATE_DIR, "tune", f"rf{a.rf}_{ant.replace(' ', '_')}")
    best = atsc3.tune(cfg, a.rf, ant, out, log=log)
    return 0 if best else 1


def _standard_for(a, cfg):
    if a.standard in ("1", "3"):
        return "atsc1" if a.standard == "1" else "atsc3"
    ch = state.channel(a.rf) or {}
    if ch.get("standard"):
        return ch["standard"]
    return None


def cmd_watch(a, cfg):
    std = _standard_for(a, cfg)
    if std is None:
        print(f"RF {a.rf}: not in the channel table; say --standard 1 or 3, or run `tvtuna scan --rf {a.rf}`")
        return 2
    kind = a.player or cfg.player
    live_dir = os.path.join(config.STATE_DIR, "live", f"rf{a.rf}")
    if std == "atsc3":
        missing = cfg.missing("atsc3_root")
        if missing:
            print(config.explain_missing(missing))
            return 2
        ant = a.antenna or cfg.antenna_atsc3
        st = radio.holder(cfg)
        if st and st.get("owner") not in (None, radio.OWNER, "atsc3_watch"):
            log(radio.describe(st) + "; the receiver will wait for it")
        return atsc3.watch(cfg, a.rf, ant, live_dir, cc=not a.no_cc, chain_extra=a.chain_extra, log=log)
    # ATSC 1.0: tvTuna owns the radio, runs the chain once, searches, then holds; restarts on death.
    missing = cfg.missing("stvt_tv_live", "rxtune_src")
    if missing:
        print(config.explain_missing(missing))
        return 2
    config.rxtune_on_path(cfg)
    from rxtune.lock import Yielded
    from rxtune.loop import DecoderDied
    ant = a.antenna or cfg.antenna_atsc1
    start = state.atsc1_start(a.rf, ant)
    restarts = 0
    pl = None
    t_end = time.time() + a.duration if a.duration else None
    try:
        with radio.site_lock(cfg) as lk:
            while True:
                chain = atsc1.Atsc1Chain(cfg, a.rf, ant, live_dir, start=start, log=log)
                try:
                    if not chain.start(beat=lk.heartbeat):
                        log("chain did not come up; see " + os.path.join(live_dir, "chain.log"))
                        return 3
                    if pl is None and kind != "none":
                        pl = player.TsPlayer(chain.ts_path, kind, f"tvtuna RF {a.rf}", log=log)
                        if not pl.start():
                            log(f"player {kind} not started; watching without a window")
                            pl = None

                    def on_event(st_, setting, reading, _chain=chain):
                        snap = _chain.meter.snapshot()
                        mer = None if reading.value is None else round(reading.value, 1)
                        log(f"  [{st_:9}] {setting}  MER {mer}  video {'yes' if reading.alive else 'NO'} "
                            f"({reading.live_rate:.1f} pic/s)  pkts {snap['packets']}  cc_err {snap['cc_errors']}")

                    def window_closed(_pl=pl):
                        return _pl is not None and _pl.exited_by_user

                    remaining = (t_end - time.time()) if t_end else 10 * 365 * 86400.0
                    rep = chain.serve(remaining, lk, on_event=on_event, autonomous=not a.watch_only,
                                      start={"gain:RFGR": [start[0]], "gain:IFGR": [start[1]]} if a.resume else None,
                                      until=window_closed)
                    if rep.held:
                        dial = rep.verdicts[-1].dial if rep.verdicts else None
                        state.remember_atsc1_hold(a.rf, ant, rep.held, dial, rep.proven)
                    print("\n".join(rep.log))
                    print(rep)
                    if pl is not None and pl.exited_by_user:
                        log("player window closed")
                    return 0 if rep.proven else 2
                except DecoderDied as e:
                    restarts += 1
                    log(f"chain died ({e}); restart {restarts} of {a.max_restarts} after {a.cooldown:.0f} s")
                    if restarts > a.max_restarts:
                        return 3
                    time.sleep(a.cooldown)
                    continue
                except Yielded as y:
                    log(f"gave up the radio: {y}")
                    return 4
                except KeyboardInterrupt:
                    log("stopping")
                    return 0
                finally:
                    if not chain.stop():
                        log("NOTE: the chain did not close the radio by itself")
    finally:
        if pl is not None:
            pl.stop()


def cmd_report(a, cfg):
    out = a.out or os.path.join(config.STATE_DIR, f"tvtuna_report_{time.strftime('%Y%m%d_%H%M%S')}.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(config.STATE_DIR):
            for f in files:
                if f.endswith((".log", ".json", ".jsonl", ".txt")) and not f.startswith("tvtuna_report"):
                    p = os.path.join(root, f)
                    if os.path.getsize(p) < 50_000_000:
                        z.write(p, os.path.relpath(p, config.STATE_DIR))
                        n += 1
        z.writestr("about.txt", f"tvtuna {__version__}\n{time.ctime()}\n{sys.platform}\n{radio.describe(radio.holder(cfg))}\n")
    print(f"{out}: {n} files (logs, channel table, events; no stream data)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tvtuna", description="live over-the-air TV on an SDR: ATSC 1.0 and 3.0")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write ~/.tvtuna/config.json from these paths")
    for k, (env, what) in config.KEYS.items():
        p.add_argument("--" + k.replace("_", "-"), dest=k, default=None, help=what)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("status", help="who holds the radio, and the channel table")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("scan", help="which channels carry TV, and in which standard")
    p.add_argument("--rf", type=int, nargs="*", help="channels to try (default UHF 14-36)")
    p.add_argument("--antenna1", help="port for the ATSC 1.0 pass")
    p.add_argument("--antenna3", help="port for the ATSC 3.0 pass")
    p.add_argument("--secs", type=float, default=25.0, help="ATSC 1.0 dwell per channel")
    p.add_argument("--skip-atsc3", action="store_true")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("tune", help="ATSC 3.0: search the gains with the receiver as judge, remember them")
    p.add_argument("--rf", type=int, required=True)
    p.add_argument("--antenna")
    p.set_defaults(fn=cmd_tune)

    p = sub.add_parser("watch", help="watch a channel")
    p.add_argument("--rf", type=int, required=True)
    p.add_argument("--standard", choices=("auto", "1", "3"), default="auto")
    p.add_argument("--antenna")
    p.add_argument("--player", choices=("ffplay", "mpv", "none"))
    p.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until the window closes")
    p.add_argument("--watch-only", action="store_true", help="ATSC 1.0: search, then hold with no knob moved")
    p.add_argument("--resume", action="store_true", help="ATSC 1.0: start the search at the last proven hold")
    p.add_argument("--no-cc", action="store_true", help="ATSC 3.0: no caption track")
    p.add_argument("--chain-extra", default="", help="ATSC 3.0: extra args for the chain")
    p.add_argument("--max-restarts", type=int, default=10)
    p.add_argument("--cooldown", type=float, default=5.0)
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("serve", help="network tuner: M3U playlist + live MPEG-TS over HTTP for any IPTV player")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8554)
    p.add_argument("--idle", type=float, default=30.0, help="seconds without a client before the radio is released")
    p.set_defaults(fn=lambda a, cfg: __import__("tvtuna.serve", fromlist=["run"]).run(cfg, a.host, a.port, log, a.idle))

    p = sub.add_parser("engine", help="engine for another program's receiver: JSON lines on stdin/stdout, TS on a local TCP socket")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--tcp", type=int, default=8555, help="TS socket port (0 = pick a free one, told in 'hello')")
    p.add_argument("--replay", metavar="FILE.ts", help="no radio, no receivers: play this recorded transport stream at its own rate, in a loop")
    p.add_argument("--mbps", type=float, default=0.0, help="--replay: the stream rate, if the file's own clock cannot be read")
    p.set_defaults(fn=cmd_engine)

    p = sub.add_parser("doctor", help="check every piece the configuration points at; one PASS / FAIL line each")
    p.add_argument("--radio", action="store_true", help="also list the attached SDRs (not while something is streaming)")
    p.set_defaults(fn=lambda a, cfg: __import__("tvtuna.doctor", fromlist=["run"]).run(cfg, a.radio))

    p = sub.add_parser("report", help="zip the logs and tables for a bug report (no stream data)")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_report)

    a = ap.parse_args(argv)
    cfg = config.load()
    return a.fn(a, cfg)
