# Tuning harmonic drift

How to act on the output of the continuous harmonic-residual monitoring
introduced in v2.5.4. This file is operator-facing: it describes what
to do, when, and why. Source documentation for the monitoring itself
is in `docs/CALIBRATION_NOTES.md` under "Continuous monitoring (v2.5.4)".

## Background

As of v2.5.4 the daily scheduler computes harmonic-vs-UKHO residuals at
HW/LW level and writes the result to `activity_log` with
`event_type="harmonic_residuals"`. Three rolling windows are reported in
the structured details JSON: 7d, 30d, and 90d. Each window contains
separate HW and LW stats with both height and timing residuals.

The monitoring detects drift in the harmonic model over time -
particularly the per-day drift documented as item 3 in
`CALIBRATION_NOTES.md`, and the HW-peak undershoot documented as item
4. With daily logging, those items are now observable from the activity
log rather than only by re-running the calibration corpus script.

The monitoring **does not** recalibrate. It tells the operator whether
recalibration is justified and provides the dataset on which to base
it.

## At day 30 (~30 May 2026)

Nothing automatic needs to happen. The system continues to run
unattended. What changes is that the 30-day window has accumulated
enough history (~58 HW + ~58 LW matches expected) for the threshold
check to become informative.

The action is operator-side.

### 1. Check the activity log

```powershell
docker exec tidal-access sqlite3 /app/data/tides.db "SELECT timestamp, severity, message FROM activity_log WHERE event_type='harmonic_residuals' ORDER BY timestamp DESC LIMIT 7;"
```

For the structured details:

```powershell
docker exec tidal-access sqlite3 /app/data/tides.db "SELECT timestamp, details FROM activity_log WHERE event_type='harmonic_residuals' ORDER BY timestamp DESC LIMIT 1;"
```

The `details` column is JSON; pretty-print with `python -m json.tool`
or any JSON-aware tool.

### 2. Decide based on the recent severity pattern

If severity has been **`info` consistently**: model is operating within
thresholds. No action needed. Schedule another check at day 90.

If severity has been **`warning` consistently**: there is a real drift
larger than 0.10m mean or 0.25m RMS. This is the signal to plan a
recalibration. The right next steps are:

- Decide whether the drift is bias (mean offset) or noise (RMS); the
  fix differs between the two.
- Bias suggests a constituent amplitude or phase is off, addressed by
  recalibrating M2/S2/N2 amplitudes and phases.
- High RMS without bias suggests weather (atmospheric pressure surge,
  wind setup) is contributing. The harmonic model cannot fix that, but
  the threshold may need loosening to avoid persistent false alarms.

If severity **flickers between info and warning**: the threshold is
set close to the natural variation. Consider loosening (e.g. 0.15m
mean) rather than treating each warning as actionable.

### 3. Important caveat - chronic warnings

Item 4 (HW peaks consistently low, ~-0.12m mean per the 16-day
calibration corpus) means the HW height mean threshold of 0.10m is
likely to be breached every day once enough HW samples accumulate. So
expect `warning` severity to become the default state at day 30, even
if the model is not drifting. **This is not a system fault**; it is
the threshold being tighter than item 4's residual.

Two reasonable responses at that point:

- **Loosen `HEIGHT_MEAN_THRESHOLD` to 0.15m in `app/scheduler.py`.**
  Removes chronic warning. Item 4 stays visible in the JSON details.
  Threshold becomes a "real change" detector rather than a "current
  state" detector.
- **Leave threshold at 0.10m.** Persistent warning serves as a daily
  reminder that item 4 is unresolved. Annoying but not harmful.

## At day 90 (~28 July 2026)

Same drill, longer window. The 90-day window has now stabilised,
providing a more robust baseline. The 90-day numbers in the
`details.window_90d` JSON become trustworthy.

### 1. Compare 30d vs 90d means

If they differ significantly (e.g. 30d says +0.05m, 90d says -0.05m),
there is a trend over the quarter - the model bias is moving. This is
the signature of seasonal effects (constituents like SA, SSA which
span months) or genuine constituent drift.

### 2. Decide whether to recalibrate

Recalibration becomes feasible at 90 days because:

- Sample size is statistically robust (~175 HW + ~175 LW points).
- Window covers enough astronomical variation (3 months) to fit M2
  amplitude/phase reliably.
- Still short enough that fitting will not pick up annual constituents
  (those need 6+ months).

Recalibration is **not automatic**; it is a deliberate workstream:

- Build a fitting harness using `scipy.optimize.least_squares`
  (separate session of work, roughly one day's effort).
- Run it against the accumulated 90-day data (read from
  `harmonic_predictions` and `tide_data`).
- Validate the new constants against held-out data (e.g. fit on first
  60 days, validate on last 30).
- Update the `HARMONICS` dict in `app/harmonic.py` (and the reference
  values in `model_config.json`).
- Bump version to v2.6.0. A constituent change is a more significant
  version bump than the v2.5.x curve work.
- Track residuals over the next 90 days to confirm the improvement is
  real.

### 3. If both windows look fine

No action. Continue monitoring. Revisit at day 180 or 365.

## Findings from the 21 May 2026 analysis session

The following was established using the 30-day logged data and the
half-hourly calibration corpus (now 25+ days spanning 14 April to
27 May). These findings should inform the day-30 and day-90 reviews.

### HW and LW residuals behave differently

`scripts/review_logged_residuals.py` (run against 30 days of logged
HW/LW pairs) showed that the "per-day oscillation" documented in
item 3 of CALIBRATION_NOTES.md is not a single phenomenon:

  - **HW residuals are consistently negative** (harmonic under-predicts
    HW heights). Range -0.12m to -0.35m across the 30-day window.
    The pattern is a steady negative trend, not an oscillation.
    This is item 4 (HW peak undershoot).

  - **LW residuals oscillate around zero**. Range -0.29m to +0.28m.
    The oscillation has a period of roughly 14-15 days, consistent
    with the spring-neap cycle.

  - **The combined per-day mean** (which earlier analysis reported as
    a ~15-day oscillation) is a mixture of these two signals. The
    apparent oscillation in the combined mean is partly driven by the
    day-to-day variation in HW vs LW event count per date (3 vs 4
    events), which changes the weight of the HW and LW contributions.

Implication: the recalibration harness should fit HW and LW residuals
separately rather than minimising a combined height residual. M2
amplitude (which dominates HW peak height) is likely the highest-
priority parameter.

### S2 is NOT the primary driver of the oscillation

`scripts/probe_s2_sensitivity.py` was run against the same 30-day
window. The probe perturbed S2 amplitude by +/-0.03m and phase_lag
by +/-6 degrees (49 combinations), re-running `predict_events ->
apply_offset` for each perturbation and computing both height and
timing residuals against UKHO actuals.

Result: **4% height oscillation reduction** at the best perturbation
(d_amp=-0.03m, d_phase=0). This is a weak signal. The height and
timing optima were at different S2 perturbations, confirming S2
alone is not the driver.

Possible explanations for why S2 perturbation did not help:

  1. Multiple constituents contribute to the spring-neap modulation
     (N2, K2, etc.) and S2 alone cannot compensate.
  2. The oscillation is partly meteorological (atmospheric pressure,
     wind setup correlating loosely with the spring-neap cycle).
  3. The apparent oscillation in the combined daily mean is partly
     an artefact of mixing HW and LW signals with different
     characteristics, as described above.

Implication: there is no shortcut via a single-constituent S2 fix.
The full M2+S2+N2 recalibration documented below remains the correct
approach.

### Timing residuals

The logged data showed HW mean +6.1min, LW mean +6.3min (harmonic
predicts ~6 minutes late on average). RMS ~15 minutes for both,
consistent with the documented Admiralty-convention offset calibration
(HW stdev 14.6min, LW stdev 19.5min from the 710-point validation).

One outlier: 40.4min timing error on 12 May HW. Worth investigating
if recurrent. Could be a data-quality issue on the UKHO side or a
genuine model failure for that specific tide.

### Tools available for the reviews

Four analysis scripts are now available, all analysis-only (no
production behaviour changes):

```powershell
# Per-day time series from logged HW/LW data (replaces manual
# activity_log queries for trend analysis)
docker exec tidal-access python -m scripts.review_logged_residuals
docker exec tidal-access python -m scripts.review_logged_residuals --days 90
docker exec tidal-access python -m scripts.review_logged_residuals --start 2026-04-30 --end 2026-07-28

# S2 sensitivity probe (or re-run with wider grid if needed)
docker exec tidal-access python -m scripts.probe_s2_sensitivity --days 90

# Half-hourly corpus analysis (requires manual CSV data addition)
docker exec tidal-access python -m scripts.calibrate_from_ukho_week

# Phase-position diagnostic (requires half-hourly corpus)
docker exec tidal-access python -m scripts.diagnose_residual_position
```

`review_logged_residuals` is the primary tool for the day-30 and
day-90 reviews because it works from automatically-gathered data.
The half-hourly scripts require manual addition of calibration CSVs
to `app/calibration_data/`.

### Revised recalibration priorities

Based on the 21 May 2026 findings, the priority order for the
eventual M2+S2+N2 recalibration is:

  1. **M2 amplitude** - the HW peak undershoot (item 4, -0.12 to
     -0.35m) is the largest single residual and is consistent with
     M2 amplitude being slightly too low.
  2. **M2 phase** - the +6min systematic timing bias may be partly
     M2 phase error (partly absorbed by the Admiralty convention
     offset, but the offset was calibrated against the current
     constituents and would need re-evaluation after a phase change).
  3. **S2 amplitude and phase** - contributes to the spring-neap
     modulation but not the dominant driver per the probe.
  4. **N2 amplitude and phase** - contributes to the longer-period
     modulation (~27.6 day beat with M2).

The fitting harness should weight HW height residuals more heavily
than LW (or fit them separately), because HW accuracy directly
affects access-window calculations for the boater use case.

## What "taking action" actually looks like

To be concrete: the most likely action that monitoring drives is a
decision to do item 3 work (constituent recalibration). The monitoring
itself does not recalibrate.

Two scenarios for comparison:

**Scenario A: monitoring shows stable residuals around v2.5.3's known
biases.**

Action at day 30: none. At day 90: none. The model is performing as
expected. The persistent residuals (item 4 HW undershoot, item 3
per-day drift) are documented limitations, not regressions.

**Scenario B: monitoring shows the 30-day mean drifting from -0.05m at
day 30 to +0.15m at day 90.**

Action at day 90: investigate. The drift means something has changed -
either a constituent is genuinely shifting (rare at this timescale,
but happens with the M2 nodal cycle), or there has been a sustained
meteorological pattern (winter storm season). At that point look at
the `details.window_7d` history over those 90 days to see whether the
change is gradual (constituent drift) or punctuated (storm event). The
right action depends on what that reveals.

## Caveats worth knowing

**The thresholds are working estimates, not data-derived.** Values of
0.10m / 0.25m were chosen by reasoning about expected v2.5.3
residuals, not by observing actual operational data. They might be
wrong. The day-30 review is partly about confirming or refining them.

**The chronic-warning question cannot be answered before day 30.**
Whether item 4's HW peak undershoot averages above or below 0.10m in
the rolling window is the empirical question that day 30 will
resolve. If above, expect chronic warnings; if below, the threshold is
well-set.

Both of these are reasons to not over-interpret day-30 output. Treat
it as a calibration of the calibration system itself, not as a
verdict on the model.

## Where the relevant constants live

| Constant | File | Line context |
|---|---|---|
| `MIN_30D_MATCHES_FOR_WARNING` | `app/scheduler.py` | `daily_ukho_fetch` residual block |
| `HEIGHT_MEAN_THRESHOLD` | `app/scheduler.py` | `daily_ukho_fetch` residual block |
| `HEIGHT_RMS_THRESHOLD` | `app/scheduler.py` | `daily_ukho_fetch` residual block |
| Activity-log retention (system scope) | `app/scheduler.py` | `prune_activity_log(system_days=30, ...)` call |

The activity-log retention currently rolls system-scope entries off at
30 days. That includes `harmonic_residuals` rows. If longer history is
wanted for trend analysis, either extend `system_days` or add a
special-case for the `harmonic_residuals` event type. Out of scope for
v2.5.4; flag for consideration when next touching the scheduler.
