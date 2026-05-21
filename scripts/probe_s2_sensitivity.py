"""
S2 sensitivity probe: test whether small perturbations to the S2
constituent reduce the per-day oscillation observed in harmonic
residuals.

Reads paired (harmonic, UKHO) HW/LW events from the database, then
for each S2 perturbation:
  1. Re-runs predict_events over the full window to find perturbed
     HW/LW times and heights.
  2. Matches perturbed predictions to UKHO events by cycle_number.
  3. Computes both height and timing residuals per pair.
  4. Reports the oscillation metric (stdev of per-day means) for
     both height and timing.

This evaluates the FULL effect of an S2 change - both the amplitude
contribution to peak height AND the phase contribution to peak timing.
Earlier versions of this script evaluated height-at-UKHO-time only,
which partially captured S2 phase effects through their height
projection but could not distinguish amplitude error from timing
artefact. The current version separates them.

This is NOT a full recalibration. It tests one hypothesis:
  "Is S2 mis-calibration the primary cause of the ~15-day oscillation
   in per-day residuals documented in CALIBRATION_NOTES.md item 3?"

If the answer is yes (i.e. a small perturbation materially reduces the
oscillation amplitude), that confirms S2 as the priority target for
the full recalibration harness at the day-90 review.

Usage:

    docker exec tidal-access python -m scripts.probe_s2_sensitivity
    docker exec tidal-access python -m scripts.probe_s2_sensitivity --days 60
    docker exec tidal-access python -m scripts.probe_s2_sensitivity --start 2026-04-14 --end 2026-05-28

The --amp-range and --phase-range arguments control the perturbation
grid (defaults: amplitude +/-0.03m in 0.01 steps, phase +/-6 degrees
in 2-degree steps).
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import app.config as config
from app.config import to_utc_str, compute_cycle_number
from app.database import get_harmonic_residual_pairs
from app.harmonic import predict_events, HARMONICS
from app.secondary_port import apply_offset

LONDON = ZoneInfo("Europe/London")

# Cache key used by app.config for the harmonics dict
_HARMONICS_CACHE_KEY = "harmonic_reference.constituents"


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _frange(start: float, stop: float, step: float) -> list[float]:
    """Inclusive float range, avoiding float-precision boundary issues."""
    vals = []
    v = start
    while v <= stop + step * 0.01:
        vals.append(round(v, 6))
        v += step
    return vals


def _override_s2(amp: float, phase: float):
    """
    Override the cached harmonics with a modified S2 entry.
    Returns the previous cached value so it can be restored.
    """
    current = config.get_harmonics(HARMONICS)
    modified = dict(current)
    modified["S2"] = (amp, phase)
    old = config._resolved_cache.get(_HARMONICS_CACHE_KEY)
    config._resolved_cache[_HARMONICS_CACHE_KEY] = modified
    return old


def _restore_harmonics(old_value) -> None:
    """Restore the harmonics cache to its previous state."""
    if old_value is not None:
        config._resolved_cache[_HARMONICS_CACHE_KEY] = old_value
    else:
        config._resolved_cache.pop(_HARMONICS_CACHE_KEY, None)


def _predict_and_match(
    start_dt: datetime,
    end_dt: datetime,
    ukho_index: dict[tuple[int, str], dict],
) -> list[dict]:
    """
    Run predict_events -> apply_offset for the window, then match
    against the UKHO index by (cycle_number, event_type).

    Returns a list of matched pairs with height and timing residuals.
    """
    portsmouth_events = predict_events(start_dt, end_dt)
    langstone_events = apply_offset(portsmouth_events)

    pairs = []
    for h in langstone_events:
        h_ts = h["timestamp"]
        h_cyc = compute_cycle_number(h_ts)
        et = h["event_type"]
        u = ukho_index.get((h_cyc, et))
        if u is None:
            continue
        try:
            u_dt = datetime.fromisoformat(
                u["ukho_timestamp"].replace("Z", "+00:00")
            )
            h_dt = datetime.fromisoformat(
                h_ts.replace("Z", "+00:00")
            )
        except (ValueError, KeyError):
            continue
        pairs.append({
            "cycle_number": h_cyc,
            "event_type": et,
            "ukho_timestamp": u["ukho_timestamp"],
            "ukho_height_m": u["ukho_height_m"],
            "harmonic_timestamp": h_ts,
            "harmonic_height_m": h["height_m"],
            "height_residual_m": round(h["height_m"] - u["ukho_height_m"], 3),
            "timing_residual_min": round(
                (h_dt - u_dt).total_seconds() / 60.0, 1
            ),
        })
    return pairs


def _daily_means(
    pairs: list[dict], field: str
) -> dict[str, float]:
    """Per-BST-date mean of the named field."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for p in pairs:
        dt = datetime.fromisoformat(
            p["ukho_timestamp"].replace("Z", "+00:00")
        ).astimezone(LONDON)
        by_day[dt.strftime("%Y-%m-%d")].append(p[field])
    return {d: sum(v) / len(v) for d, v in by_day.items() if v}


def _oscillation(daily_means: dict[str, float]) -> float:
    """Stdev of per-day means. Lower = less oscillation."""
    vals = list(daily_means.values())
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / n)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S2 sensitivity probe for harmonic residual oscillation."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=30)
    group.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument(
        "--amp-range", type=float, default=0.03,
        help="Amplitude perturbation range +/- (default 0.03m)"
    )
    parser.add_argument(
        "--amp-step", type=float, default=0.01,
        help="Amplitude perturbation step (default 0.01m)"
    )
    parser.add_argument(
        "--phase-range", type=float, default=6.0,
        help="Phase perturbation range +/- (default 6 degrees)"
    )
    parser.add_argument(
        "--phase-step", type=float, default=2.0,
        help="Phase perturbation step (default 2 degrees)"
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--start requires --end")
    if args.end and not args.start:
        parser.error("--end requires --start")

    if args.start:
        start_dt = _parse_date(args.start)
        end_dt = _parse_date(args.end) + timedelta(days=1)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=args.days)

    start_str = to_utc_str(start_dt)
    end_str = to_utc_str(end_dt)

    print()
    print(f"S2 sensitivity probe: {start_str} to {end_str}")

    # Get UKHO reference events
    ref_pairs = get_harmonic_residual_pairs(start_str, end_str)
    print(f"  Matched reference pairs: {len(ref_pairs)}")
    if len(ref_pairs) < 4:
        print("  Too few pairs for meaningful analysis.")
        return 1

    # Build UKHO index for matching against perturbed predictions
    ukho_index: dict[tuple[int, str], dict] = {}
    for p in ref_pairs:
        ukho_index[(p["cycle_number"], p["event_type"])] = p

    # Current S2 values
    current_harmonics = config.get_harmonics(HARMONICS)
    base_amp, base_phase = current_harmonics["S2"]
    print(f"  Current S2: amplitude={base_amp:.3f}m, phase_lag={base_phase:.1f} deg")
    print()

    # Baseline from stored predictions
    baseline_h_daily = _daily_means(ref_pairs, "height_residual_m")
    baseline_t_daily = _daily_means(ref_pairs, "timing_residual_min")
    baseline_h_osc = _oscillation(baseline_h_daily)
    baseline_t_osc = _oscillation(baseline_t_daily)
    baseline_h_mean = sum(p["height_residual_m"] for p in ref_pairs) / len(ref_pairs)
    baseline_t_mean = sum(p["timing_residual_min"] for p in ref_pairs) / len(ref_pairs)

    print(f"  Baseline (stored predictions):")
    print(f"    Height: mean={baseline_h_mean:+.3f}m  oscillation={baseline_h_osc:.3f}m")
    print(f"    Timing: mean={baseline_t_mean:+.1f}min  oscillation={baseline_t_osc:.1f}min")
    print(f"    Days with data: {len(baseline_h_daily)}")
    print()

    # Build perturbation grid
    amp_deltas = _frange(-args.amp_range, args.amp_range, args.amp_step)
    phase_deltas = _frange(-args.phase_range, args.phase_range, args.phase_step)
    n_combos = len(amp_deltas) * len(phase_deltas)

    print(f"  Sweep grid: {len(amp_deltas)} amp x {len(phase_deltas)} phase "
          f"= {n_combos} combinations")
    print(f"  Each runs predict_events over the full window; may take a few minutes.")
    print()

    results: list[dict] = []
    done = 0

    for d_amp in amp_deltas:
        for d_phase in phase_deltas:
            test_amp = base_amp + d_amp
            test_phase = base_phase + d_phase

            old = _override_s2(test_amp, test_phase)
            perturbed = _predict_and_match(start_dt, end_dt, ukho_index)
            _restore_harmonics(old)

            if not perturbed:
                done += 1
                continue

            h_daily = _daily_means(perturbed, "height_residual_m")
            t_daily = _daily_means(perturbed, "timing_residual_min")
            h_osc = _oscillation(h_daily)
            t_osc = _oscillation(t_daily)
            h_mean = sum(p["height_residual_m"] for p in perturbed) / len(perturbed)
            t_mean = sum(p["timing_residual_min"] for p in perturbed) / len(perturbed)

            results.append({
                "d_amp": d_amp,
                "d_phase": d_phase,
                "amp": test_amp,
                "phase": test_phase,
                "h_mean": h_mean,
                "h_osc": h_osc,
                "t_mean": t_mean,
                "t_osc": t_osc,
                "matched": len(perturbed),
            })
            done += 1
            if done % 10 == 0:
                print(f"    ... {done}/{n_combos} complete")

    if not results:
        print("  No perturbations produced matched pairs.")
        return 1

    print()

    # Print results table sorted by height oscillation
    print("=" * 110)
    print("S2 PERTURBATION RESULTS (sorted by height oscillation, lower = better)")
    print("=" * 110)
    header = (
        f"  {'d_amp':>7s}  {'d_phs':>6s}  "
        f"{'amp':>7s}  {'phase':>7s}  "
        f"{'h_mean':>7s}  {'h_osc':>6s}  {'dh_osc':>7s}  "
        f"{'t_mean':>7s}  {'t_osc':>6s}  {'dt_osc':>7s}  "
        f"{'n':>4s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    best_h = min(results, key=lambda x: x["h_osc"])

    for r in sorted(results, key=lambda x: x["h_osc"]):
        dh = r["h_osc"] - baseline_h_osc
        dt = r["t_osc"] - baseline_t_osc
        marker = " <-- BEST" if r is best_h else ""
        print(
            f"  {r['d_amp']:>+7.3f}  {r['d_phase']:>+6.1f}  "
            f"{r['amp']:>7.3f}  {r['phase']:>7.1f}  "
            f"{r['h_mean']:>+7.3f}  {r['h_osc']:>6.3f}  {dh:>+7.3f}  "
            f"{r['t_mean']:>+7.1f}  {r['t_osc']:>6.1f}  {dt:>+7.1f}  "
            f"{r['matched']:>4d}{marker}"
        )

    print()

    # Summary
    best_t = min(results, key=lambda x: x["t_osc"])

    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print(f"  Baseline height oscillation:  {baseline_h_osc:.3f}m")
    print(f"  Best height oscillation:      {best_h['h_osc']:.3f}m "
          f"(delta {best_h['h_osc'] - baseline_h_osc:+.3f}m)")
    print(f"    S2 perturbation:  d_amp={best_h['d_amp']:+.3f}m, "
          f"d_phase={best_h['d_phase']:+.1f} deg")
    print(f"    S2 values:        amp={best_h['amp']:.3f}m, "
          f"phase={best_h['phase']:.1f} deg")
    print(f"    Height mean:      {best_h['h_mean']:+.3f}m "
          f"(baseline: {baseline_h_mean:+.3f}m)")
    print(f"    Timing mean:      {best_h['t_mean']:+.1f}min "
          f"(baseline: {baseline_t_mean:+.1f}min)")
    print()
    print(f"  Baseline timing oscillation:  {baseline_t_osc:.1f}min")
    print(f"  Best timing oscillation:      {best_t['t_osc']:.1f}min "
          f"(delta {best_t['t_osc'] - baseline_t_osc:+.1f}min)")
    print(f"    S2 perturbation:  d_amp={best_t['d_amp']:+.003f}m, "
          f"d_phase={best_t['d_phase']:+.1f} deg")
    print()

    if best_h is best_t:
        print("  Height and timing optima COINCIDE at the same S2 perturbation.")
        print("  Strong evidence that S2 is the dominant driver of both.")
    else:
        print("  Height and timing optima are at DIFFERENT S2 perturbations.")
        print("  S2 affects both but may not be the sole driver; other")
        print("  constituents or non-tidal forcing likely contribute.")
    print()

    h_reduction = (
        (baseline_h_osc - best_h["h_osc"]) / baseline_h_osc * 100
        if baseline_h_osc > 0 else 0
    )
    if h_reduction > 20:
        print(f"  STRONG SIGNAL: {h_reduction:.0f}% height oscillation reduction.")
        print("  S2 mis-calibration is likely a significant contributor to")
        print("  the per-day drift (item 3). Consider prioritising S2 in")
        print("  the full recalibration harness.")
    elif h_reduction > 5:
        print(f"  MODERATE SIGNAL: {h_reduction:.0f}% height oscillation reduction.")
        print("  S2 contributes to the drift but is not the sole cause.")
        print("  Full recalibration (M2 + S2 + N2) is likely needed.")
    else:
        print(f"  WEAK SIGNAL: {h_reduction:.0f}% height oscillation reduction.")
        print("  S2 perturbation alone does not explain the per-day drift.")
        print("  The oscillation may be driven by other constituents or")
        print("  non-tidal forcing (meteorological).")

    print()
    print("=" * 110)
    print("DONE. No model parameters were modified.")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
