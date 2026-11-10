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
2 s after the first request, the ATSC 3.0 one after about 40 s (the receiver's start-up).

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
played at its own rate (read from its PCR), in a loop. Type `{"cmd":"tune","rf":7}` and it
answers `up`; the stream is on `127.0.0.1:8555`. It is the first thing to try from another
program. In OnAir's fork ([Felbs/OnAir](https://github.com/Felbs/OnAir), branch
`felbs/real-air`, `docs/EXTERNAL-ENGINE.md`): pick **External engine**, set the engine command
to `<your python> -m tvtuna engine --tcp 8555 --replay <file.ts>`, give any channel number,
press **Start and tune**. The picture plays and the panel shows the packet and continuity
counts of what was sent; servo, MER and radio holder read empty, because there is no radio.

## Setting up for real air

tvTuna is the part around the receivers, so the receivers come first. Tested with an
**SDRplay RSPdx on Windows 11**; the gain knobs it searches are the SDRplay RSP family's (LNA
state and IF gain reduction). Another SoapySDR radio would need its own knob map, which is not
written. The receivers themselves also run on Linux; tvTuna has not been run there yet.

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
