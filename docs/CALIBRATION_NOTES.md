# Calibration notes

State of the tidal model and harmonic prediction calibration as of
30 April 2026, post-v2.5.5 (model_config.json persistence removed).
Records what has been refined, what has not, and the tradeoffs that
remain open.

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

## Continuous monitoring (v2.5.4)

As of v2.5.4 the daily scheduler computes harmonic-vs-UKHO residuals
at HW/LW level and writes the result to `activity_log` with
`event_type="harmonic_residuals"`. Three rolling windows are reported
in the structured details JSON: 7d, 30d, and 90d. Each window contains
separate HW and LW stats with both height and timing residuals (count,
mean, RMS, max-abs).

The purpose is to detect drift in the harmonic model over time -
particularly the per-day drift documented in item 3 below. With
daily logging, items 3 and 4 are now observable from the activity log
rather than only by re-running the calibration corpus script.

### Sign convention

`residual = predicted - actual`. Positive = harmonic over-predicts.
Matches `scripts/calibrate_from_ukho_week.py` so the numbers are
directly comparable.

### Why HW/LW only

The production UKHO API endpoint `/Stations/{station}/TidalEvents`
returns only HW and LW points with their heights. There is no
half-hourly observation in the API. The 16-day half-hourly
calibration corpus came from the UKHO Easy Tide web portal, which is
a one-off manual download not available to the running container.

Consequence: continuous monitoring covers items 3 (per-day drift)
and 4 (HW peak undershoot) but cannot observe item 2 (mid-tide
bias), since item 2's symptom is between HW and LW points and the
harmonic model would need half-hourly comparison data to evaluate.

### Threshold logic

The scheduler raises the activity-log severity to `warning` if the
30-day window contains at least 20 matched events AND any of:

  - HW height mean bias |x| > 0.10 m
  - HW height RMS > 0.25 m
  - LW height mean bias |x| > 0.10 m
  - LW height RMS > 0.25 m

Thresholds are kept in `app/scheduler.py` as named constants
(`HEIGHT_MEAN_THRESHOLD`, `HEIGHT_RMS_THRESHOLD`,
`MIN_30D_MATCHES_FOR_WARNING`). They live there rather than in
`model_config.json` because they are operational ("when should the
operator look"), not model parameters ("what does the model
produce"). Move into config if a UI for tuning them is added.

The threshold values are calibrated to be loose enough to not warn
on v2.5.3's expected residuals (HW mean -0.12m would breach, but
in the 30-day window the bias is averaged across HWs and LWs and
is expected to come in below 0.10m absolute) and tight enough to
flag a real drift larger than ~0.10m mean. They are working
estimates, not data-derived; revisit after 30+ days of real
residuals have accumulated.

### What the residual log does NOT do

  - **Does not recalibrate.** Fitting a new set of harmonic
    constituents requires `scipy.optimize` machinery and a
    deliberate window choice (recommend 6+ months, M2/S2/N2
    parameters only). That is a separate workstream when residual
    history justifies it.
  - **Does not alert externally.** Warnings appear in the activity
    log only. No email/SMS/webhook integration. Add when it becomes
    necessary.
  - **Does not gate predictions.** Even if residuals breach the
    threshold, predictions and access-window calculations continue
    unchanged. The signal is for human-in-the-loop review.

### How to inspect

```
docker exec tidal-access sqlite3 /app/data/tides.db \
  "SELECT timestamp, severity, message FROM activity_log \
   WHERE event_type='harmonic_residuals' \
   ORDER BY timestamp DESC LIMIT 30;"
```

For the structured stats:

```
docker exec tidal-access sqlite3 /app/data/tides.db \
  "SELECT timestamp, details FROM activity_log \
   WHERE event_type='harmonic_residuals' \
   ORDER BY timestamp DESC LIMIT 1;"
```

The `details` column is JSON; pretty-print with `python -m json.tool`
or any JSON-aware tool.

## Calibration accuracy

### v2.5.3 (current, validated 30 April 2026)

| Path                           | Mean bias  | RMS error | Max error |
|--------------------------------|-----------:|----------:|----------:|
| UKHO curve, overall            | +0.010 m   | 0.190 m   | 0.66 m    |
| UKHO curve, flood phase        | +0.016 m   | 0.219 m   | 0.66 m    |
| UKHO curve, ebb phase          | +0.002 m   | 0.139 m   | 0.44 m    |
| Harmonic, raw                  | -0.068 m   | 0.218 m   | 0.60 m    |
| Harmonic, production path      | -0.036 m   | 0.207 m   | 0.58 m    |

Improvement vs v2.5.2 (which used pure-cosine flood):

  - UKHO curve overall: RMS 0.255m -> 0.190m (25% reduction); mean
    bias +0.121m -> +0.010m (effectively zeroed).
  - Harmonic production path: RMS 0.222m -> 0.207m (7% reduction);
    mean bias +0.024m -> -0.036m (smaller in magnitude but flipped
    sign).

The harmonic production path inherits the flood-side improvement
because it goes through `_curve_interpolate` for half-hourly sample
prediction. The residual mean shift and the per-day drift documented
in item 3 are harmonic-side issues that the curve change does not
reach.

### v2.5.2 (preserved for reference)

| Path                           | Mean bias  | RMS error | Max error |
|--------------------------------|-----------:|----------:|----------:|
| UKHO curve, overall            | +0.121 m   | 0.255 m   | 0.70 m    |
| UKHO curve, flood phase        | +0.134 m   | 0.290 m   | 0.70 m    |
| UKHO curve, ebb phase          | +0.002 m   | 0.139 m   | 0.60 m    |
| Harmonic, raw                  | -0.068 m   | 0.218 m   | 0.60 m    |
| Harmonic, production path      | +0.024 m   | 0.222 m   | 0.68 m    |

Under v2.5.2 the production path tracked raw harmonic output to within
4mm RMS across the full corpus.

## Items remaining to be addressed

### 0. Harmonic-prediction duplicate-rows on within-day refresh  **[RESOLVED v2.5.3]**

**Original symptom**: Tides tab "Forecast (next 180 days)" showed
duplicate HW/LW entries for the same tide cycle, times differing by
tens of seconds. `get_harmonic_predictions(latest_only=True)`
deduplicated on exact `(timestamp, event_type)` and could not
collapse near-duplicates from independent harmonic batches.

**Correction to original framing**: the notes attributed duplicates
to "multiple refreshes per day". In fact there is no intra-day
write path - `store_harmonic_predictions` is only called from the
daily cron in `scheduler.py`. The drift is between consecutive
*daily* runs, because `harmonic_predict_events` samples on a grid
anchored at "now" which advances 24h+seconds between runs and
produces slightly different `_refine`-output timestamps for the
same physical tide. After N days, each future tide had up to N
rows in `harmonic_predictions`.

**Resolution**: added a `cycle_number INTEGER` column to
`harmonic_predictions` (derived as `round(hours_since_2026-01-01
/ 12.4167)`, matching the existing pattern in
`access_calc.generate_event_uid` and
`ical_manager._tide_event_uid`). Changed the `latest_only`
correlated subquery to group by `(cycle_number, event_type)`
instead of `(timestamp, event_type)`. Same physical tide with
drifting timestamps now collapses to one row per cycle, with the
freshest `generated_at` winning. Migration backfills
`cycle_number` for existing rows and creates a `UNIQUE INDEX`
on `(cycle_number, event_type, generated_at)` for storage-time
enforcement. All changes contained in `app/database.py`. Multi-
day historical-prediction capability preserved as the design
intended - different daily runs have different `generated_at`
values and remain distinct rows under `latest_only=False`.

**Cleanup of pre-migration data**: not required. Old duplicate
rows are migrated (cycle_number backfilled) and `latest_only`
naturally returns one row per cycle. `cleanup_old_harmonic_
predictions(days=365)` washes them out over time.

**Out of scope, deferred**: the 90-min UKHO-vs-harmonic clash
filter in `generate_langstone_harmonic_180d_feed` and
`/api/tides?range=extended` is unrelated and unaffected. The
"Best" extension (rounding `generated_at` to day) was not
implemented because there is no intra-day write path to defend
against.

### 1. Flood-phase mid-cycle bias  **[RESOLVED v2.5.3]**

**Original state**: pure cosine flood interpolation with +0.134m
mean bias and 0.290m RMS through the mid-flood region. Direction
unsafe for boater (model overstated available water).

**Correction to original framing**: the notes proposed `cos^p` with
`p < 1` ("flattens extremes, steepens mid-cycle"). Working through
the math, `p < 1` *increases* mid-flood height - the wrong direction
for the observed +0.134m positive bias. The empirical sweep showed
`p > 1` is what helps; `p = 1.20` zeroes the mean.

**Resolution**: applied a "young flood stand" - linear rise during
the first 60 minutes after LW lifting the water by 8% of the flood
range, then a half-cosine from that level to HW. Symmetric in role
to the existing ebb stand near HW but uses fraction of *range*
rather than fraction of absolute height (because LW heights in
Langstone are small, 0.5-1.5m typical, and a fraction of LW would
be physically negligible).

Selected by `scripts/sweep_flood_curve.py` over forms (1)
`((1-cos(pi*f))/2)^p` swept on `p`, and (2) LW stand + cosine swept
on (duration, rise_fraction). Form (2) at `(60min, 0.08)` gives
flood mean +0.016m (statistically indistinguishable from zero with
~380 samples), RMS 0.219m, max 0.66m. Form (1) was numerically
close (`p=1.20` -> mean -0.001m, RMS 0.250m) but is curve-fitting;
form (2) has physical justification in the documented Solent young-
flood-stand effect (early-flood inflows around the Isle of Wight
arrive out of phase with the main flood and briefly pause the rise
just after LW).

Implementation in `app/access_calc.py::_curve_interpolate` flood
branch with two new keys in `model_config.json`:
`flood_lw_stand_minutes=60` and `flood_lw_stand_rise_fraction=0.08`.
Backward-compatible: if either key is zero or absent the flood
reverts to pure cosine (legacy v2.5.2 behaviour).

**Boater safety**: previously the model overstated mid-flood water
by ~13cm. The resolution essentially zeroes that bias. Net safety
improvement.

**Validation completed 30 April 2026**: post-deploy
`calibrate_from_ukho_week.py` rerun produced flood mean +0.016m,
RMS 0.219m, max 0.66m - matching the sweep prediction to three
decimal places. Combined-corpus rows in the accuracy table above
now reflect the live deployed configuration.

**Out of scope, deferred**: `compute_access_windows` still uses
the curve interpolation path for harmonic-derived windows. Item 2
below will replace that path for harmonic with direct
`predict_height_at_time` calls, at which point the flood curve
only affects UKHO/KHM windows.

### 2. Harmonic mid-tide bias  **[partially addressed by v2.5.3]**

**v2.5.2 state**: production-path harmonic had +0.22m mean bias in
the 2.5-3.5m height band, with RMS 0.32m - the single largest
remaining residual in the harmonic prediction path.

**v2.5.3 state**: the flood-stand fix (item 1) reduced the mid-band
bias substantially. The `_curve_interpolate` overshoot through mid-
flood was a major contributor. New numbers in the 2.5-3.5m band:
mean +0.073m (was +0.22m; 67% reduction), RMS 0.202m (was 0.32m;
37% reduction). Still material but priority of the proposed
refactor below is now lower.

**Why it happens**: the residual ~+0.07m mean is the part of the
bias that the flood-curve change does not reach - direct sampling
of `predict_height_at_time(t)` (raw harmonic, no interpolation)
shows that the harmonic synthesis itself has some residual bias in
this band, which the curve interpolation cannot correct.

**What would help**: use `predict_height_at_time` directly for
harmonic-derived access window threshold-crossing detection,
instead of going via `predict_events -> apply_offset -> interpolate`.
The interpolation step throws away mid-cycle information the
harmonic synthesis has calibrated for.

**Implementation note**: would require modification to
`app/access_calc.py::_find_crossing` so it can call
`predict_height_at_time` directly when the source is harmonic,
rather than always using event-bracketed curve interpolation.
Significant refactor; now sensible to weigh against item 3
(constituent recalibration) before committing - they address
overlapping but distinct symptoms.

### 3. Harmonic per-day drift across the corpus  **[monitored automatically v2.5.4]**

**v2.5.3 state**: production-path mean bias drifts from +0.002m on
14 April to -0.163m on 5 May - a coherent ~16cm change across 22
days, with a step jump of ~+0.18m at the boundary between the two
CSVs (22 April: -0.070m -> 29 April: +0.106m). Pattern essentially
identical to v2.5.2 (which had +0.049m -> -0.110m and step ~+0.18m);
the v2.5.3 flood fix shifted the absolute baseline by about -0.06m
but did not affect the day-to-day drift shape, confirming the drift
is harmonic-side and not curve-side.

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

### 4. Harmonic HW heights consistently low  **[monitored automatically v2.5.4]**

**v2.5.3 state**: in the very-high band (>=4.5m, n=121), the
production-path harmonic prediction is on average 0.12m too low,
with RMS 0.16m. Essentially unchanged from v2.5.2 (-0.11m / 0.15m).
Less critical than the per-day drift in item 3, but worth knowing -
the harmonic model's HW peaks aren't quite reaching real-world
peaks.

(Title corrected from "LW heights" - the band described is HW
peaks, not LW. The original wording was a documentation defect
predating v2.5.2.)

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

## How to update the model configuration

As of v2.5.5 the model configuration lives only in the bundled file
`app/model_config.json` in the repository. The previous arrangement
persisted an operative copy at `/app/data/model_config.json` in the
Docker volume; this caused silent staleness on image rebuilds because
the operative copy was preferred over the bundled default once it
existed, and the staleness was not visible to the operator until a
calibration test exposed it. The persistence has been removed.

To apply a change to the model configuration:

  1. Edit `app/model_config.json` in the repo.
  2. Rebuild and restart the container:
     ```
     docker compose up -d --build
     docker restart tidal-access
     ```

No `docker cp` step is needed. The bundled file is read at startup
and cached in `access_calc._get_curve_params` for the lifetime of
the process. Container restart is the deliberate refresh trigger.

If an old deployment has a `data/model_config.json` file left over
from pre-v2.5.5, it is now orphaned and ignored. Optionally delete
it for cleanliness; leaving it in place has no effect on behaviour.

The calibration scripts (`calibrate_from_ukho_week.py`,
`sweep_ebb_params.py`, `sweep_flood_curve.py`) run as short-lived
processes so the cache is naturally fresh on each invocation. The
sweep scripts mutate `_cached_curve_params` directly to inject test
parameters; this remains the supported pattern for ad-hoc analysis.
