"""
Per-day harmonic residual time series from logged database data.

Reads paired (harmonic prediction, UKHO actual) HW/LW events from the
database and reports per-day residual statistics in the same format as
the per-day breakdown in scripts/calibrate_from_ukho_week.py - but
from automatically-logged data rather than the manual half-hourly
calibration corpus.

This bridges the gap between:
  - The activity_log summary stats (7d/30d/90d aggregates, no per-day)
  - The calibrate_from_ukho_week.py per-day breakdown (manual corpus only)

The output is designed to be directly comparable to the per-day
production-path breakdown in calibrate_from_ukho_week.py output. Sign
convention: positive = harmonic over-predicts (same as everywhere else
in this project).

Limitations:
  - Only HW/LW events, not half-hourly. Cannot show mid-cycle residuals.
  - Only as good as the data in the database. If UKHO fetch failed for a
    day, that day has no pairs.
  - The harmonic predictions stored are Langstone-corrected at write time
    (secondary_port.apply_offset applied in scheduler.daily_ukho_fetch).
    The UKHO events are Langstone-corrected at read time (via
    get_ukho_tide_events). Both are correct; flagging because a future
    reader might wonder why there is no offset step in this script.

Usage:

    docker exec tidal-access python -m scripts.review_logged_residuals
    docker exec tidal-access python -m scripts.review_logged_residuals --days 90
    docker exec tidal-access python -m scripts.review_logged_residuals --start 2026-04-30 --end 2026-05-21
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import to_utc_str
from app.database import get_harmonic_residual_pairs


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD to a timezone-aware UTC midnight datetime."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _stats(vals: list[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "rms": None, "max_abs": None}
    mean = sum(vals) / n
    rms = math.sqrt(sum(v * v for v in vals) / n)
    max_abs = max(abs(v) for v in vals)
    return {"n": n, "mean": mean, "rms": rms, "max_abs": max_abs}


def _fmt(s: dict) -> str:
    if s["n"] == 0:
        return "n=  0  (no data)"
    return (
        f"n={s['n']:>3d}  "
        f"mean={s['mean']:+.3f}m  "
        f"RMS={s['rms']:.3f}m  "
        f"max|err|={s['max_abs']:.2f}m"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-day harmonic residual time series from logged data."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--days", type=int, default=30,
        help="Trailing window in days (default 30). Ignored if --start/--end used."
    )
    group.add_argument(
        "--start", type=str, default=None,
        help="Start date YYYY-MM-DD (inclusive). Requires --end."
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date YYYY-MM-DD (inclusive). Requires --start."
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--start requires --end")
    if args.end and not args.start:
        parser.error("--end requires --start")

    if args.start:
        start_dt = _parse_date(args.start)
        # End is inclusive: add 1 day so events on the end date are captured.
        end_dt = _parse_date(args.end) + timedelta(days=1)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=args.days)

    start_str = to_utc_str(start_dt)
    end_str = to_utc_str(end_dt)

    print()
    print(f"Querying logged harmonic residual pairs: {start_str} to {end_str}")
    pairs = get_harmonic_residual_pairs(start_str, end_str)
    print(f"  Matched pairs: {len(pairs)}")

    if not pairs:
        print("  No matched pairs found. Check that both UKHO data and")
        print("  harmonic predictions exist in the database for this window.")
        return 1

    # Separate HW and LW
    hw_pairs = [p for p in pairs if p["event_type"] == "HighWater"]
    lw_pairs = [p for p in pairs if p["event_type"] == "LowWater"]

    print(f"  HW pairs: {len(hw_pairs)}, LW pairs: {len(lw_pairs)}")
    print()

    # Overall stats
    all_height = [p["height_residual_m"] for p in pairs]
    hw_height = [p["height_residual_m"] for p in hw_pairs]
    lw_height = [p["height_residual_m"] for p in lw_pairs]
    all_timing = [p["timing_residual_min"] for p in pairs]
    hw_timing = [p["timing_residual_min"] for p in hw_pairs]
    lw_timing = [p["timing_residual_min"] for p in lw_pairs]

    print("=" * 76)
    print("OVERALL HEIGHT RESIDUALS (predicted - actual)")
    print("=" * 76)
    print(f"  All:  {_fmt(_stats(all_height))}")
    print(f"  HW:   {_fmt(_stats(hw_height))}")
    print(f"  LW:   {_fmt(_stats(lw_height))}")
    print()

    print("=" * 76)
    print("OVERALL TIMING RESIDUALS (predicted - actual, minutes)")
    print("=" * 76)
    hw_t = _stats(hw_timing)
    lw_t = _stats(lw_timing)
    if hw_t["n"] > 0:
        print(
            f"  HW:   n={hw_t['n']:>3d}  "
            f"mean={hw_t['mean']:+.1f}min  "
            f"RMS={hw_t['rms']:.1f}min  "
            f"max|err|={hw_t['max_abs']:.1f}min"
        )
    if lw_t["n"] > 0:
        print(
            f"  LW:   n={lw_t['n']:>3d}  "
            f"mean={lw_t['mean']:+.1f}min  "
            f"RMS={lw_t['rms']:.1f}min  "
            f"max|err|={lw_t['max_abs']:.1f}min"
        )
    print()

    # Per-day breakdown, keyed by BST date of the UKHO event
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")

    by_day: dict[str, list[float]] = defaultdict(list)
    hw_by_day: dict[str, list[float]] = defaultdict(list)
    lw_by_day: dict[str, list[float]] = defaultdict(list)

    for p in pairs:
        ts = p["ukho_timestamp"]
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(london)
        day_key = dt.strftime("%Y-%m-%d")
        by_day[day_key].append(p["height_residual_m"])
        if p["event_type"] == "HighWater":
            hw_by_day[day_key].append(p["height_residual_m"])
        else:
            lw_by_day[day_key].append(p["height_residual_m"])

    print("=" * 76)
    print("PER-DAY HEIGHT RESIDUAL BREAKDOWN (BST date of UKHO event)")
    print("=" * 76)
    for day_key in sorted(by_day.keys()):
        vals = by_day[day_key]
        s = _stats(vals)
        hw_vals = hw_by_day.get(day_key, [])
        lw_vals = lw_by_day.get(day_key, [])
        hw_s = _stats(hw_vals)
        lw_s = _stats(lw_vals)

        hw_detail = ""
        if hw_s["n"] > 0:
            hw_detail = f"  HW: mean={hw_s['mean']:+.3f}m"
        lw_detail = ""
        if lw_s["n"] > 0:
            lw_detail = f"  LW: mean={lw_s['mean']:+.3f}m"

        print(f"    {day_key}: {_fmt(s)}{hw_detail}{lw_detail}")

    print()

    # Per-event detail table
    print("=" * 76)
    print("PER-EVENT DETAIL (chronological)")
    print("=" * 76)
    header = (
        f"  {'UKHO time':>20s}  {'type':>4s}  "
        f"{'UKHO ht':>7s}  {'harm ht':>7s}  "
        f"{'ht res':>7s}  {'t res':>7s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for p in sorted(pairs, key=lambda x: x["ukho_timestamp"]):
        ukho_dt = datetime.fromisoformat(
            p["ukho_timestamp"].replace("Z", "+00:00")
        ).astimezone(london)
        et_short = "HW" if p["event_type"] == "HighWater" else "LW"
        print(
            f"  {ukho_dt.strftime('%Y-%m-%d %H:%M'):>20s}  {et_short:>4s}  "
            f"{p['ukho_height_m']:>7.2f}  {p['harmonic_height_m']:>7.2f}  "
            f"{p['height_residual_m']:>+7.3f}  {p['timing_residual_min']:>+7.1f}"
        )

    print()
    print("=" * 76)
    print("DONE. No model parameters were modified.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
