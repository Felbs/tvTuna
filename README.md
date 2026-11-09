# tvTuna

Live over-the-air television on a software-defined radio, ATSC 1.0 and ATSC 3.0, from one
command, with the parts a television needs around a receiver and that receivers usually
leave out.

```
tvtuna scan                     which channels carry TV, and in which standard
tvtuna watch --rf 34            watch one (the table knows the standard, the port, the gains)
tvtuna tune --rf 33             ATSC 3.0: measure the gains with the receiver as judge
tvtuna status                   who holds the radio; the channel table
tvtuna report                   a zip of logs and tables for a bug report (no stream data)
```

## What it is made of

The receivers are other projects, each the best this author knows for its standard:

| | receiver | role here |
|---|---|---|
| ATSC 1.0 (8-VSB) | [Software TV Tuner](https://github.com/Felbs/Software-TV-Tuner) `tv_live.py` | runs once per channel, takes gain changes live (its persistent retune), writes the transport stream |
| ATSC 3.0 | the `atsc3` receiver (bootstrap to pictures: L1, LDM, LDPC, ALP, ROUTE and MMTP, AC-4, captions) | its own orchestrator and supervisor; tvTuna launches it with measured gains and reads its health |
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

## Setting up

`tvtuna init --python <interpreter with SoapySDR and GNU Radio> --stvt-tv-live <path to
tv_live.py> --atsc3-root <atsc3 tree> --rxtune-src <gr-rxtune/src> --radio-lock <radio_lock.py>
--sdrplay-api <SDRplay API dll dir>` writes `~/.tvtuna/config.json`. Every key also has a
`TVTUNA_*` environment variable. No path is built in.

## Credits

The three receivers and the tuning library are the work they stand on. The servo's shape
(hold while locked, act on triggers only, undo a step that made things worse) is an idea from
[OnAir](https://github.com/adswill/OnAir)'s gain controller, adopted as an idea and judged
here on decoder output; no OnAir code is in this tree.

## License

GPL-3.0-or-later. tvTuna drives GPL receivers and is one itself.
