# SPDX-License-Identifier: GPL-3.0-or-later
"""The channel scan's first run on air (2026-11-09) failed three ways, pinned here without a radio.

1. An empty channel took two minutes: the lock test waited its whole start-up time for an
   equaliser line that an empty channel never prints.
2. The scan never told the radio warden it was alive, so after 90 s the warden read the radio
   as free and the scan stopped itself after one channel.
3. The channels it never tested were then filed as "no ATSC 3.0 carrier" (one of them a known
   ATSC 1.0 station), and saving that table crashed on a row with no channel number."""
import importlib
import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def fresh(tmp_path):
    os.environ["TVTUNA_STATE"] = str(tmp_path)
    import tvtuna.config, tvtuna.state, tvtuna.atsc1, tvtuna.scan
    for m in (tvtuna.config, tvtuna.state, tvtuna.atsc1, tvtuna.scan):
        importlib.reload(m)
    return tvtuna.config, tvtuna.state, tvtuna.atsc1, tvtuna.scan


class FakeLock:
    """The warden as the scan sees it: asks for the radio back on the `yield_at`-th check."""

    def __init__(self, yield_at=None):
        self.beats, self.checks, self.yield_at = 0, 0, yield_at

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def heartbeat(self):
        self.beats += 1

    def should_yield(self):
        self.checks += 1
        return "a higher priority waiter" if self.yield_at and self.checks >= self.yield_at else None


def wire(scan, monkeypatch, lock, atsc1_rows, atsc3_rows, asked3):
    monkeypatch.setattr(scan.radio, "site_lock", lambda cfg, **kw: lock)

    def probe1(cfg, rf, antenna, out_dir, secs=25.0, beat=None, **kw):
        assert beat is not None, "the lock test must be handed the heartbeat"
        beat()
        return dict(atsc1_rows[rf])

    def probe3(cfg, rf, antenna, secs=20.0, **kw):
        asked3.append(rf)
        return dict(atsc3_rows[rf])

    monkeypatch.setattr(scan.atsc1, "probe", probe1)
    monkeypatch.setattr(scan.atsc3, "probe", probe3)


def test_a_full_scan_files_each_channel_under_what_it_carries(tmp_path, monkeypatch):
    config, state, atsc1, scan = fresh(tmp_path)
    lock, asked3 = FakeLock(), []
    wire(scan, monkeypatch, lock,
         {32: {"rf": 32, "standard": None, "error": "no field sync in 15 s"},
          33: {"rf": 33, "standard": None, "error": "no field sync in 15 s"},
          34: {"rf": 34, "standard": "atsc1", "mer_db": 19.3, "pictures": 48}},
         {32: {"rf": 32, "standard": None},
          33: {"rf": 33, "standard": "atsc3", "frames": 83, "fec_pct": 100.0}}, asked3)
    lines = []
    table = scan.run(config.Config(), [32, 33, 34], "Antenna A", "Antenna B", log=lines.append)
    assert [r["rf"] for r in table] == [32, 33, 34]
    assert [r["standard"] for r in table] == [None, "atsc3", "atsc1"]
    assert asked3 == [32, 33], "ATSC 3.0 is looked for only where ATSC 1.0 was not found"
    assert lock.beats >= 6, "the warden hears from the scan on every channel"
    saved = state.load()
    assert saved["channels"]["34"]["standard"] == "atsc1" and saved["channels"]["34"]["antenna"] == "Antenna A"
    assert saved["channels"]["33"]["standard"] == "atsc3" and saved["channels"]["33"]["antenna"] == "Antenna B"
    assert "standard" not in saved["channels"]["32"]
    assert "error" not in saved["channels"]["33"]["last_scan"], "an ATSC 3.0 channel keeps no ATSC 1.0 complaint"
    assert any("no field sync" in ln for ln in lines), "an empty channel says why it was passed over"


def test_a_scan_cut_short_keeps_what_it_tested_and_claims_nothing_else(tmp_path, monkeypatch):
    config, state, atsc1, scan = fresh(tmp_path)
    lock, asked3 = FakeLock(yield_at=2), []
    wire(scan, monkeypatch, lock,
         {32: {"standard": None, "error": "no field sync in 15 s"}},      # a row with no "rf": the crash
         {}, asked3)
    lines = []
    table = scan.run(config.Config(), [32, 33, 34, 35], "Antenna A", "Antenna B", log=lines.append)
    assert [r["rf"] for r in table] == [32], "only the tested channel is in the table"
    assert asked3 == [], "untested channels are not sent to the ATSC 3.0 pass"
    saved = state.load()
    assert list(saved["channels"]) == ["32"] and saved["scans"][-1]["rfs"] == [32]
    assert any("cut short" in ln and "3 not tested" in ln for ln in lines), lines


def test_two_ports_each_channel_is_filed_under_the_port_that_received_it_best(tmp_path, monkeypatch):
    """Measured on air 2026-11-10: one channel is 1.3 dB better on the first port, its neighbour up to
    0.9 dB better on the second.  With both ports named the scan tries each and files the channel
    under the better one; a channel only one port can lock is filed under that port."""
    config, state, atsc1, scan = fresh(tmp_path)
    lock = FakeLock()
    monkeypatch.setattr(scan.radio, "site_lock", lambda cfg, **kw: lock)
    air = {(34, "Antenna A"): {"rf": 34, "standard": "atsc1", "mer_db": 18.9, "pictures": 58},
           (34, "Antenna B"): {"rf": 34, "standard": "atsc1", "mer_db": 17.6, "pictures": 58},
           (35, "Antenna A"): {"rf": 35, "standard": "atsc1", "mer_db": 17.0, "pictures": 86},
           (35, "Antenna B"): {"rf": 35, "standard": "atsc1", "mer_db": 17.6, "pictures": 86},
           (36, "Antenna A"): {"rf": 36, "standard": None, "error": "no field sync in 15 s"},
           (36, "Antenna B"): {"rf": 36, "standard": "atsc1", "mer_db": 16.2, "pictures": 40},
           (33, "Antenna A"): {"rf": 33, "standard": None, "error": "no field sync in 15 s"},
           (33, "Antenna B"): {"rf": 33, "standard": None, "error": "no field sync in 15 s"}}
    asked = []

    def probe1(cfg, rf, antenna, out_dir, secs=25.0, beat=None, **kw):
        asked.append((rf, antenna))
        return dict(air[(rf, antenna)])

    monkeypatch.setattr(scan.atsc1, "probe", probe1)
    lines = []
    table = scan.run(config.Config(), [33, 34, 35, 36], "Antenna A, Antenna B", "Antenna B", log=lines.append,
                     skip_atsc3=True)
    assert asked == [(rf, a) for rf in (33, 34, 35, 36) for a in ("Antenna A", "Antenna B")]
    saved = state.load()["channels"]
    assert saved["34"]["antenna"] == "Antenna A" and saved["35"]["antenna"] == "Antenna B"
    assert saved["36"]["antenna"] == "Antenna B" and saved["36"]["last_scan"]["mer_db"] == 16.2
    assert "antenna" not in saved["33"] and "standard" not in saved["33"]
    assert saved["35"]["last_scan"]["ports"]["Antenna A"]["mer_db"] == 17.0, "the losing port's reading is kept"
    assert any("RF 35" in ln and "on Antenna B (Antenna A 17.0 dB)" in ln for ln in lines), lines
    assert any("RF 36" in ln and "on Antenna B (Antenna A no lock)" in ln for ln in lines), lines
    assert [r["standard"] for r in table] == [None, "atsc1", "atsc1", "atsc1"]


def test_one_port_is_the_scan_as_it_was(tmp_path, monkeypatch):
    config, state, atsc1, scan = fresh(tmp_path)
    lock, asked3 = FakeLock(), []
    wire(scan, monkeypatch, lock, {34: {"rf": 34, "standard": "atsc1", "mer_db": 19.3, "pictures": 48}}, {}, asked3)
    lines = []
    table = scan.run(config.Config(), [34], "Antenna A", "Antenna B", log=lines.append)
    assert "ports" not in table[0] and table[0]["antenna"] == "Antenna A"
    assert any(ln.strip() == "RF 34  ATSC 1.0  MER 19.3 dB  48 pictures" for ln in lines), lines
    assert lock.checks == 1


def test_a_scan_asked_for_the_radio_between_two_ports_stops_there(tmp_path, monkeypatch):
    config, state, atsc1, scan = fresh(tmp_path)
    lock, asked3 = FakeLock(yield_at=2), []
    wire(scan, monkeypatch, lock, {34: {"rf": 34, "standard": "atsc1", "mer_db": 19.3, "pictures": 48}}, {}, asked3)
    lines = []
    table = scan.run(config.Config(), [34, 35], "Antenna A,Antenna B", "Antenna B", log=lines.append)
    assert [r["rf"] for r in table] == [34] and list(table[0]["ports"]) == ["Antenna A"]
    assert state.load()["channels"]["34"]["antenna"] == "Antenna A", "what was tested is kept"
    assert any("stopped during RF 34" in ln for ln in lines) and asked3 == []


def test_a_chain_that_dies_before_it_has_the_radio_gets_a_second_go(tmp_path, monkeypatch):
    config, state, atsc1, scan = fresh(tmp_path)
    lock = FakeLock()
    monkeypatch.setattr(scan.radio, "site_lock", lambda cfg, **kw: lock)
    monkeypatch.setattr(scan, "RETRY_WAIT_S", 0.0)
    answers = [{"rf": 35, "standard": None, "error": "chain did not start"},
               {"rf": 35, "standard": "atsc1", "mer_db": 17.5, "pictures": 80}]
    monkeypatch.setattr(scan.atsc1, "probe",
                        lambda cfg, rf, antenna, out_dir, secs=25.0, beat=None, **kw: answers.pop(0))
    table = scan.run(config.Config(), [35], "Antenna B", "Antenna B", log=lambda m: None, skip_atsc3=True)
    assert table[0]["standard"] == "atsc1" and answers == []
    assert state.load()["channels"]["35"]["antenna"] == "Antenna B"


def test_a_port_setting_names_one_port_or_several(tmp_path):
    config, state, atsc1, scan = fresh(tmp_path)
    assert config.ports("Antenna A") == ["Antenna A"]
    assert config.ports("Antenna A, Antenna B") == ["Antenna A", "Antenna B"]
    assert config.ports("Antenna A,") == ["Antenna A"]



FAKE_CHAIN = '''import sys, time
print("2026-11-09 22:53:49,439 [sdr_agent.tv_live] INFO: PERSIST-RETUNE: watcher armed on x", flush=True)
if "--rf" in sys.argv and sys.argv[sys.argv.index("--rf") + 1] == "34":
    for i in range(400):
        print("[eq-long t=%6.2fs] fs=%d fs_err_rms=0.5000 |taps|=1.000" % (i * 0.05, i + 1), flush=True)
        time.sleep(0.05)
time.sleep(60)
'''


def rxtune_src():
    for p in (os.environ.get("TVTUNA_TEST_RXTUNE_SRC"), os.path.join(ROOT, "..", "gr-rxtune", "src")):
        if p and os.path.isdir(os.path.join(p, "rxtune")):
            return os.path.abspath(p)
    return None


@pytest.mark.skipif(rxtune_src() is None, reason="needs gr-rxtune next to this clone (or TVTUNA_TEST_RXTUNE_SRC)")
def test_an_empty_channel_is_answered_in_seconds_and_the_lock_is_kept_alive(tmp_path):
    config, state, atsc1, scan = fresh(tmp_path)
    fake = tmp_path / "fake_tv_live.py"
    fake.write_text(FAKE_CHAIN)
    cfg = config.Config(python=sys.executable, stvt_tv_live=str(fake), rxtune_src=rxtune_src())
    beats = []
    t0 = time.time()
    r = atsc1.probe(cfg, 32, "Antenna A", str(tmp_path / "rf32"), secs=25.0, beat=lambda: beats.append(time.time()),
                    sync_wait_s=2.0)
    took = time.time() - t0
    assert r["rf"] == 32 and r["standard"] is None and "no field sync" in r["error"], r
    assert took < 25.0, f"an empty channel took {took:.0f} s"
    assert len(beats) >= 3, "the heartbeat runs through the wait"

    beats.clear()
    r = atsc1.probe(cfg, 34, "Antenna A", str(tmp_path / "rf34"), secs=3.0, beat=lambda: beats.append(time.time()),
                    sync_wait_s=2.0)
    assert "error" not in r and r["mer_db"] == pytest.approx(20.0, abs=0.1), r      # 20 log10(5 / 0.5)
    assert r["standard"] is None, "an equaliser line with no pictures is not a lock"
    assert len(beats) >= 10, "the heartbeat runs through the measurement"
