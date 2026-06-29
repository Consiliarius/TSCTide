# Calibration notes

State of the tidal model and harmonic prediction calibration as of
1 May 2026, post-v2.5.7 (ebb stand re-tuned to 75/94 against the
18-day corpus, replacing v2.5.3's 70/96; sweep-script defect that
selected 70/96 was found and fixed).
Records what has been refined, what has not, and the tradeoffs that
remain open.

## Calibration corpus

Located at `app/calibration_data/`. Currently contains 18 days of
half-hourly UKHO data for Langstone Harbour (station 0066), spanning
14 April – 22 April, 29 April – 5 May, and 6 May – 7 May 2026.
Total 864 samples covering a full neaps-springs-neaps progression
and the start of the next cycle.

Re-running the headline analysis:

```
docker exec tidal-access python -m scripts.calibrate_from_ukho_week
```

Phase-position diagnostic (added 1 May 2026, see item 2 below):

```
docker exec tidal-access python -m scripts.diagnose_residual_position
```

Optional parameter sweeps:

```
docker exec tidal-access python -m scripts.sweep_ebb_params
docker exec tidal-access python -m scripts.sweep_flood_curve
```

Adding new days is a matter of dropping a new CSV into
`app/calibration_data/` and re-running. The scripts auto-discover
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
and 4 (HW peak undershoot) but cannot observe items 2a (mid-flood
synthesis residual) or 2b (late-ebb synthesis residual) directly,
since those symptoms are between HW and LW points and the harmonic
model would need half-hourly comparison data to evaluate. The
phase-position diagnostic in `scripts/diagnose_residual_position.py`
is the offline tool for those; new half-hourly data must be supplied
manually to evaluate them.

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
the curve interpolation path for harmonic-derived windows. The
original note here said this would be replaced via item 2 with
direct `predict_height_at_time` calls; that plan was abandoned on
1 May 2026 after the phase-position diagnostic showed the curve is
NET POSITIVE for accuracy (it corrects synthesis-side error). See
item 2 below.

### 2. Harmonic mid-tide bias  **[reframed 1 May 2026; see below]**

**v2.5.2 state**: production-path harmonic had +0.22m mean bias in
the 2.5-3.5m height band, with RMS 0.32m - reported as the single
largest remaining residual in the harmonic prediction path.

**v2.5.3 state**: the flood-stand fix (item 1) reduced the mid-band
bias substantially. The `_curve_interpolate` overshoot through mid-
flood was a major contributor. New numbers in the 2.5-3.5m band:
mean +0.073m (was +0.22m; 67% reduction), RMS 0.202m (was 0.32m;
37% reduction).

**v2.5.6 reframing (1 May 2026)**: the 16-day height-bin breakdown
was misleading because the Solent's marked tidal asymmetry maps the
same absolute height to two physically distinct positions on the
cycle (one fast leg, one slow leg). After the corpus expanded to
18 days and the phase-position diagnostic
(`scripts/diagnose_residual_position.py`) was run, three things
became clear:

  1. **The original symptom (mid-band production residual) is
     small.** Re-binned by phase position rather than absolute
     height, the largest production-path mid-flood residual is in
     fraction 0.2-0.4 of the flood at mean -0.283m, NOT a positive
     bias. The +0.073m height-bin number averaged opposing flood
     and ebb residuals.

  2. **The originally-proposed fix (`_find_crossing` refactor to
     bypass curve interpolation) would make accuracy WORSE.** The
     diagnostic's variant b (synthesis only, no curve) returns RMS
     0.279m vs variant c (production path) RMS 0.205m - the curve
     is providing a 27% improvement, not adding error. Refactoring
     it out would introduce a -0.370m mid-flood bias that the curve
     currently corrects.

  3. **The largest remaining production residuals are at specific
     phase positions, not specific heights.** From the 18-day
     diagnostic:

     | Phase           | Bin        | Production mean | Synthesis mean |
     |-----------------|-----------:|----------------:|---------------:|
     | flood           | 0.2-0.4    | -0.283m         | -0.370m        |
     | ebb             | 0.0-0.2    | -0.156m         | -0.134m        |
     | flood           | 0.8-1.0    | -0.110m         | -0.315m        |
     | (others)        |            | <0.1m           |                |

     The first row is the largest single residual in the corpus.
     The curve corrects ~25% of the underlying synthesis error
     here; the rest is synthesis-side and is item 3 territory.

**Status**: item 2 in its original framing (mid-tide bias from
curve interpolation) is essentially closed. The diagnostic shows
the curve is the single largest contributor to overall accuracy
(RMS reduction 0.279m -> 0.205m). What remains are two distinct
residuals worth tracking separately:

  - **2a. Mid-flood synthesis residual** (flood 0.2-0.4):
    -0.283m production mean, -0.370m synthesis mean. The curve
    corrects only ~25% of this; the underlying synthesis under-
    predicts this position by 37cm. Likely overlaps with item 3
    (constituent recalibration) and item 4 (HW peak undershoot).

  - **2b. Just-after-HW residual** (ebb 0.0-0.2): -0.156m
    production mean. The curve makes this slightly worse here
    (synthesis -0.134m, curve contribution -0.022m), suggesting
    the v2.5.3 ebb stand parameters may have a re-tunable point
    closer to the trough side. Re-running `sweep_ebb_params.py`
    against the 18-day corpus on 1 May 2026 (after fixing a
    silent defect in the sweep) shifted the ebb stand from 70/96
    to 75/94; see the v2.5.7 section below. The diagnostic was
    not re-run at the new parameters, so the specific impact on
    item 2b is not quantified - but the headline ebb numbers
    improved (|mean| 0.013 -> 0.006m, RMS 0.137 -> 0.129m) and
    the parameter shift extends the stand by 5 minutes which
    should reduce the just-after-HW under-prediction in direction
    if not fully in magnitude.

**What does NOT help**: the original `_find_crossing` refactor,
rejected on the basis of the diagnostic. Documented in "Items NOT
to address" below.

**Boater-safety direction of remaining residuals**: the production
flood 0.2-0.4 residual of -0.283m means the model UNDER-predicts
mid-flood heights. The access window calculator would say "not yet
accessible" when actually it is - boater inconvenience, but the
opposite direction is what would matter for safety. Same for the
just-after-HW residual of -0.156m: model says "start of inaccessible"
slightly earlier than reality. Conservative direction in both cases.

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

**v2.5.6 phase-position view (1 May 2026)**: the height-bin finding
is confirmed and localised. The HW peak undershoot lives in the
late-flood region, fraction 0.8-1.0: production mean -0.110m, but
the underlying synthesis is -0.315m at this position. The curve
corrects ~65% of the synthesis shortfall, but the synthesis itself
is the source. This strongly supports M2 amplitude as a candidate
for under-fit, since the late-flood approach to HW is dominated
by the M2 cosine peak.

**What would help**: same as item 3, harmonic constituent
recalibration. The peak-amplitude shortfall is consistent with the
M2 amplitude being slightly under-fit. Item 2a (mid-flood under-
prediction) and item 4 may be the same defect viewed at different
phase positions; a single M2 amplitude correction could plausibly
address both.

## Items NOT to address

These were considered and rejected.

### Adding a pre-HW stand to the flood
Rolled back. See item 1 above.

### Increasing `stand_duration_minutes` beyond 75
The parameter sweep over 60-100 minutes (run 1 May 2026 against the
18-day corpus) found 75 (with fraction 0.94) optimal. Going wider
does not help; the 90 and 100 minute cells materially worsen ebb
residuals.

Note: the original v2.5.3 selection (70/96) was made by a sweep
that had a silent defect (it disabled the v2.5.3 flood stand during
the sweep run). The corrected sweep on the 18-day corpus selected
75/94. See the v2.5.7 section below for details.

### Removing the +9 minute HW timing offset
The timing offset is independently supported by both the original
April 2026 validation and the 16-day corpus. Only the height offset
was removed in v2.5.2.

### Refactoring `_find_crossing` to use `predict_height_at_time` directly
Proposed by the original item 2 framing. Rejected 1 May 2026 after
the phase-position diagnostic showed that the curve interpolation
is a 27% RMS improvement over direct synthesis (production RMS
0.205m vs synthesis-only RMS 0.279m across the 18-day corpus). The
curve actively corrects synthesis-side bias rather than adding
error on top. Bypassing it would introduce a -0.370m mid-flood
bias and the late-flood HW undershoot would worsen by ~0.20m.
Boater-safety direction would also flip negative in places where
it is currently neutral. Documented here so future readers do not
revive the proposal.

## Hardcoded constants migration (v2.5.6)

A targeted migration of the model's tunable physical constants from
hardcoded Python values into `model_config.json`, completing the path
started by v2.5.5. The audit done during v2.5.5 noted that several
entries in `model_config.json` were nominally documented but not
actually consulted at runtime, while the live values lived in .py
modules. v2.5.6 closes that gap for the values that are genuinely
tunable per-deployment.

### Scope

Migrated to JSON, now read at runtime via accessors in `app/config.py`:

  - `harmonic_reference.mean_level_m` (Z0 in `harmonic.py`)
  - `harmonic_reference.constituents` (HARMONICS in `harmonic.py`)
  - `secondary_port_offset.*` (4 offsets in `secondary_port.py`,
    plus the `+9 min` literal previously inline in `khm_parser.py`)
  - `cycle_number.epoch_iso` and `cycle_number.avg_cycle_hours`
    (the cycle constants previously triplicated across
    `access_calc.py`, `ical_manager.py`, and `database.py`, plus a
    fourth copy inside the `init_db` migration backfill)

Left hardcoded by deliberate decision:

  - `SPEEDS` and `DOODSON` in `harmonic.py` - constituent angular
    speeds and Doodson multipliers are physical constants of the
    harmonic-prediction algorithm itself, not tuning parameters.
    Editability is risk, not feature.
  - The nodal-correction coefficients in `harmonic._nodal` - same
    character: physical constants of the algorithm.
  - `HW_ADMIRALTY_OFFSET_MINUTES` and `LW_ADMIRALTY_OFFSET_MINUTES`
    in `harmonic.py` - data-derived corrections from a 710-point
    validation set; alteration without revalidation invalidates the
    accuracy claims.

### Failure mode (lenient)

If any migrated key is missing or malformed in `model_config.json`,
the corresponding .py module-level constant is used as a fallback
and a single INFO log line is emitted per (process, json_path) on
first use. Subsequent fallbacks for the same path during the same
process are silent. This preserves backward compatibility - an
empty or absent `model_config.json` still produces correct
predictions - at the cost that drift between JSON and .py defaults
can go unnoticed unless the operator inspects logs. This is
deliberate; the alternative (fail-fast) was rejected because the
bundled JSON is part of the image and a missing key during local
development should not break the whole app.

The `harmonic_reference.constituents` accessor is all-or-nothing:
if any one of the 19 constituents has a malformed amplitude or
phase_lag, the entire dict reverts to the .py default rather than
mixing JSON values with .py values. Harmonic synthesis combines
all 19 in a sum, so a partial mix would produce subtly wrong
predictions that pass silent validation.

### Treatment of .py defaults

Module-level constants are kept as readable documentation and as
the fallback target. Each file's top-of-section comment block was
updated to make this explicit:

> Reference defaults. The values actually used at runtime come from
> model_config.json (loaded via app.config.get_X). These constants
> are kept here as readable documentation and as a fallback if the
> JSON is missing or malformed. To change the model behaviour, edit
> the JSON; do not edit these.

The one exception is the cycle-number constants. Their .py defaults
live in `app/config.py` (not duplicated across the three consuming
modules) so the deduplication is genuine. `harmonic.py` and
`secondary_port.py` keep their own .py defaults because each is the
natural home for its own values.

### Caching and refresh

Each accessor caches its resolved value (or the fallback) in
`app.config._resolved_cache` for the lifetime of the process.
Hot-path callers (`predict_height_at_time` is invoked tens of
thousands of times per scheduled run) bind the accessor return to
a local variable once per outer call to avoid repeated dict access
in the inner loop. Container restart is the deliberate refresh
trigger; there is no in-process invalidation path. Same model as
`access_calc._get_curve_params`.

### Cycle-number constants - critical safety note

The `cycle_number.epoch_iso` and `cycle_number.avg_cycle_hours`
values ARE the dedup key for stored rows in
`harmonic_predictions.cycle_number` AND form part of every iCal
event UID issued. The bundled values are bit-for-bit identical to
the pre-migration hardcoded values (epoch 2026-01-01T00:00:00Z,
12.4167 hours). They MUST NOT be changed without a database
migration plan covering the full data lifecycle; doing so would
invalidate every existing UID (calendar apps see delete-and-re-add)
and break the dedup index in `harmonic_predictions`.

A matching warning is included in the JSON file alongside those
keys.

### Validation

Numeric behaviour must be unchanged. Post-deploy validation runs
`scripts/calibrate_from_ukho_week.py` and confirms residual statistics
match v2.5.5 to three decimal places. If any number drifts, the
migration changed math behaviour and must be rolled back.

The sweep scripts (`scripts/sweep_flood_curve.py`,
`scripts/sweep_ebb_params.py`) continue to work unchanged because
they mutate `access_calc._cached_curve_params` directly; the new
accessors do not affect them.

## Ebb stand re-tuning and sweep-script fix (v2.5.7)

The v2.5.3 ebb stand parameters of 70min / 96% were re-tuned to
75min / 94% against the expanded 18-day corpus on 1 May 2026.
During re-tuning, a silent defect in `scripts/sweep_ebb_params.py`
was discovered and fixed. The defect did not affect production
behaviour but did affect the parameter selection process.

### The defect

`sweep_ebb_params.py::run_sweep_iteration` overwrote
`access_calc._cached_curve_params` with a new dict containing only
the two ebb keys being swept:

```python
access_calc._cached_curve_params = {
    "stand_duration_minutes": stand_minutes,
    "stand_height_fraction": stand_fraction,
}
```

This silently dropped the `flood_lw_stand_minutes` and
`flood_lw_stand_rise_fraction` keys that drive the v2.5.3 flood
stand. The flood branch of `_curve_interpolate` looked up these
keys via `.get(..., 0)`, found them missing, and reverted to pure
cosine flood interpolation. Consequence: every cell of the v2.5.3
ebb sweep was evaluated against a hypothetical pure-cosine-flood +
proposed-ebb-parameters configuration, which is NOT the
configuration actually deployed in v2.5.3 (which has the LW stand
active on the flood).

The v2.5.3 selection of 70/96 happened to be close to the corrected
optimum (75/94), so the defect did not produce a catastrophically
wrong selection. But it could have, and the reproducibility of the
original selection was compromised.

### The fix

`run_sweep_iteration` now reads the bundled curve params first and
overlays only the swept keys:

```python
base = dict(access_calc._get_curve_params())
base["stand_duration_minutes"] = stand_minutes
base["stand_height_fraction"] = stand_fraction
access_calc._cached_curve_params = base
```

This preserves whatever flood-stand keys are present in the
bundled JSON, so the sweep evaluates ebb parameters against the
actual deployed flood configuration. `sweep_flood_curve.py` was
inspected and found to be free of the symmetric defect: its ebb
branch reads `stand_duration_minutes` and `stand_height_fraction`
via `_get_curve_params()` directly, picking up the bundled values.

### The new selection

With the corrected sweep run on the 18-day corpus, the optimum
shifted from 70/96 to 75/94:

| Metric            | v2.5.3 (70/96) | v2.5.7 (75/94) | Delta    |
|-------------------|---------------:|---------------:|---------:|
| ebb mean bias     | +0.013 m       | -0.006 m       | -0.019 m |
| ebb RMS           | 0.137 m        | 0.129 m        | -0.008 m |
| all-corpus RMS    | 0.187 m        | 0.184 m        | -0.003 m |

Three "best" criteria (smallest |mean bias|, smallest RMS, combined
heuristic) all converged on 75/94. The improvement is modest (~6%
ebb RMS reduction; ebb mean shift roughly 3sigma at corpus size)
and multiple neighbouring cells (70/95, 75/95) are statistically
indistinguishable from 75/94 within sampling noise. 75/94 was
selected as the unambiguous "best" by all three criteria.

Direction of mean shift (ebb mean +0.013 -> -0.006) is
safety-positive: a small ebb under-prediction means the model
reports "no longer accessible" slightly earlier than reality,
which is the conservative direction for the ebb phase.

### Flood stand re-evaluated, retained

The corresponding flood-stand sweep (`sweep_flood_curve.py`,
unaffected by the defect) was also re-run against the 18-day
corpus on 1 May 2026. The v2.5.3 selection of 60min / 8% remained
close to optimal:

| Cell               | flood mean | flood RMS | flood max |
|--------------------|-----------:|----------:|----------:|
| 60/0.08 (deployed) | +0.026     | 0.215     | 0.66      |
| 60/0.10 (best RMS) | +0.063     | 0.208     | 0.58      |
| 50/0.05 (best |m|) | +0.013     | 0.234     | 0.71      |

No dominant winner. 60/0.08 sits at a balance point; the
alternatives offer specific tradeoffs (better mean OR better RMS,
but not both) and the magnitudes are small. v2.5.3 selection
retained.

### Diagnostic was not re-run

The phase-position diagnostic (`scripts/diagnose_residual_position.py`)
was not re-run at 75/94. The expected shift in item 2b
(just-after-HW residual, currently -0.156m at 70/96) is
directionally favourable but not quantified. Re-running the
diagnostic at 75/94 would close that gap and is a useful next
analytical step, but does not block the parameter change.

### Wider impact

  - **Existing harmonic_predictions table**: unaffected. Stored
    rows are harmonic synthesis output, not curve-interpolated.
  - **Existing calendar_events**: events for past tides are
    unaffected. Future events will use the new parameters on next
    regeneration.
  - **Continuous monitoring (v2.5.4)**: thresholds in
    `app/scheduler.py` monitor harmonic-vs-UKHO HW/LW only. Not
    affected by the curve-parameter change.
  - **Calendar event UIDs**: depend only on the cycle constants,
    not curve parameters. Unaffected.

### Reproducibility note for future readers

Anyone re-deriving the v2.5.3 ebb parameters from a fresh
`sweep_ebb_params.py` run will get 75/94 as the answer, not the
70/96 actually deployed at v2.5.3. This is because the v2.5.3
selection was made with the broken sweep and is not reproducible
without that defect. The current deployed parameters (75/94 from
v2.5.7) ARE reproducible from the corrected sweep.

## Barometric coefficient k (v2.9)

The v2.9 barometric correction shifts predicted tide heights for the
deviation of forecast pressure from a reference,
`correction_m = clamp((P_ref − P) × k × scale, ±0.30 m)`, with
`P_ref = 1013.25 hPa`, `k = 0.0100 m/hPa`, `scale = 1.0`. This section
records the provenance of `k` and why `scale_factor` has no tuning
source. The full design rationale is in
`docs/V2.9_BAROMETRIC_DESIGN.md` (§6).

### k is validated offline, not from mooring observations

Mooring observations are **not** a tuning source for `k`: a single user
records too few to accumulate a usable pressure-stratified dataset, and
— unlike the tidal secondary-port offset, which captures genuine *local*
astronomical-tide propagation — the inverse-barometer effect is
**regional** (pressure systems span ~1000 km; UKHO notes water level
responds to average pressure "over a considerable area"). So the
Portsmouth-derived coefficient is the correct Langstone coefficient to
better than our precision, and there are **no observation-pipeline or
schema changes in v2.9** for this purpose.

### The regression method

`scripts/validate_barometric_k.py` fits

```
(measured_sea_level − harmonic_prediction) = intercept + k · (P_ref − P)
```

The **slope is the empirical `k`**. Crucially, datum offset (Portsmouth
Chart Datum ↔ the gauge's mAOD reference) and any constant instrument
bias fall into the **intercept**, so `k` is identifiable without precise
datum alignment. Measured water level comes from the UK National Tide
Gauge Network at Portsmouth (station `E71839`, mAOD), pulled
programmatically — the Defra/EA flood-monitoring **archive**
(`--source ea-archive`, daily all-station dumps that include the
Portsmouth 15-minute tidal rows) gives multi-year history with no manual
download; the BODC quality-controlled archive (`--source csv`) is an
optional gold-standard alternative. Historical pressure and wind come
from the Open-Meteo ERA5 archive (free, no key), interpolated to each
reading. The fit is pure-Python OLS (no numpy).

### Result and decision (15 June 2026)

A 12-month fit (2025-06-01 .. 2026-05-31; 32,938 matched readings;
pressure span 969.7–1034.2 hPa, i.e. 64.5 hPa — deep lows to strong
highs) gave:

| Sample            | k (m/hPa) | SE       | intercept | R²   |
|-------------------|----------:|---------:|----------:|-----:|
| All points        | +0.01047  | 0.00014  | −2.59 m   | 0.14 |
| Wind ≤ 8 m/s      | +0.00999  | 0.00015  | −2.59 m   | 0.14 |

The tight standard error over a wide pressure range is a robust fit. The
wind filter lowering `k` is the expected surge-reduction direction
(deep lows bring wind setup that co-varies with low pressure), and the
filtered **k ≈ 0.0100** coincides with the textbook inverse-barometer
1 cm/hPa — about 13% above the old UKHO-rounded prior 0.00882.

**Decision: ship k = 0.0100.** A second year was judged not worth it:
the fit is systematic-limited (the all-points-vs-wind-filtered spread
~0.0005 exceeds the SE several-fold), `k` is a stable physical constant
not expected to vary year to year, the pressure range is already
near-maximal, and the 13% gap from the prior is below the 5-minute
display rounding while the deployed error budget is dominated by
forecast-pressure error and the harmonic/curve model (~0.2 m RMS — note
R² was only 0.14). The chosen follow-up is the production
**self-consistency check** (does the corrected residual still slope with
pressure, once live?), not more offline fitting.

### scale_factor stays 1.0

`barometric.scale_factor` is a manual trim multiplier with **no
empirical tuning source** — the effect is regional (Portsmouth ≈
Langstone), so there is nothing local to fit it against. It is kept only
as a documented override and stays `1.0`. `k` itself is physics and does
not drift, so its validation is a one-off offline fit (this script),
**not** an ongoing automated loop; there is no `sea_level_observations`
table or daily gauge fetch.

## Echo-sounder depth-sounding calibration (v2.10)

v2.10 adds a second calibration input alongside the existing afloat/aground
observations: a **depth sounding**. An afloat/aground observation is a
one-sided inequality on drying height and is only tight near the grounding
transition; a sounding is a two-sided point estimate that can be taken at
any state of tide. Both inputs coexist and cross-check each other. Removing
all soundings reverts `calibrate_drying_height` to its pre-v2.10 result
bit-for-bit — soundings are purely additive.

### Raw storage, query-time derivation

A sounding stores only the **raw measured depth**, the datum it was read to,
and (optionally) a per-reading transducer-offset override. It never stores a
pre-computed drying height. Derivation happens at calibration time:

```
water_depth = measured_depth + offset_to_waterline(datum, transducer_offset, draught)
drying_CD   = interpolate_height_at_time(t) − water_depth
```

`offset_to_waterline` is: `0` for a waterline datum; the transducer offset
for a transducer datum; the boat's draught for a keel datum (the keel sits
`draught` below the waterline — this avoids needing a separate
keel-to-transducer geometry the tool does not hold). The helpers live in
`app/access_calc.py` (`sounder_water_depth`, `sounding_sigma`); the estimator
extension is in `app/database.py::calibrate_drying_height`.

This mirrors the Portsmouth→Langstone query-time offset (Option B): soundings
re-derive automatically if the harmonic model is recalibrated, with no stored
value going stale. Stored soundings **must stay raw** — a later height-model
change would otherwise leave historical soundings inconsistent with new ones.

### Pressure (deliberately blind for v2.10.0) — follow-up flagged

`interpolate_height_at_time` is **pressure-blind**: it applies no barometric
(v2.9) correction. The v2.9 correction is applied to event heights in the
feed write-path and the conditions display, not in the interpolation the
calibration uses, and `app/barometric.py` deliberately keeps the calibration
corpus pressure-blind. v2.10.0 follows that convention — soundings derive
through the pressure-blind path, matching afloat/aground.

The consequence: a sounding taken under an extreme inverse-barometer anomaly
carries that anomaly into its derived drying height. Unlike a one-sided
bound, a sounding measures real water depth at a real pressure, so there is a
defensible argument for correcting the sounding instant for pressure when
deriving `drying_CD`. **This is a documented follow-up, not yet built.** It
was deferred because the offset and soft-mud uncertainties (below) dominate
at typical anomaly magnitudes, and because shipping it pressure-blind keeps
v2.10.0 consistent with the rest of the calibration. If revisited, apply the
v2.9 `correction_for_pressure` at the sounding timestamp inside the
derivation, and re-derive historical soundings (they are raw, so this is
safe).

### Estimator and the aground-floor safety interlock

When soundings exist, the central estimate is the inverse-variance weighted
mean of the per-sounding `drying_CD`, with a standard error reported as the
spread. It is then reconciled against the hard bounds:

  - **Aground floor (non-negotiable).** An aground observation is a hard
    physical fact: the keel touched, so the bed is at least `height −
    draught` there. The sounding estimate is never allowed below the aground
    lower bound. This is the primary guard against the soft-mud failure mode
    (next), where an over-read makes the bed appear deeper than it is and
    would bias the estimate toward more access than is safe — the unsafe
    direction.
  - **Conflict, not silent averaging.** If the sounding cluster sits more
    than ~2 standard errors below the aground floor (or materially above an
    afloat ceiling), the result is flagged `sounding-bound-conflict` and the
    conservative (higher-drying / shallower-water) interpretation is taken,
    rather than averaging the sounding and the bound. The new confidence
    state renders in both the UI (`confDesc`/`confSymbols`) and the feed
    (`ical_manager.conf_labels`), distinct from the existing `inconsistent`
    (which means the afloat/aground bounds themselves cross).

### Soft mud (Langstone-specific)

Echo sounders over fluid mud can return from the mud surface or penetrate it,
reading to a layer the keel does not rest on. The over-read biases derived
drying **low**, which is the unsafe direction. Handling:

  - A per-mooring `bed_type` (`hard` / `soft` / `unknown`) inflates the
    sounding σ over soft or unknown bed (×2 in `sounding_sigma`), so those
    soundings carry less weight.
  - The aground floor above is the hard guard; σ inflation is only a
    soft down-weight.

### Spatial variance on a swing mooring

A sounding samples the bed under the transducer at the boat's current lay,
within the swing circle, on a bed the wind-offset feature already treats as
sloping. The controlling keel grounding point may be elsewhere.
`direction_of_lay` is captured with every sounding. Lay-correlated scatter is
the same across-channel slope the wind offset models; v2.10 surfaces it as
low confidence / wide spread rather than averaging it away. Using
sounding-vs-lay data to populate the wind-offset shallow-side parameter
empirically is noted as a future linkage, **not built** in v2.10.

### Validation strategy (empirical, before relying on sounding-only data)

Ordered by effort-to-value:

  1. **Bound cross-check (free, primary).** Every sounding-derived estimate
     is checked against the afloat upper bounds and aground lower bounds
     already collected on the mooring. Soundings that respect the bounds
     corroborate the method; persistent conflicts indicate offset error,
     mud, or spatial mismatch. The `calibration_update` activity-log detail
     records the sounding count, the per-sounding derived `drying_CD`, and
     whether the floor or conflict flag fired, so the method can be audited
     against the bounds after the fact.
  2. **Multi-state self-consistency (free).** Soundings taken at different
     states of tide should derive the same drying height; scatter beyond σ
     flags an offset or height-model problem, not the bed.
  3. **Transition-timestamp correlation (deferred).** The grounding/float
     transition timestamp — the most direct anchor — is **not** in the v2.10
     schema; it is a later branch. Until then, validation is against the
     afloat/aground bounds only.

A sounding-only calibration should not be relied on until the estimator has
been checked against collected bound data on a real mooring (validation 1).

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

## Future calibration: planning and data acquisition

This section is intended as a reference when returning to calibration
work after several months. The goal is to accumulate enough half-hourly
data across different seasons and tidal conditions that any future
parameter changes can be validated against the full corpus rather than
tuned to a single week.

### Data acquisition options

The half-hourly depth-of-tide readings used for calibration come from
UKHO Admiralty predictions. Two acquisition paths exist:

**Manual (current approach)**: copy and paste from Admiralty EasyTide
(https://easytide.admiralty.co.uk). EasyTide is free and provides the
current day plus 6 days of half-hourly heights. To build a multi-month
corpus, a new week must be captured every 7 days. If a week is missed,
that window is lost -- EasyTide does not provide historical data. The
data must be pasted into a Claude chat session and saved to a CSV in
`app/calibration_data/` following the format documented in
`app/calibration_data/README.md`.

**Automated via UKHO API upgrade**: the current TSCTide deployment
uses the UKHO Tidal API **Discovery** tier (free), which provides
HW/LW events only. Half-hourly height data is NOT available on
Discovery. Two paid tiers offer it:

  - **Foundation** (GBP 120/year): current + 13 days of tidal heights
    at configurable intervals (e.g. 30 minutes). Would allow the
    daily scheduler to fetch and store 13 days of half-hourly data
    automatically, building the calibration corpus without manual
    intervention. Endpoint: `GET /Stations/{id}/TidalHeights` with
    `intervalInMinutes=30`. Rate limit: 20 calls/sec, 20,000/month.
  - **Premium** (pricing not public): up to 1 year of historical and
    future data at 1-minute resolution. Would let the system
    backfill and build the entire corpus in one shot.

If upgrading to Foundation, the implementation would be:
  1. Add a new function in `app/ukho.py` to call the TidalHeights
     endpoint with `intervalInMinutes=30`.
  2. Add a new database table `calibration_heights` (or extend
     `tide_data` with a `resolution` column) to store the half-hourly
     samples separately from HW/LW events.
  3. Add a daily scheduler job to fetch and store the latest 13 days
     of half-hourly data. Overlap with previously-stored data is
     handled by UPSERT on `(timestamp, station, source)` key.
  4. Update the calibration script to read from both
     `app/calibration_data/` CSVs (manual data) and the database
     table (automated data), merging them into a single corpus.
  5. Note: UKHO terms require that stored data is not made available
     after 72 hours. The calibration use case (internal model
     refinement, not redistribution) likely falls within acceptable
     use, but the terms should be reviewed before implementation.

**Recommendation**: if calibration work is going to be ongoing (which
it should be, given items 2a-4 above), the Foundation tier pays for
itself quickly in saved manual effort. A single year at GBP 120 would
produce a corpus of ~17,500 half-hourly samples (365 days x 48/day)
covering all seasonal and tidal conditions. That corpus would be
sufficient for robust harmonic-constituent recalibration (item 3) and
would reveal whether the remaining residuals vary seasonally.

### Calibration cadence

Whether data is acquired manually or automatically, the recommended
calibration review cadence is:

  - **Monthly (first 3 months)**: run the calibration script against
    the growing corpus. Watch for the per-day drift (item 3) to see
    whether it is consistent or seasonal. Check the harmonic-residual
    monitoring in the activity log (v2.5.4). Do NOT change parameters
    based on a single month's data.
  - **Quarterly (after 3 months)**: with ~90 days of data, the corpus
    is large enough to support parameter sweeps. Compare new-corpus
    residuals against the v2.5.7 baseline before applying any
    changes.
  - **Annually**: with a full year of data, the corpus covers all
    seasonal variation. This is the appropriate point to attempt
    harmonic-constituent recalibration (item 3) with confidence that
    the result generalises.

### What to do when returning to calibration

1. **Add any new half-hourly data** to `app/calibration_data/` as a
   new CSV following the naming convention
   `langstone_ukho_YYYY-MM-DD_to_YYYY-MM-DD.csv`.
2. **Run the analysis scripts** against the full corpus:
   ```
   docker exec tidal-access python -m scripts.calibrate_from_ukho_week
   docker exec tidal-access python -m scripts.diagnose_residual_position
   ```
3. **Compare the new residuals** against the v2.5.3 accuracy table
   and the v2.5.7 ebb-stand numbers in this document. If the numbers
   are similar, the model is stable. If they have drifted, investigate
   which phase/height-band is responsible.
4. **Check the harmonic-residual monitoring** for drift trends:
   ```
   docker exec tidal-access sqlite3 /app/data/tides.db \
     "SELECT timestamp, severity, message FROM activity_log \
      WHERE event_type='harmonic_residuals' \
      ORDER BY timestamp DESC LIMIT 30;"
   ```
5. **Do NOT change parameters** based on a single new week. The
   April 2026 session demonstrated that single-week tuning can
   optimise away from the broader truth.
6. **If parameter changes are warranted**, use the sweep scripts:
   ```
   docker exec tidal-access python -m scripts.sweep_ebb_params
   docker exec tidal-access python -m scripts.sweep_flood_curve
   ```
   Evaluate the sweep results against the FULL corpus (all CSVs),
   not just the newest week.
7. **After any parameter change**, rebuild and restart:
   ```
   docker compose up -d --build
   ```
   As of v2.5.5, `model_config.json` is read from the bundled image.
   Rebuilds pick up changes automatically with no manual copy step.

### Harmonic constituent recalibration (when the corpus justifies it)

When item 3 is pursued (recommended: after 6+ months of data), the
approach should be:

  1. Use `scipy.optimize.least_squares` to fit M2, S2, and N2
     amplitudes and phase lags against the half-hourly corpus.
     These three constituents dominate Langstone's tidal signal;
     fitting all 19 simultaneously risks overfitting on the smaller
     constituents.
  2. Split the corpus: fit on the first 60% of days, validate on
     the remaining 40%. If the validation residuals are materially
     worse than the fit residuals, the model is overfitting.
  3. Compare the new constituent values against the current values
     in `model_config.json::harmonic_reference.constituents`.
     Changes > 5% in M2 amplitude or > 10 degrees in M2 phase
     warrant investigation rather than blind application.
  4. After applying new constituents, re-run both the calibration
     script and the sweep scripts to confirm the curve parameters
     are still optimal. Constituent changes shift the harmonic
     baseline, which may change the optimal ebb/flood stand values.
  5. Bump the version in `model_config.json` and document the
     change in this file.
