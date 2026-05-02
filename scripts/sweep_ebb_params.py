"""
Parameter sweep over the ebb-stand parameters.

Runs the curve test from calibrate_from_ukho_week.py against multiple
combinations of (stand_duration_minutes, stand_height_fraction), reporting
the ebb residuals for each. The flood is pure cosine and unaffected by
these parameters, so flood RMS is constant across the sweep and not
re-printed.

Goal: find the combination that minimises ebb mean bias (closest to zero)
while keeping ebb RMS low, ideally near or below the 0.195m achieved at
90/0.95.

Usage:
    docker exec tidal-access python -m scripts.sweep_ebb_params

Override defaults via the SWEEP_* env vars if needed - they accept
comma-separated lists of values:
    SWEEP_DURATIONS=60,70,80,90,100
    SWEEP_FRACTIONS=0.94,0.95,0.96,0.97
"""

from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytz

# Reuse the loader and the residual-bookkeeping helpers from the main
# calibration script. This keeps the sweep behaviour consistent with the
# headline analysis and avoids duplicating data parsing.
from scripts.calibrate_from_ukho_week import (
    DATA_DIR, LONDON,
    load_all_data, classify_phase, _bracket_too_wide,
    stats, fmt_stats,
)

from app import access_calc
from app.access_calc import interpolate_height_at_time


def parse_list(env_value: str | None, default: list) -> list:
    """Parse a comma-separated SWEEP_ env override, falling back to default."""
    if not env_value:
        return default
    out = []
    for s in env_value.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(float(s) if "." in s else int(s))
        except ValueError:
            print(f"WARNING: ignoring unparseable sweep value '{s}'")
    return out or default


def run_sweep_iteration(
    samples: list[dict],
    events: list[dict],
    stand_minutes: int,
    stand_fraction: float,
) -> dict:
    """
    Run one residual-analysis pass with the given parameters.
    Returns flood/ebb/all stats dicts.

    Overrides _cached_curve_params directly rather than rewriting
    model_config.json - faster, no I/O, no risk of leaving the file in
    a half-edited state if the script is interrupted.

    Preserves the flood-stand keys from the bundled config so the flood
    branch behaves the same as production while the ebb keys are
    swept. Earlier versions of this script overwrote the entire dict
    with only the ebb keys, which silently disabled the v2.5.3 flood
    stand and meant the "best ebb" reported was the best ebb against a
    pure-cosine flood that does not match production. The fix is to
    read the bundled config once and overlay only the keys being
    swept.
    """
    # Read the bundled curve params once. _get_curve_params() caches on
    # first call; we deliberately call it before overwriting the cache
    # so the overwrite picks up the bundled flood-stand values rather
    # than {} from a stale cache state.
    base = dict(access_calc._get_curve_params())
    base["stand_duration_minutes"] = stand_minutes
    base["stand_height_fraction"] = stand_fraction
    access_calc._cached_curve_params = base

    flood: list[float] = []
    ebb: list[float] = []
    all_resid: list[float] = []

    event_dts = {
        datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        for ev in events
    }

    for s in samples:
        if any(abs((s["dt_utc"] - et).total_seconds()) < 180 for et in event_dts):
            continue
        if _bracket_too_wide(s["dt_utc"], events):
            continue
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, events)
        if predicted is None:
            continue
        residual = predicted - s["height_m"]
        all_resid.append(residual)
        phase = classify_phase(s["dt_utc"], events)
        if phase == "flood":
            flood.append(residual)
        elif phase == "ebb":
            ebb.append(residual)

    return {
        "flood": stats(flood),
        "ebb": stats(ebb),
        "all": stats(all_resid),
    }


def main() -> int:
    durations = parse_list(os.environ.get("SWEEP_DURATIONS"),
                           [60, 70, 75, 80, 85, 90, 100])
    fractions = parse_list(os.environ.get("SWEEP_FRACTIONS"),
                           [0.94, 0.95, 0.96, 0.97])

    print(f"\nLoading calibration data from: {DATA_DIR}\n")
    samples, events = load_all_data()
    if not samples:
        print("No data loaded. Exiting.")
        return 2
    print(f"\nCorpus: {len(samples)} samples, {len(events)} events")
    print(f"Sweeping {len(durations)} durations x {len(fractions)} fractions "
          f"= {len(durations) * len(fractions)} combinations\n")

    print("=" * 88)
    print(f"{'duration':>10} {'fraction':>10}  "
          f"{'ebb_mean':>10} {'ebb_RMS':>10} {'ebb_max':>10}  "
          f"{'all_mean':>10} {'all_RMS':>10}")
    print("=" * 88)

    # Track the best by (a) absolute mean bias, (b) RMS, so we can report
    # both at the end - sometimes these point at different cells.
    results: list[tuple] = []
    for d in durations:
        for f in fractions:
            r = run_sweep_iteration(samples, events, d, f)
            results.append((d, f, r))
            print(
                f"{d:>10} {f:>10.2f}  "
                f"{r['ebb']['mean']:>+10.3f} {r['ebb']['rms']:>10.3f} {r['ebb']['max_abs']:>10.2f}  "
                f"{r['all']['mean']:>+10.3f} {r['all']['rms']:>10.3f}"
            )

    print("=" * 88)
    print()

    # Best by absolute mean bias on ebb.
    best_by_bias = min(results, key=lambda x: abs(x[2]["ebb"]["mean"]))
    # Best by RMS on ebb.
    best_by_rms = min(results, key=lambda x: x[2]["ebb"]["rms"])
    # Best by combined: equal weighting of normalised bias and RMS.
    # Quick heuristic - lets the script flag a likely "best overall".
    max_bias = max(abs(r[2]["ebb"]["mean"]) for r in results) or 1
    max_rms = max(r[2]["ebb"]["rms"] for r in results) or 1
    best_combined = min(
        results,
        key=lambda x: abs(x[2]["ebb"]["mean"]) / max_bias
                       + x[2]["ebb"]["rms"] / max_rms,
    )

    def _fmt(label, row):
        d, f, r = row
        return (
            f"  {label:30s} duration={d}, fraction={f:.2f}  ->  "
            f"ebb mean={r['ebb']['mean']:+.3f}m, "
            f"RMS={r['ebb']['rms']:.3f}m, "
            f"all RMS={r['all']['rms']:.3f}m"
        )

    print("SUMMARY:")
    print(_fmt("Smallest ebb |mean bias|:", best_by_bias))
    print(_fmt("Smallest ebb RMS:", best_by_rms))
    print(_fmt("Best combined (heuristic):", best_combined))
    print()
    print("Pick by inspection of the table above - the heuristic 'combined'")
    print("is just a starting point.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
