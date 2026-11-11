# tvTuna

Live over-the-air television on a software-defined radio, ATSC 1.0 and ATSC 3.0, from one
command, with the parts a television needs around a receiver and that receivers usually
leave out.

```
tvtuna scan                     which channels carry TV, and in which standard
tvtuna watch --rf N             watch one (the table knows the standard, the port, the gains)
tvtuna tune --rf N              ATSC 3.0: measure the gains with the receiver as judge
tvtuna serve                    a network tuner: playlist, live MPEG-TS over HTTP, XMLTV guide
tvtuna engine                   an engine for another program's receiver: JSON lines in and out, TS on a socket
tvtuna doctor                   is every piece installed and new enough: one PASS / FAIL line each
tvtuna status                   who holds the radio; the channel table
tvtuna report                   a zip of logs and tables for a bug report (no stream data)
```

## In another program's window

`tvtuna serve` makes the receivers a network tuner on `http://127.0.0.1:8554/`:

| path | what |
|---|---|
| `/playlist.m3u` | every channel in the table, grouped ATSC 1.0 / ATSC 3.0, `tvg-id` = `rf<N>`, `url-tvg` = the guide |
| `/ch/<N>.ts` | that channel as live MPEG-TS over HTTP, from the live edge; the first client tunes it, the last one's leaving releases the radio after an idle time |
| `/guide.xml` | XMLTV: the virtual channels and programmes decoded from the ATSC 1.0 PSIP tables of the streams seen so far |
| `/status.json` | tuned channel, standard, radio holder, the stream numbers |

Any IPTV player takes this. OnAir's **Internet TV (IPTV)** mode does: add the playlist address
as a list and our receivers play in its window, with its player, controls and guide. ATSC 1.0 is
the chain's own transport stream; ATSC 3.0 is the receiver's muxer output (HEVC copied, AC-3
and MP2 audio with their languages, captions as a soft subtitle track), because IPTV players take
MPEG-TS and not the fragmented-MP4 lanes themselves. Measured here: the ATSC 1.0 stream flows
2 s after the first request, the ATSC 3.0 one after 13 to 15 s and starting on a keyframe (the
receiver's start-up; with a receiver tree from before its low-latency options, which tvTuna
detects and does not ask for, about 40 s).

## What it is made of

The receivers are other projects, each the best this author knows for its standard:

| | receiver | role here |
|---|---|---|
| ATSC 1.0 (8-VSB) | [Software TV Tuner](https://github.com/Felbs/Software-TV-Tuner) `tv_live.py` | runs once per channel, takes gain changes live (its persistent retune), writes the transport stream |
| ATSC 3.0 | the [atsc3](https://github.com/Felbs/atsc3) receiver (bootstrap to pictures: L1, LDM, LDPC, ALP, ROUTE and MMTP, AC-4, captions) | its own orchestrator and supervisor; tvTuna launches it with measured gains and reads its health |
| tuning | [gr-rxtune](https://github.com/Felbs/gr-rxtune) | the gain search on decoder truth, then the servo: hold while comfortable, probe a neighbour when the margin is thin, re-search when the decode is lost |
| the radio | a site warden (`radio_lock.py`) | one SDR shared by every tenant on the machine; a streaming receiver is asked to stop, never killed |

tvTuna itself is the glue and the habits:

* **One radio, many tenants.** Every run goes through the warden. A higher-priority tenant
  (a satellite pass) makes a watch give the radio up cleanly; a stop request does the same;
  a lock that is no longer ours is noticed and the run stops instead of streaming unprotected.
* **Decoder truth, not a signal meter.** The ATSC 1.0 gain search is judged on the equaliser's
  training error AND on pictures arriving in the transport stream; the ATSC 3.0 search on the
  share of FEC blocks that come out clean. A setting that reads well but does not decode is not
  a good setting.
* **A fade is not a wedge.** The ATSC 3.0 supervisor's rule, read back here: frames advancing
  with nothing decoding is the air, and the right move is to wait; frames frozen is the code,
  and the right move is a restart. The ATSC 1.0 servo's equivalent: no pictures for a while
  triggers a re-search, and a fruitless one doubles the wait.
* **The player sits at the live edge** of the growing stream file, realigned on rotation,
  kept alive with null packets through quiet spells; closing the window ends the watch.
* **The table remembers**: standard and port per channel, the gains a search proved, when.
  The next watch starts there.
* **Numbers in the log**: packets, TEI, continuity errors, nulls, MER percentiles, pictures
  per second, knob writes, time off the held setting. What was measured, not what was felt.

## Try it without a radio

```
pip install .
tvtuna engine --replay some_recording.ts
```

That is the whole engine protocol with nothing behind it but a file: any MPEG-TS recording,
played at its own rate (read from its PCR), in a loop. No recording at hand? ffmpeg makes a
20-second test pattern shaped like a broadcast (MPEG-2 video, AC-3 sound, constant rate):

```
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=30000/1001 -f lavfi -i sine=frequency=440:sample_rate=48000 -t 20 -c:v mpeg2video -b:v 6M -maxrate 6M -bufsize 2M -g 15 -pix_fmt yuv420p -c:a ac3 -b:a 192k -f mpegts -muxrate 8M pattern.ts
```
 Type `{"cmd":"tune","rf":7}` and it
answers `up`; the stream is on `127.0.0.1:8555`. It is the first thing to try from another
program. In OnAir's fork ([Felbs/OnAir](https://github.com/Felbs/OnAir), branch
`felbs/real-air`, `docs/EXTERNAL-ENGINE.md`): pick **External engine**, set the engine command
to `<your python> -m tvtuna engine --tcp 8555 --replay <file.ts>`, give any channel number,
press **Start and tune**. The picture plays and the panel shows the packet and continuity
counts of what was sent; the servo line reads `replay (no radio)` and there is no MER, because
there is no radio. Checked this way on 2026-11-09 with the pattern above: 1345 pictures shown,
0 late, 0 decode errors, 0 continuity errors across two passes of the file.

## Measured on Linux, without a radio (2026-11-11)

One machine: Ryzen 5 1600X (6 cores, 12 threads), GTX 1060, Arch-based Linux 7.2, Python 3.14.8,
GNU Radio 3.10.12, ffmpeg 9.0.2. tvTuna at 47cacfd, the ATSC 3.0 receiver with its packed
int16 LDPC kernel (ABI 7) rebuilt. Everything served on `127.0.0.1`.

* **Tests**: `pytest tests`: 13 passed, 0 failed.
* **`tvtuna doctor`**: 15 PASS, 1 FAIL, 3 SKIP. The FAIL is "tv_live.py keeps the stream through a
  gain step": the Software TV Tuner checkout on that machine is a branch from before the
  2026-11-09 fix (ATSC 1.0 only). The SKIPs: no warden configured, no channel table, radio not
  looked for. Before a `git pull` of gr-rxtune the count was 14 / 2 / 3 ("gr-rxtune has the servo").

**The engine on a banked ATSC 3.0 capture.** `tvtuna engine` has no switch for a recorded IQ
file (`--replay` takes a transport stream), so the receiver's own replay options
(`--capture FILE --rate 8e6 --realtime --shed`: the file at the air's pace, Frames shed as live
air would shed them) were handed to it through the remembered-gains entry of a throwaway channel
table (`TVTUNA_STATE` pointed at a scratch directory). The engine code that ran is the code as
committed: line protocol, orchestrator, low-latency muxer, the socket. The capture is 120 s of
one 6 MHz multiplex at 8 MS/s (485 Frames of 247 ms; HEVC 1280x720 at 59.94, AC-3 5.1, a second
language, captions). A client connects at the tune and records 36 s; a second client then takes
the socket mid-stream and records 36 s. Three runs.

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| process start to `hello` | 0.09 s | 0.09 s | 0.09 s |
| `tune` to the `up` event | 10.24 s | 9.44 s | 9.45 s |
| `tune` to the first TS byte at the client (keep-alive nulls not counted) | 10.52 s | 9.51 s | 9.51 s |
| first client: first video packet / first decoded picture | keyframe / I | keyframe / I | keyframe / I |
| first client, first 30 s decoded by ffmpeg: pictures | 1799 | 1799 | 1799 |
| first client: lines from the HEVC, AC-3 and MP2 decoders | 0 | 0 | 0 |
| second client (joined 46 s after the tune): first byte after connect | 14 ms | 6 ms | 6 ms |
| second client: first video packet / first decoded picture | keyframe / I | keyframe / I | keyframe / I |
| second client, 30 s: pictures; decoder lines | 1799; 0 | 1799; 0 | 1799; 0 |
| transport errors (TEI) in either recording | 0 | 0 | 0 |

1799 pictures in 30.08 s is every picture of a 59.94 Hz stream. The first client's stream opens
with the tables (SDT, PAT, PMT) and then the keyframe; the second client's opens with the PAT
in front of the latest keyframe, which is what `player.join_point` is for.

**One thing ffmpeg does say.** Its MPEG-TS demuxer prints `Packet corrupt` once per 2 s slot:
15 or 16 times in each 30 s, in every recording of every run. The receiver's muxer writes the
sink one ffmpeg run per slot, and each run restarts the 4-bit continuity counter of every
stream: 16 to 18 restarts per stream in a 36 s recording, none carrying a discontinuity flag.
The decoders are not affected (0 lines, every picture). A player that drops flagged packets is:
the same 30 s read with `-fflags +discardcorrupt` (as `player.py`'s ffplay command does) gives
1786 pictures instead of 1799 (one recording measured). The receiver has a counter rewrite for
exactly this on its player pipe; on the file sink that tvTuna serves from it is not applied.

**Does the receiver keep up?** Yes, with little to spare on six cores.

| | |
|---|---|
| paced at the air's rate, to the end of the capture (runs 2 and 3, identical) | 485 of 485 Frames, 119.849 s of air in 119.360 s: 1.004x |
| FEC blocks | 35,890 of 35,890 converged (485 x 74: no Frame shed) |
| 5 s reports, all three runs | 0.99x to 1.16x; input queue 7 Frames deep while starting, then 0; 0 underruns |
| slowest stage against the 247 ms Frame budget | 1.05x to 1.06x |
| re-acquisitions, supervisor restarts | 0, 0 |
| muxer | 224 to 227 ms per 2 s slot; dropped 0, failed 0, stalled 0 s |
| audio decode | 2.4x real time; 0 padded, 0 behind |
| media units | 237 complete, 3 lost, the same 3 in every run, 0 sequence breaks |
| not paced, nothing shed, the whole stack running (one run) | 119.849 s of air in 112.117 s: 1.069x |

Run 1 was stopped by the test at 84 s (306 Frames at 75 s, 1.00x to 1.02x, same queue). The 3
lost media units being identical in every run points at the capture's own ends, not at this
machine; that is a reading, not a measurement. The margin is the number to keep in mind: 7 %
on this processor with nothing else running.

**`tvtuna engine --replay` on the transport stream** that run produced (53 MB, 3.60 Mb/s read
from its PCR), three runs: `hello` 0.13 to 0.14 s after the process starts, the first TS byte
2 to 3 ms after `tune`, first video packet a keyframe, 1799 pictures in 30.08 s, 0 decoder
lines, the same 16 demuxer flags.

## Setting up for real air

tvTuna is the part around the receivers, so the receivers come first. Tested with an
**SDRplay RSPdx on Windows 11**; the gain knobs it searches are the SDRplay RSP family's (LNA
state and IF gain reduction). Another SoapySDR radio would need its own knob map, which is not
written. The receivers themselves also run on Linux. tvTuna has been run there without a radio (the tests, `doctor`, and the engine on a banked capture: the section below); its live paths on Linux are not tested yet.

1. **An interpreter with SoapySDR and GNU Radio 3.10.** [radioconda](https://github.com/ryanvolz/radioconda)
   has both. Install your radio's driver (for an RSP, the SDRplay API).
2. **The ATSC 1.0 receiver**: clone [Software TV Tuner](https://github.com/Felbs/Software-TV-Tuner)
   and follow its `docs/install/` page for your system until its own tuner shows a picture.
3. **The tuning library**: clone [gr-rxtune](https://github.com/Felbs/gr-rxtune). Nothing to build.
4. **The ATSC 3.0 receiver** (optional): clone [atsc3](https://github.com/Felbs/atsc3) and follow
   its `INSTALL.md` (the C LDPC kernel matters) until `python tools/atsc3_doctor.py` passes.
5. **tvTuna**: `pip install .` in this clone, with the interpreter from step 1. Then

   ```
   tvtuna init --python <that interpreter> --stvt-tv-live <Software-TV-Tuner>/tools/tv_live.py
               --rxtune-src <gr-rxtune>/src --atsc3-root <atsc3>
   tvtuna doctor            every piece, one PASS / FAIL line each, and what to do about a FAIL
   tvtuna doctor --radio    the same, and lists the attached SDRs
   ```

   `init` writes `~/.tvtuna/config.json`; every key also has a `TVTUNA_*` environment variable
   and no path is built in. `--antenna-atsc1` / `--antenna-atsc3` name the SDR's antenna ports
   (default `Antenna A` / `Antenna B`); `--radio-lock` names a site warden module if several
   programs share the radio, and is not needed otherwise.

   Two antennas on two ports: name both for ATSC 1.0 (`--antenna-atsc1 "Antenna A,Antenna B"`, or
   `tvtuna scan --antenna1 "Antenna A,Antenna B"`). The scan then tests every channel on each port
   and files the channel under the one with the higher MER; `watch` and `serve` use that port for
   that channel from then on. Here the better port differs between two neighbouring channels, by
   about 1 dB each way.
6. **First picture**: `tvtuna watch --rf N` on an ATSC 1.0 channel you can receive. The first
   watch of a channel searches the gains (two to three minutes, the picture comes and goes while
   it looks); the table then remembers what it proved and the next start is a short confirmation.
   For an ATSC 3.0 channel run `tvtuna tune --rf N` once first; that measures its gains and marks
   the channel's standard.
7. **Behind OnAir**: engine command `<that interpreter> -m tvtuna engine --tcp 8555`. Give the
   full path of the interpreter: the `python` OnAir finds on its own PATH is usually a different one.

### With a coding agent

The steps are written so that a coding agent (Claude Code or the like) can run them: each one
ends in a command whose output says pass or fail, and `tvtuna doctor` names the piece that is
wrong. Give it this file and the two receivers' install pages. It cannot plug in the radio or
aim the antenna, and a first picture still depends on a signal above the cliff.

## Credits

The three receivers and the tuning library are the work they stand on. The servo's shape
(hold while locked, act on triggers only, undo a step that made things worse) is an idea from
[OnAir](https://github.com/adswill/OnAir)'s gain controller, adopted as an idea and judged
here on decoder output; no OnAir code is in this tree.

## License

GPL-3.0-or-later. tvTuna drives GPL receivers and is one itself.
