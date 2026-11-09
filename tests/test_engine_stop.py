# SPDX-License-Identifier: GPL-3.0-or-later
"""Two faults found on air on 2026-11-09, pinned without a radio.

1. Stopping a channel took exactly 90 s: the stopper joined the worker while holding the
   engine's lock, and the worker's last step asked for that lock.
2. A saved hold equal to the default start setting read as "no hold", so the viewer sat
   through a full search (cells that decode nothing included) on a channel already known."""
import importlib
import json
import os
import threading
import time


def fresh(tmp_path, channels=None):
    os.environ["TVTUNA_STATE"] = str(tmp_path)
    if channels is not None:
        (tmp_path / "channels.json").write_text(json.dumps({"channels": channels, "scans": []}))
    import tvtuna.config, tvtuna.state, tvtuna.serve
    for m in (tvtuna.config, tvtuna.state, tvtuna.serve):
        importlib.reload(m)
    return tvtuna.config, tvtuna.state, tvtuna.serve


def test_stop_does_not_wait_out_the_join(tmp_path):
    config, state, serve = fresh(tmp_path)
    eng = serve.Engine(config.Config(), lambda m: None)
    eng.rf, eng.up = 7, True

    def worker():                      # the shape of _run_atsc1: serve until asked to stop, then finish
        eng.stop_flag.wait(30)
        eng._done(7)

    eng.worker = threading.Thread(target=worker, daemon=True)
    eng.worker.start()
    t0 = time.time()
    with eng.lock_obj:                 # as the engine's stop / quit and a channel change do
        eng._stop_locked()
    assert time.time() - t0 < 2.0, f"stop took {time.time() - t0:.1f} s"
    assert not eng.worker.is_alive() and not eng.up


def test_a_hold_equal_to_the_default_start_is_still_a_hold(tmp_path):
    hold = {"atsc1_hold": {"Antenna A": {"held": {"gain:RFGR": 4, "gain:IFGR": 40}, "proven": True}}}
    config, state, serve = fresh(tmp_path, {"7": hold, "8": {}})
    assert state.atsc1_start(7, "Antenna A") == (4, 40) == state.atsc1_start(8, "Antenna A")
    assert state.atsc1_hold(7, "Antenna A") == (4, 40)
    assert state.atsc1_hold(8, "Antenna A") is None
    assert state.atsc1_hold(7, "Antenna B") is None
