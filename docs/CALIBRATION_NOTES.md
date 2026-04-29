# Calibration notes

State of the tidal model and harmonic prediction calibration as of
29 April 2026. Records what has been refined, what has not, and the
tradeoffs that remain open.

## Calibration corpus

Located at `app/calibration_data/`. Currently contains 16 days of
half-hourly UKHO data for Langstone Harbour (station 0066), spanning
14 April – 22 April and 29 April – 5 May 2026. Total 768 samples
covering a full neaps-springs-neaps progression.

Re-running the analysis is straightforward:

```
docker exec tidal-access python -m scripts.calibrate_from_ukho_week
```

Optional parameter sweep over the ebb-stand parameters:

```
docker exec tidal-access python -m scripts.sweep_ebb_params
```

Adding new weeks is a matter of dropping a new CSV into
`app/calibration_data/` and re-running. The script auto-discovers
all CSVs in the directory.

## Calibration accuracy as of v2.5.2

| Path                           | Mean bias  | RMS error | Max error |
|--------------------------------|-----------:|----------:|----------:|
| UKHO curve, overall            | +0.121 m   | 0.255 m   | 0.70 m    |
| UKHO curve, flood phase        | +0.134 m   | 0.290 m   | 0.70 m    |
| UKHO curve, ebb phase          | +0.002 m   | 0.139 m   | 0.60 m    |
| Harmonic, raw                  | -0.068 m   | 0.218 m   | 0.60 m    |
| Harmonic, production path      | +0.024 m   | 0.222 m   | 0.68 m    |

The production path now tracks raw harmonic output to within 4mm RMS
across the full corpus.

## Items remaining to be addressed

### 0. Harmonic-prediction duplicate-rows on within-day refresh

**Current state**: each call to the harmonic refresh path
(`harmonic_predict_events -> apply_offset -> store_harmonic_predictions`)
inserts a fresh row per predicted tide event tagged with the current
`generated_at` time. The row's primary deduplication key includes the
exact `timestamp`, which drifts by seconds across runs as the harmonic
synthesis is re-evaluated. Consequence: two refreshes of the harmonic
feed within the same day produce two rows for what is the same tide
cycle, with timestamps that differ by tens of seconds.

**Symptom**: Tides tab "Forecast (next 180 days)" view shows duplicate
HW/LW entries for the same tide cycle - times appearing twice with
slight differences. The deduplication in `get_harmonic_predictions(
latest_only=True)` is per exact `(timestamp, event_type)` key and
cannot collapse near-duplicates from independent runs.

**Why the design allowed this**: the `(timestamp, event_type,
generated_at)` UNIQUE constraint was intended to preserve historical
predictions across multiple days for later delta-vs-actual analysis.
The assumption was "one refresh per day" so the timestamp would be
identical between consecutive same-cycle predictions. That assumption
breaks under multiple-refreshes-per-day, which is normal during
operator activity.

**Immediate workaround** (run when duplicates are observed):

```
docker exec tidal-access sqlite3 /app/data/tides.db "
DELETE FROM harmonic_predictions
WHERE generated_at < (SELECT MAX(generated_at) FROM harmonic_predictions);
"
```

This keeps only the most recent batch.

**Permanent fix**: change the deduplication semantics so that
"prediction for the same tidal cycle" collapses correctly while
preserving day-by-day history.

Three levels of effort, ordered:

  - **Quick**: in the harmonic refresh path, delete existing rows for
    the prediction window before inserting the new batch. One-line
    change in `app/scheduler.py::daily_ukho_fetch` or in
    `store_harmonic_predictions`. Sacrifices intra-day history.
  - **Better**: add a cycle-number-since-epoch column to the
    `harmonic_predictions` schema (matching the pattern used by
    `access_calc.generate_event_uid`). Make the natural deduplication
    key `(cycle_number, event_type)` with `timestamp` as a data
    column. `latest_only=True` then groups by cycle. Preserves
    multi-day history of predictions for the same cycle.
  - **Best**: same as "better", plus round `generated_at` to the day
    in the dedup logic so within-day refreshes collapse but
    once-per-day predictions persist as distinct rows. Best matches
    the original design intent.

**Implementation note**: the dedup logic also exists implicitly in
`generate_langstone_harmonic_180d_feed` (90-min clash filter against
UKHO events). If the schema changes, the feed-generation deduplication
should be reviewed for consistency.

### 1. Flood-phase mid-cycle bias

**Current state**: pure cosine interpolation on the flood, with a
residual ~+0.13 m mean bias through the mid-flood region.

**What was tried**: adding a pre-HW stand symmetric to the existing
ebb stand. This was rolled back because it more than doubled flood
RMS error. The visible flatness near HW in the flood is largely
accounted for by the cosine's own slowdown near its peak; an explicit
linear stand on top double-counts that flatness AND compresses the
mid-flood cosine to be unrealistically steep.

**What would actually help**: a different curve shape for the flood
that has slower mid-rise than a cosine. Three candidate approaches:

  - **Cosine variant**: e.g. `cos^p` with `p < 1`, which flattens both
    extremes and steepens the mid-cycle. Could be evaluated by parameter
    sweep over `p`.
  - **Piecewise**: cosine to ~80% of HW, linear ramp through middle,
    slow approach near HW. More parameters, more risk of overfitting.
  - **Admiralty lookup table**: import the published Portsmouth tidal
    curve diagram from Admiralty Tide Tables NP 159 as a height-vs-
    hours-from-HW lookup. This is the industry standard approach and
    would supersede the function-based curve. Requires sourcing the
    table data.

**Direction of bias**: model predicts ~13cm higher than reality during
mid-flood. This is the **less-safe direction** for access-window
calculation - a boater on the flood seeing the model's prediction
might assume more water is available than there actually is. A
typical safety margin (30cm or more) absorbs this, but it is not
zero risk.

### 2. Harmonic mid-tide bias

**Current state**: production-path harmonic has a +0.22 m mean bias
in the 2.5-3.5 m height band, with RMS 0.32 m. This is the single
largest remaining residual in the harmonic prediction path.

**Why it happens**: the bias is dominated by the curve interpolation
step that the production path inherits from the access-window code.
Direct sampling of `predict_height_at_time(t)` (raw harmonic, no
interpolation) shows much smaller bias in this band.

**What would help**: Fix 1B from the option-1 analysis - use
`predict_height_at_time` directly for harmonic-derived access window
threshold-crossing detection, instead of going via
`predict_events -> apply_offset -> interpolate`. The interpolation
step throws away mid-cycle information the harmonic synthesis has
calibrated for.

**Implementation note**: would require modification to
`app/access_calc.py::_find_crossing` so it can call
`predict_height_at_time` directly when the source is harmonic, rather
than always using event-bracketed curve interpolation. Significant
refactor; not undertaken in the April 2026 calibration session.

### 3. Harmonic per-day drift across the corpus

**Current state**: production-path mean bias drifts from +0.049 m on
14 April to -0.110 m on 5 May - a coherent ~16 cm change across 22
days, with a step jump of ~+0.18 m at the boundary between the two
CSVs (22 Apr → 29 Apr).

**Possible causes**: phase or amplitude drift in one or more harmonic
constituents (likely M2), meteorological surge effects that the
harmonic model cannot capture, or both.

**What would help**: option 2 from the original analysis -
re-optimise the 19 harmonic constituents against the corpus. Requires
a numerical-optimisation harness (e.g. scipy.optimize) that can fit
amplitudes and phase lags simultaneously. Risk of overfitting to a
22-day window if not constrained.

**What does NOT help**: any single-week recalibration. The drift is
too small to disambiguate from noise on a single week.

### 4. Harmonic LW heights consistently low

**Current state**: in the very-high band (>=4.5m), the production-path
harmonic prediction is on average 0.11 m too low. Less critical than
the mid-tide bias because RMS is only 0.15 m here, but worth knowing
- the harmonic model's peaks aren't quite reaching real-world peaks.

**What would help**: same as item 3, harmonic constituent
recalibration. The peak-amplitude shortfall is consistent with the
M2 amplitude being slightly under-fit.

## Items NOT to address

These were considered and rejected during the session.

### Adding a pre-HW stand to the flood
Rolled back. See item 1 above.

### Increasing `stand_duration_minutes` beyond 70
The parameter sweep over 60-100 minutes found 70 (with fraction 0.96)
optimal. Going wider does not help.

### Removing the +9 minute HW timing offset
The timing offset is independently supported by both the original
April 2026 validation and the 16-day corpus. Only the height offset
was removed in v2.5.2.

## How to ensure model_config.json changes reach the running app

There is a known wrinkle in how `model_config.json` is loaded that has
caused confusion this session and will catch out future operators if
not documented.

### Where the config lives

There are two copies of `model_config.json` at runtime:

  - **Bundled default**: `/app/app/model_config.json` (inside the
    Docker image, copied during build from `app/model_config.json` in
    the repo).
  - **Operative copy**: `/app/data/model_config.json` (in the bind-
    mounted Docker volume `./data:/app/data`).

The bundled default is read **only** on first run when the operative
copy does not yet exist - `load_model_config()` copies it into the
volume. From that point onward, the operative copy is the source of
truth. The bundled default is ignored.

### Consequence: rebuilds do not pick up new defaults

When the repo's `app/model_config.json` is updated and the image is
rebuilt with `docker compose up -d --build`, the new bundled default
does not reach the running app. The operative copy in the volume still
contains the previous version.

### How to apply changes correctly

Two reliable methods:

**Option A: copy the new file directly into the volume.** This is
the surgical fix - updates the runtime config, no restart needed,
preserves any user customisations to other parts of the file (none
exist today, but the pattern is future-proof).

```
docker cp app/model_config.json tidal-access:/app/data/model_config.json
```

**Option B: delete the volume copy and let it re-populate.** Simpler
to remember; loses any user customisations to the file (currently
none).

```
docker exec tidal-access rm /app/data/model_config.json
docker restart tidal-access
```

Either works. The next call to `load_model_config()` will read the
updated values.

### Caching

`access_calc._get_curve_params()` caches the loaded config in module-
level state. Long-running processes (the FastAPI server, the
APScheduler thread) will continue to use the old values until either:

  - The cache is invalidated via `invalidate_model_config_cache()`,
    which is called automatically by `save_model_config()`.
  - The process is restarted.

For an out-of-band update via Option A or B, the safe approach is to
restart the container:

```
docker restart tidal-access
```

This is fast (< 5 seconds) and guarantees a fresh cache.

The calibration script `calibrate_from_ukho_week.py` runs as a
short-lived process so the cache is naturally fresh on each
invocation - no restart needed when running analysis.

### Long-term improvement

The volume-persistence of `model_config.json` was implemented
anticipating a UI for users to edit model parameters. That UI does
not exist and there is no plan for one. The cost of the current
arrangement is silent staleness on rebuild. A possible future
refactor: stop persisting the file, read the bundled default every
time. Single source of truth, no admin step needed for upgrades.

Not undertaken in this session. Worth considering when next touching
`app/config.py`.
