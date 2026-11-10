# SPDX-License-Identifier: GPL-3.0-or-later
"""The replay engine: the protocol and the socket with no radio and no receiver."""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tvtuna import replay                                    # noqa: E402

RATE = 2_000_000 / 8.0          # bytes per second of the synthetic stream
PKT = 188


def synthetic_ts(path: str, seconds: float = 4.0) -> None:
    """One PID of payload with a PCR every 40 packets, clocked for RATE."""
    n = int(RATE * seconds) // PKT
    cc = 0
    with open(path, "wb") as f:
        for i in range(n):
            if i % 40 == 0:
                pcr = int(i * PKT / RATE * 27e6)
                base, ext = divmod(pcr, 300)
                af = bytes([7, 0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                            ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF])
                f.write(bytes([0x47, 0x01, 0x00, 0x30 | cc]) + af + bytes(PKT - 4 - len(af)))
            else:
                f.write(bytes([0x47, 0x01, 0x00, 0x10 | cc]) + bytes(PKT - 4))
            cc = (cc + 1) & 0x0F


def test_rate_is_read_from_the_streams_own_clock(tmp_path):
    p = str(tmp_path / "s.ts")
    synthetic_ts(p)
    got = replay.byte_rate(p)
    assert got is not None and abs(got - RATE) / RATE < 0.01, got


def test_no_pcr_means_no_rate(tmp_path):
    p = str(tmp_path / "n.ts")
    with open(p, "wb") as f:
        f.write((bytes([0x47, 0x01, 0x00, 0x10]) + bytes(PKT - 4)) * 500)
    assert replay.byte_rate(p) is None


def test_engine_replay_speaks_the_protocol_and_paces_the_stream(tmp_path):
    p = str(tmp_path / "s.ts")
    synthetic_ts(p, seconds=1.5)          # shorter than the read below: the loop is exercised
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    env = dict(os.environ, TVTUNA_STATE=str(tmp_path / "state"), PYTHONPATH=root)
    eng = subprocess.Popen([sys.executable, "-m", "tvtuna", "engine", "--tcp", "0", "--replay", p],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        hello = json.loads(eng.stdout.readline())
        assert hello["ev"] == "hello" and hello["tcp"] > 0 and hello["radio"] is None
        eng.stdin.write('{"cmd": "tune", "rf": 7}\n')
        eng.stdin.flush()
        up = json.loads(eng.stdout.readline())
        assert up["ev"] == "up" and up["rf"] == 7 and up["standard"] == "replay"

        s = socket.create_connection(("127.0.0.1", hello["tcp"]), timeout=5)
        s.settimeout(5)
        got, t0 = b"", time.monotonic()
        while time.monotonic() - t0 < 3.0:
            got += s.recv(65536)
        dt = time.monotonic() - t0
        assert got[0] == 0x47 and got[PKT] == 0x47, "the stream starts on a packet boundary"
        assert 0.7 < len(got) / dt / RATE < 1.3, f"paced at {len(got) / dt * 8 / 1e6:.2f} Mb/s, wanted 2.00"

        eng.stdin.write('{"cmd": "status"}\n')
        eng.stdin.flush()
        st = None
        for _ in range(10):
            line = json.loads(eng.stdout.readline())
            if line["ev"] == "status" and line["last"]["packets"] > 0:
                st = line
                break
        assert st is not None, "a status line with packets counted"
        assert st["last"]["video"] is True, "packets are going out to a reader"
        s.close()
        assert st["last"]["cc_errors"] == 0 and st["last"]["mer_db"] is None and st["last"]["loops"] >= 1
        assert "replay" in st["last"]["state"]

        eng.stdin.write('{"cmd": "quit"}\n')
        eng.stdin.flush()
        rest = [json.loads(x) for x in eng.stdout.read().splitlines() if x.strip()]
        assert any(x["ev"] == "released" and x["rf"] == 7 for x in rest)
        assert eng.wait(timeout=10) == 0
    finally:
        if eng.poll() is None:
            eng.kill()
