# Moorwatch — the on-board readout

TSCTide publishes access windows to an iCal feed, which is the right shape for
planning ashore and the wrong shape for the question asked on board: *right now,
how much water is over the mooring, and how long have I got?*

Moorwatch answers that, offline, on the boat netbook. It reuses TSCTide's own
harmonic model, secondary-port offset and window engine unchanged — it is a
read-only consumer, not a fork of the model.

```
  Moorwatch - Kerry Dancer - 3.5m Tide Required          <- the window title

  Fri 17 Jul 2026, 09:46 BST

  Current Height of Tide:   1.84 m above CD
  Est. under Keel:         -1.16 m at mooring
  Afloat at:               11:20 BST - in 1h 33m
  Depart after:            11:55 BST - in 2h 08m

  Next window 11:55 BST - 17:05 BST  (5h 10m)
```

Four readings, and the pairs are the point:

- **Height of tide** is the sea; **under the keel** is the gap under this hull.
  The drying height between them is why one can be positive while the other is
  negative.
- **Afloat at** is when the keel lifts; **Depart after** is when there is the
  safety margin on top — 15–20 minutes later on a flooding spring. Once out,
  they read *Aground at* and **Moor by**, which is the pair that matters: be
  back before the margin is gone, not before the boat touches.

The access line (drying + draught + margin) lives in the title bar because it
never changes. Colour is on two rows only: the keel goes green when there is
water under the boat, and **Moor by** goes amber, then red inside the last half
hour.

## Accuracy — read this before trusting a start time

Moorwatch runs the **harmonic model only**, because there is no connectivity at
the mooring. UKHO data (which the feed uses ashore) is better, and the gap is
not uniform:

| Where the access line sits | Window start error vs UKHO |
|---|---|
| ≥ 1.4 m above low water | −5 min mean, ~25 min worst |
| ≥ 2.4 m above low water | +1 min mean, ~25 min worst |
| **0.4 m above low water** | **−26 min mean, −74 min worst** |

The harmonic model reads about **+0.14 m high at low water** (and ~0.10 m low at
high water). Low in the flood the Langstone curve is deliberately flat — the
young-flood stand — so that phantom water becomes a large *timing* error, and it
errs **early**: it says there is water to leave on before there is.

A mooring that properly dries is unaffected; its access line sits well up the
steep part of the flood. A mooring that barely covers at low water is badly
affected. **Moorwatch detects this and warns on screen** rather than quietly
reporting an optimistic number — but the warning is not a fix. Where it appears,
treat the start as the earliest conceivable time, not a time to leave on, and
check the feed ashore.

High-water times, window *ends* and depths are all within the model's documented
accuracy (~15–20 min timing, ~0.13–0.19 m height) regardless.

## Install (Debian)

```bash
sudo apt install python3-tk python3-dateutil     # tk is NOT in a netinstall
git clone https://github.com/Consiliarius/TSCTide.git
cd TSCTide
python3 -m moorwatch --sync --url https://tsctide.uk --mooring 7
python3 -m moorwatch --gui
```

`python3-tk` is a separate Debian package and is not installed by a netinstall;
without it the tool fails at import. `python-dateutil` is the only third-party
dependency in the whole path (pure Python, no compilation).

No Docker on the netbook. The Dockerfile copies only `app/` and `scripts/`, so
moorwatch is not in the image and does not affect it.

## Telling it about your boat

The vessel configuration lives in **`moorwatch/config.json`** — a local,
gitignored cache of the mooring's row in TSCTide. TSCTide's `moorings` table
stays the single source of truth; this is a copy so the boat works offline.

Two ways to fill it in.

**Sync from TSCTide** (the normal path — configure the mooring in the TSCTide
web UI as usual, then, on wifi ashore):

```bash
python3 -m moorwatch --sync --url https://tsctide.uk --mooring 7
```

It reads `GET /api/moorings/{id}`, which is a PIN-free endpoint — the server
strips `pin_hash` before returning. No PIN is needed and none is stored, so the
netbook carries no credential. After the first sync the URL and mooring id are
remembered, so `--sync` on its own is enough thereafter, and it reports what
moved:

```
Synced Moonshadow (mooring 7) from https://tsctide.uk.
  draught 1.0 m, drying height 2.0 m, safety margin 0.3 m
Changed:
  drying_height_m: 2.2 -> 2.0
```

**Or edit `config.json` by hand** — no server needed. Only three fields matter
for the readout:

| field | meaning |
|---|---|
| `draught_m` | how deep the boat sits |
| `drying_height_m` | seabed level relative to chart datum |
| `safety_margin_m` | water you want under the keel before moving (default 0.3) |

`shallow_direction` and `shallow_extra_depth_m` are carried for a future wind
offset and unused today. `timezone` only affects display.

**It will not guess.** A fresh install copies the example, then refuses to show
anything until `draught_m` and `drying_height_m` are real:

```
Created moorwatch/config.json has no draught_m, drying_height_m.
Moorwatch will not guess a draught or a drying height.
```

This is deliberate. Wrong vessel numbers do not fail loudly — they produce a
complete, confident readout about a boat that does not exist. Refusing to show
a number is the correct answer to not knowing the boat.

## Usage

```bash
python3 -m moorwatch              # one-shot text readout
python3 -m moorwatch --watch      # refreshing console readout
python3 -m moorwatch --gui        # the always-on window
python3 -m moorwatch --at 2026-07-16T05:00Z    # readout for a given instant
python3 -m moorwatch --sync       # refresh vessel config from TSCTide
```

In the window: **F2** night scheme · **F11** fullscreen · **Esc** quit.

`--at` makes the tool a pure function of an instant, which is how its numbers
are checked against `/api/calculate` and the feed for a known tide.

## Keeping it current

Two things go stale, and both fail silently:

**The vessel config.** `drying_height_m` is not a surveyed constant — it is the
calibrated output of TSCTide's observation corpus, and it moves as calibration
improves. A stale config models the wrong seabed with complete confidence. Run
`--sync` whenever there is wifi ashore; the readout shows the config's age and
warns past 90 days.

**The model itself.** The constituents are recalibrated periodically (the
seasonal Sa/Ssa terms changed in July 2026). `git pull` ashore is the update
mechanism — there is no vendored copy to drift.

## Design notes

- **Height comes from the events + curve path**, never from
  `harmonic.predict_height_at_time`. The two disagree by RMS 0.18 m / 0.37 m
  worst, which exceeds a typical safety margin. `compute_access_windows` derives
  its crossings through the Langstone curve, so the depth shown must come from
  the same place or the readout would contradict its own countdown.
- **Two lines, two countdowns.** The boat lifts at `drying + draught`; it has
  *access* at `drying + draught + margin`. On a flooding spring those are ~15–20
  min apart. One countdown cannot honestly be labelled both.
- **The default `step_min` is fine.** `predict_events` refines each turning
  point by golden-section search against the curve itself, so event times are
  stable to ~1 s no matter where the sampling grid falls — which matters here,
  because this tool recomputes continuously against a moving `now` and its
  countdown has to tick down rather than jump. A finer step buys the identical
  answer for several times the work.
- **Nothing goes red for being aground.** A drying mooring is aground half of
  every cycle by design; red is reserved for a deadline that is arriving, so it
  means "be back" and nothing else.
- **The window says "Depart after" and "Moor by", never "Access".** "Access"
  reads just as naturally as getting *to* the boat, which is the tender's
  problem, a different depth, and one this tool does not compute.

## Not included

- **Barometric correction.** `barometric.correction_for_pressure()` takes a
  single hPa reading, so a typed barometer value would fit naturally later. A
  forecast synced ashore would not: its staleness tolerance is 36 h, so a day
  sail would run on day-old pressure.
- **Wind offset.** Needs live wind; `compute_next_window_with_wind` and the
  config's `shallow_*` fields are ready for a typed observed direction later.
- **Any write back to TSCTide.** Moorwatch is a display. Observation capture
  belongs in SYLog, where the observing happens.
