"""
Diagnostic: residual position by phase fraction and source path.

Splits the harmonic-model residuals from the existing calibration corpus
(see scripts/calibrate_from_ukho_week.py) by physical position within
each tidal cycle, separately for the synthesis-only path (variant b) and
the production path (variant c). The third column, "curve contribution",
is the per-sample difference between the two and isolates what curve
interpolation adds on top of synthesis.

Why this exists
---------------
The height-bin breakdown in calibrate_from_ukho_week.py is misleading
for the Solent because the tide is markedly asymmetric: the same height
occurs once during the fast leg and once during the slow leg of each
cycle, and binning by absolute height conflates those two physically-
distinct positions. Phase-fraction bins respect the asymmetry and put
samples that are at the same point in the cycle into the same bin.

What it does NOT do
-------------------
Does not modify any model parameters, JSON, or production code. Pure
analysis.

Variant naming and interpretation
---------------------------------
The diagnostic uses the same variant labels as the existing calibration
script for cross-reference:

    variant b  = predict_height_at_time(t - 9min) + 0.0m
                 Direct synthesis evaluated at every sample timestamp,
                 with the secondary-port offset applied as a uniform
                 shift across the cycle (the post-v2.5.5 height offset
                 is 0.0m, so this is effectively a 9-min time shift
                 only). NO curve interpolation.

    variant c  = interpolate_height_at_time(t, langstone_events)
                 The deployed app's data path: predict_events ->
                 apply_offset (HW only, +9min/+0.0m) -> half-hourly
                 interpolation via the Langstone asymmetric tidal
                 curve. Same as variant c in the calibration script.

    curve      = (c - b) per sample.
                 The difference IS the curve-interpolation contribution
                 (assuming b and c apply the same secondary-port
                 correction, which they do post-v2.5.5). Positive curve
                 means the curve interpolation pushes the production
                 estimate higher than the direct-synthesis estimate at
                 that timestamp.

Phase fraction
--------------
For each sample, the script identifies the bracketing UKHO HW/LW events
from the calibration corpus and computes
    fraction = (sample_time - prior_event_time)
               / (next_event_time - prior_event_time)
which lies in [0, 1]. The phase is "flood" if prior is LowWater and
next is HighWater, "ebb" the other way round. Bracket-too-wide samples
(>13h between bracketing events, indicating a gap between disjoint
CSVs) are excluded, mirroring the existing scripts.

Reuse of existing helpers
-------------------------
Imports from sister script:
    parse_dataset, derive_extrema, merge_events, load_all_data,
    classify_phase, _bracket_too_wide, stats, fmt_stats,
    LONDON, SECONDARY_HW_MINUTES, SECONDARY_HW_HEIGHT_M, DATA_DIR

_bracket_too_wide is technically a private (underscore-prefixed) helper.
It is imported here as a deliberate cross-script coupling: duplicating
the function would create the kind of drift defect that the v2.5.5
audit caught in this same script. If the function ever needs to change
its signature, both scripts are in the same directory and a refactor
covers both at once.

Usage
-----
Same launch pattern as the calibration script:

    docker exec tidal-access python -m scripts.diagnose_residual_position
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.access_calc import interpolate_height_at_time
from app.harmonic import predict_height_at_time, predict_events
from app.secondary_port import apply_offset

# Sister-script helpers and constants. See module docstring for rationale
# on importing the underscore-prefixed _bracket_too_wide.
from scripts.calibrate_from_ukho_week import (  # noqa: F401
    load_all_data,
    classify_phase,
    _bracket_too_wide,
    stats,
    fmt_stats,
    LONDON,
    SECONDARY_HW_MINUTES,
    SECONDARY_HW_HEIGHT_M,
    DATA_DIR,
)


# Five fraction bins per phase: (lower_inclusive, upper_exclusive, label).
# Final bin is upper-inclusive so a sample with fraction exactly 1.0 (a
# sample landing on the next event - rare but possible) is captured.
FRACTION_BINS: list[tuple[float, float, str]] = [
    (0.0, 0.2, "0.0-0.2"),
    (0.2, 0.4, "0.2-0.4"),
    (0.4, 0.6, "0.4-0.6"),
    (0.6, 0.8, "0.6-0.8"),
    (0.8, 1.0001, "0.8-1.0"),
]


def find_bracket(target_dt: datetime,
                 events: list[dict]) -> tuple[Optional[dict], Optional[dict]]:
    """
    Return the (before, after) UKHO HW/LW events bracketing target_dt.
    Either may be None if target_dt is outside the event range.
    """
    before: Optional[dict] = None
    after: Optional[dict] = None
    for ev in events:
        ev_dt = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        if ev_dt <= target_dt:
            before = ev
        if ev_dt > target_dt and after is None:
            after = ev
            break
    return before, after


def phase_fraction(target_dt: datetime,
                   events: list[dict]) -> Optional[tuple[str, float]]:
    """
    Return (phase, fraction) for target_dt or None if not classifiable.
    fraction is in [0, 1.0]; a sample exactly at a boundary returns 0.0
    (sample equals 'before') or close to 1.0.

    'unknown' phase (events not LW->HW or HW->LW in order) returns None.
    """
    before, after = find_bracket(target_dt, events)
    if not before or not after:
        return None
    before_dt = datetime.fromisoformat(before["timestamp"].replace("Z", "+00:00"))
    after_dt = datetime.fromisoformat(after["timestamp"].replace("Z", "+00:00"))
    span = (after_dt - before_dt).total_seconds()
    if span <= 0:
        return None
    elapsed = (target_dt - before_dt).total_seconds()
    fraction = elapsed / span
    # Clamp defensively. Samples outside [0, 1] should already have been
    # filtered by find_bracket; this catches floating-point edge cases.
    fraction = max(0.0, min(1.0, fraction))

    if before["event_type"] == "LowWater" and after["event_type"] == "HighWater":
        return ("flood", fraction)
    if before["event_type"] == "HighWater" and after["event_type"] == "LowWater":
        return ("ebb", fraction)
    return None


def fraction_bin_label(fraction: float) -> Optional[str]:
    """Return the label of the bin containing this fraction, or None."""
    for lo, hi, label in FRACTION_BINS:
        if lo <= fraction < hi:
            return label
    return None


def main() -> int:
    print()
    print(f"Loading calibration data from: {DATA_DIR}")
    print()
    samples, events = load_all_data()
    if not samples:
        print("No data loaded. Exiting.")
        return 2
    print()

    print(f"Combined corpus: {len(samples)} half-hourly samples, "
          f"{len(events)} HW/LW events")
    print(f"  First sample: {samples[0]['dt_utc'].astimezone(LONDON).isoformat()}")
    print(f"  Last sample:  {samples[-1]['dt_utc'].astimezone(LONDON).isoformat()}")
    print()

    # Compute production-path Langstone events once for the whole span.
    # Same buffer pattern as run_harmonic_test in the sister script so
    # samples near the corpus edges still have bracketing events.
    span_start = samples[0]["dt_utc"] - timedelta(hours=12)
    span_end = samples[-1]["dt_utc"] + timedelta(hours=12)
    portsmouth_events = predict_events(span_start, span_end)
    langstone_events = apply_offset(portsmouth_events)

    # Per-sample residual triples (synthesis, production, curve), tagged
    # with phase and fraction-bin. Anything that cannot be classified is
    # counted in skipped_* and excluded.
    #
    # Storage is keyed (phase, fraction_bin_label) -> list of three
    # parallel lists (syn, prod, curve), so per-bin stats can be computed
    # independently for each variant.
    grid: dict[tuple[str, str], dict[str, list[float]]] = {}
    for phase in ("flood", "ebb"):
        for _, _, label in FRACTION_BINS:
            grid[(phase, label)] = {"syn": [], "prod": [], "curve": []}

    skipped_no_phase = 0
    skipped_wide_bracket = 0
    skipped_no_harmonic_bracket = 0
    skipped_unbinned = 0

    for s in samples:
        t = s["dt_utc"]
        h_actual = s["height_m"]

        # Filter same as existing scripts for consistency.
        if _bracket_too_wide(t, events):
            skipped_wide_bracket += 1
            continue

        pf = phase_fraction(t, events)
        if pf is None:
            skipped_no_phase += 1
            continue
        phase, fraction = pf
        label = fraction_bin_label(fraction)
        if label is None:
            skipped_unbinned += 1
            continue

        # Variant b: synthesis with uniform secondary-port offset.
        syn_pred = (
            predict_height_at_time(t - timedelta(minutes=SECONDARY_HW_MINUTES))
            + SECONDARY_HW_HEIGHT_M
        )
        syn_resid = syn_pred - h_actual

        # Variant c: production-path interpolation. May fail if the
        # harmonic event list does not bracket this sample.
        target_iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        prod_pred = interpolate_height_at_time(target_iso, langstone_events)
        if prod_pred is None:
            skipped_no_harmonic_bracket += 1
            continue
        prod_resid = prod_pred - h_actual

        # Curve contribution: production minus synthesis. Positive means
        # the interpolated curve sits above the direct-synthesis value at
        # this timestamp.
        curve_resid = prod_resid - syn_resid

        cell = grid[(phase, label)]
        cell["syn"].append(syn_resid)
        cell["prod"].append(prod_resid)
        cell["curve"].append(curve_resid)

    total_skipped = (
        skipped_wide_bracket
        + skipped_no_phase
        + skipped_unbinned
        + skipped_no_harmonic_bracket
    )
    used = len(samples) - total_skipped
    print(f"Used {used} of {len(samples)} samples; "
          f"skipped {total_skipped} "
          f"(wide_bracket={skipped_wide_bracket}, "
          f"no_phase={skipped_no_phase}, "
          f"unbinned={skipped_unbinned}, "
          f"no_harmonic_bracket={skipped_no_harmonic_bracket})")
    print()

    # ----------------------------------------------------------------------
    # Output: one section per variant (synthesis, production, curve), each
    # showing flood and ebb broken into 5 fraction bins. Each row prints
    # the standard fmt_stats summary.
    # ----------------------------------------------------------------------
    def print_section(title: str, var_key: str, sign_note: str) -> None:
        print("=" * 76)
        print(title)
        print(sign_note)
        print("=" * 76)
        for phase in ("flood", "ebb"):
            print(f"  {phase.upper()} (LW->HW)" if phase == "flood"
                  else f"  {phase.upper()} (HW->LW)")
            for _, _, label in FRACTION_BINS:
                vals = grid[(phase, label)][var_key]
                print(f"    fraction {label}: {fmt_stats(stats(vals))}")
            # Flood/ebb totals for the variant
            combined = []
            for _, _, label in FRACTION_BINS:
                combined.extend(grid[(phase, label)][var_key])
            print(f"    {phase} TOTAL : {fmt_stats(stats(combined))}")
            print()
        print()

    print_section(
        "VARIANT b: synthesis only (uniform secondary-port offset)",
        "syn",
        "Sign: positive = synthesis predicts higher than UKHO actual.",
    )
    print_section(
        "VARIANT c: production path (predict_events -> apply_offset -> interpolate)",
        "prod",
        "Sign: positive = production predicts higher than UKHO actual.",
    )
    print_section(
        "CURVE CONTRIBUTION: (production - synthesis), per sample",
        "curve",
        "Sign: positive = curve interpolation lifts production above raw synthesis.",
    )

    # ----------------------------------------------------------------------
    # Side-by-side summary table for quick comparison. Same structure but
    # showing all three variants in one row per (phase, bin), so the
    # reader can spot which variant carries the bias in each cell.
    # ----------------------------------------------------------------------
    print("=" * 76)
    print("SIDE-BY-SIDE: mean residual per (phase, fraction-bin)")
    print("Columns: synthesis | production | curve contribution")
    print("=" * 76)
    header = f"  {'phase':6s} {'bin':9s} {'n':>5s}   "
    header += f"{'syn mean':>10s} {'syn rms':>9s}   "
    header += f"{'prod mean':>10s} {'prod rms':>9s}   "
    header += f"{'curve mean':>10s} {'curve rms':>9s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for phase in ("flood", "ebb"):
        for _, _, label in FRACTION_BINS:
            cell = grid[(phase, label)]
            n = len(cell["syn"])
            if n == 0:
                continue
            syn_s = stats(cell["syn"])
            prod_s = stats(cell["prod"])
            curve_s = stats(cell["curve"])
            row = f"  {phase:6s} {label:9s} {n:>5d}   "
            row += f"{syn_s['mean']:+10.3f} {syn_s['rms']:>9.3f}   "
            row += f"{prod_s['mean']:+10.3f} {prod_s['rms']:>9.3f}   "
            row += f"{curve_s['mean']:+10.3f} {curve_s['rms']:>9.3f}"
            print(row)
    print()

    print("=" * 76)
    print("DONE. No model parameters were modified.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
