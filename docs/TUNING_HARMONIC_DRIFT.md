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
