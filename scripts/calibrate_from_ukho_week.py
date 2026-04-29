"""
Calibration analysis from accumulated UKHO half-hourly data.

Reads every CSV in app/calibration_data/ and runs two independent
comparisons against the combined dataset:

  1. Tidal-curve test
     The Langstone asymmetric tidal curve in app.access_calc is fed the
     HW/LW values (from dataset header lines OR derived via local-extremum
     detection on the half-hourly data) as anchor points, and its
     mid-cycle interpolation is compared against the actual half-hourly
     UKHO heights. Residuals are reported separately for the flood
     phase (LW->HW) and the ebb phase (HW->LW).

  2. Harmonic-model test
     For each half-hour timestamp in the dataset, app.harmonic.predict_
     height_at_time() is called and its output is compared against the
     actual UKHO height. Three variants are reported:
       (a) Raw: harmonic Portsmouth output used as-is.
       (b) Uniform offset: harmonic output sampled 9 min earlier and
           shifted up by 0.05m. Approximates what would happen if the
           secondary-port HW correction were applied as a constant shift
           across the entire cycle.
       (c) Production: predict_events() -> apply_offset() ->
           interpolate_height_at_time(). This mirrors what the deployed
           app actually computes when serving harmonic-derived heights
           via /api/tides or the access-window calculator. The offset is
           applied only at HW (timing and height); LW is unchanged.

     Comparing (b) and (c) tells us whether the production code path's
     mid-cycle accuracy is closer to the cycle-wide-shift approximation
     or the event-only approximation, and confirms that production is
     applying the offset in the correct way.

The script does not change any model parameters. Decisions about whether
to refine model_config.json or any of the harmonic constants are made
separately, after reviewing the output.

Multi-file design: the script auto-discovers any CSV in
app/calibration_data/. To add another week of data, drop a new CSV in
that directory and re-run. There is no need to modify the script.

HW/LW handling:
  Some datasets include explicit HW/LW summary lines as comments
  (lines starting "# YYYY-MM-DD  LW HH:MM Xm | HW HH:MM Ym ..."). These
  are used as anchors when present.

  When summary lines are absent, the script derives anchors from the
  half-hourly data via local-extremum detection. The detected extrema
  approximate but do not exactly match the UKHO published HW/LW (which
  may fall between half-hour samples). The resulting residuals will be
  marginally larger than they would be against true UKHO turning points;
  this is an inherent limitation of summary-less data and represents
  ~0.05m additional noise at most for HW/LW height and ~10min for time.

Usage:
    # In-container, after `docker compose up -d --build`:
    docker exec tidal-access python /app/scripts/calibrate_from_ukho_week.py

    # Or as a module (works because /app is on sys.path inside the container):
    docker exec tidal-access python -m scripts.calibrate_from_ukho_week

Path resolution: the script searches a few candidate locations for the
calibration data directory so it works whether the script lives at
scripts/ in the image, at /app/run_calibration.py from a hand copy, or
run directly from a repo checkout outside Docker.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytz

from app.access_calc import interpolate_height_at_time
from app.harmonic import predict_height_at_time, predict_events
from app.secondary_port import apply_offset

# Where the input data lives. The script discovers all .csv files in this
# directory regardless of filename. Path resolution handles three layouts:
#   1. Repo checkout: script at scripts/, data at app/calibration_data/
#      -> data dir is SCRIPT_DIR.parent / "app" / "calibration_data"
#   2. In-container, script at /app/scripts/, data at /app/app/calibration_data/
#      -> data dir is SCRIPT_DIR.parent / "app" / "calibration_data"
#      (same expression, works for both layouts above)
#   3. Ad-hoc copy: script copied to /app/run_calibration.py, data at
#      /app/app/calibration_data/
#      -> data dir is SCRIPT_DIR / "app" / "calibration_data"
#   4. Ad-hoc copy with data at /app/calibration_data/
#      -> data dir is SCRIPT_DIR / "calibration_data"
SCRIPT_DIR = Path(__file__).parent
_CANDIDATE_DIRS = [
    SCRIPT_DIR.parent / "app" / "calibration_data",  # layouts 1 and 2
    SCRIPT_DIR / "app" / "calibration_data",         # layout 3
    SCRIPT_DIR / "calibration_data",                  # layout 4
]
DATA_DIR = next((d for d in _CANDIDATE_DIRS if d.exists()), _CANDIDATE_DIRS[0])

# Constants from app.secondary_port - duplicated here rather than imported
# to avoid coupling the analysis script to the offset constants.
SECONDARY_HW_MINUTES = 9
SECONDARY_HW_HEIGHT_M = 0.05

LONDON = pytz.timezone("Europe/London")


def parse_dataset(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Parse a single calibration CSV.

    Returns (samples, events_by_date) where:
      - samples is a list of {"dt_utc": datetime, "height_m": float}
        in chronological order.
      - events_by_date maps "YYYY-MM-DD" -> list of HW/LW event dicts
        ({"timestamp": iso_utc, "height_m": float, "event_type": str})
        derived from header comment lines if present.
        EMPTY for files without summary lines - extrema will be derived
        later from the half-hourly samples.

    All BST timestamps in the CSV are converted to UTC for downstream use.
    """
    samples: list[dict] = []
    events_by_date: dict[str, list[dict]] = {}

    # Header pattern: "# 2026-04-29  LW 03:45 1.3m | HW 11:29 4.4m | ..."
    header_re = re.compile(
        r"^#\s*(\d{4}-\d{2}-\d{2})\s+(.*HW.*|.*LW.*)$"
    )
    event_re = re.compile(
        r"(HW|LW)\s+(\d{2}):(\d{2})\s+([\d.]+)m"
    )

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue

            if line.startswith("#"):
                m = header_re.match(line)
                if not m:
                    continue
                date_str, rest = m.group(1), m.group(2)
                if date_str not in events_by_date:
                    events_by_date[date_str] = []
                d = datetime.strptime(date_str, "%Y-%m-%d")
                for em in event_re.finditer(rest):
                    typ, hh, mm, h = em.groups()
                    bst = LONDON.localize(d.replace(hour=int(hh), minute=int(mm)))
                    utc = bst.astimezone(timezone.utc)
                    events_by_date[date_str].append({
                        "timestamp": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "height_m": float(h),
                        "event_type": "HighWater" if typ == "HW" else "LowWater",
                    })
                continue

            # Sample row: "2026-04-29 00:00,4.4"
            try:
                ts_str, h_str = line.split(",")
                date_part, time_part = ts_str.split(" ")
                d = datetime.strptime(date_part, "%Y-%m-%d")
                hh, mm = time_part.split(":")
                bst = LONDON.localize(d.replace(hour=int(hh), minute=int(mm)))
                utc = bst.astimezone(timezone.utc)
                samples.append({
                    "dt_utc": utc,
                    "height_m": float(h_str),
                })
            except ValueError:
                continue

    return samples, events_by_date


def derive_extrema(samples: list[dict]) -> list[dict]:
    """
    Derive HW/LW extrema from half-hourly samples via local-extremum
    detection. Used for files without explicit HW/LW summary lines.

    A local maximum is a sample whose height equals or exceeds both
    neighbours, with at least one strict inequality. Ties (e.g. two
    adjacent half-hours at the same peak height) take the middle of
    the tied region as the extremum time.

    Returns a list of event dicts in the same shape as parse_dataset's
    events_by_date entries.
    """
    if len(samples) < 3:
        return []

    events: list[dict] = []
    n = len(samples)

    # Iterate looking for runs where consecutive samples are non-decreasing
    # then non-increasing (HW) or non-increasing then non-decreasing (LW).
    # The pivot point is the centre of the flat-top run.
    i = 1
    while i < n - 1:
        h_prev = samples[i - 1]["height_m"]
        h_curr = samples[i]["height_m"]
        h_next = samples[i + 1]["height_m"]

        is_max = h_prev <= h_curr >= h_next and (h_prev < h_curr or h_curr > h_next)
        is_min = h_prev >= h_curr <= h_next and (h_prev > h_curr or h_curr < h_next)

        if not (is_max or is_min):
            i += 1
            continue

        # Walk forward through any tied region.
        j = i
        while j + 1 < n and samples[j + 1]["height_m"] == h_curr:
            j += 1

        # Centre of the run (rounded to nearest sample for ties).
        centre_idx = (i + j) // 2
        ev = {
            "timestamp": samples[centre_idx]["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "height_m": h_curr,
            "event_type": "HighWater" if is_max else "LowWater",
        }
        events.append(ev)
        i = j + 1

    return events


def merge_events(
    events_by_date: dict[str, list[dict]],
    derived_events: list[dict],
) -> list[dict]:
    """
    Combine summary-line events (preferred) and derived events.

    For dates that appear in events_by_date, only those events are used.
    For dates absent from events_by_date, derived events are used.

    This handles the mixed case of multiple files where some have
    summary lines and some do not.
    """
    summary_dates = set(events_by_date.keys())
    summary_events = []
    for date_str in sorted(summary_dates):
        summary_events.extend(events_by_date[date_str])

    # Filter derived events to only dates not covered by summary events.
    # Use the date in BST so a tide event at 23:30 UTC on 30 April that
    # is actually 00:30 BST on 1 May is bucketed correctly.
    derived_filtered = []
    for ev in derived_events:
        ev_dt = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        bst_date = ev_dt.astimezone(LONDON).strftime("%Y-%m-%d")
        if bst_date not in summary_dates:
            derived_filtered.append(ev)

    combined = summary_events + derived_filtered
    combined.sort(key=lambda e: e["timestamp"])
    return combined


def load_all_data() -> tuple[list[dict], list[dict]]:
    """
    Load and combine every CSV in DATA_DIR.

    Returns (all_samples, all_events) where both are time-sorted lists
    spanning the full corpus.
    """
    if not DATA_DIR.exists():
        print(f"ERROR: calibration data directory not found at {DATA_DIR}")
        return [], []

    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"ERROR: no CSV files found in {DATA_DIR}")
        return [], []

    all_samples: list[dict] = []
    all_summary_events: dict[str, list[dict]] = {}
    per_file_derived: list[list[dict]] = []

    for csv_path in csv_files:
        samples, events_by_date = parse_dataset(csv_path)
        n_summary = sum(len(v) for v in events_by_date.values())
        derived = []
        if not events_by_date:
            # No summary lines anywhere in this file - derive all extrema.
            derived = derive_extrema(samples)
        print(f"  {csv_path.name}: {len(samples)} samples, "
              f"{n_summary} summary events, {len(derived)} derived events")

        all_samples.extend(samples)
        for date_str, evs in events_by_date.items():
            all_summary_events.setdefault(date_str, []).extend(evs)
        per_file_derived.append(derived)

    # Merge - summary events take precedence by date, derived events fill gaps.
    flat_derived = [ev for batch in per_file_derived for ev in batch]
    all_events = merge_events(all_summary_events, flat_derived)

    all_samples.sort(key=lambda s: s["dt_utc"])
    return all_samples, all_events


def classify_phase(target_dt: datetime, events: list[dict]) -> str:
    """
    Classify a sample as 'flood' (LW->HW) or 'ebb' (HW->LW) based on the
    bracketing events. Returns 'unknown' if no bracket can be found OR
    if the bracket spans more than one tidal cycle (~13 hours), which
    indicates the sample falls in a gap between disjoint datasets.
    """
    before = None
    after = None
    for ev in events:
        ev_dt = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        if ev_dt <= target_dt:
            before = ev
        if ev_dt > target_dt and after is None:
            after = ev
            break
    if not before or not after:
        return "unknown"
    # Reject brackets that span more than ~13 hours - a real flood or ebb
    # is ~6.2 hours, so a wider gap means either missing data on one side
    # or two disjoint datasets being spuriously joined.
    before_dt = datetime.fromisoformat(before["timestamp"].replace("Z", "+00:00"))
    after_dt = datetime.fromisoformat(after["timestamp"].replace("Z", "+00:00"))
    if (after_dt - before_dt).total_seconds() > 13 * 3600:
        return "unknown"
    if before["event_type"] == "LowWater" and after["event_type"] == "HighWater":
        return "flood"
    if before["event_type"] == "HighWater" and after["event_type"] == "LowWater":
        return "ebb"
    return "unknown"


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": 0.0, "rms": 0.0, "max_abs": 0.0}
    n = len(values)
    mean = sum(values) / n
    rms = math.sqrt(sum(v * v for v in values) / n)
    max_abs = max(abs(v) for v in values)
    return {"n": n, "mean": mean, "rms": rms, "max_abs": max_abs}


def fmt_stats(s: dict) -> str:
    if s["n"] == 0:
        return "no samples"
    return (
        f"n={s['n']:4d}  "
        f"mean={s['mean']:+.3f}m  "
        f"RMS={s['rms']:.3f}m  "
        f"max|err|={s['max_abs']:.2f}m"
    )


def _bracket_too_wide(target_dt: datetime, events: list[dict]) -> bool:
    """
    Returns True if the events bracketing target_dt are more than one tide
    cycle apart (~13h), or if no bracket exists at all. Used to filter
    out samples that fall in the gap between disjoint CSV datasets.
    """
    before = None
    after = None
    for ev in events:
        ev_dt = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        if ev_dt <= target_dt:
            before = ev
        if ev_dt > target_dt and after is None:
            after = ev
            break
    if not before or not after:
        return True
    before_dt = datetime.fromisoformat(before["timestamp"].replace("Z", "+00:00"))
    after_dt = datetime.fromisoformat(after["timestamp"].replace("Z", "+00:00"))
    return (after_dt - before_dt).total_seconds() > 13 * 3600


def run_curve_test(samples: list[dict], events: list[dict]) -> None:
    print("=" * 76)
    print("CURVE TEST: app.access_calc.interpolate_height_at_time")
    print("Anchors: HW/LW from summary lines where present, else derived")
    print("from local extrema in the half-hourly data.")
    print("=" * 76)

    flood_resid: list[float] = []
    ebb_resid: list[float] = []
    all_resid: list[float] = []
    skipped = 0

    event_dts = {
        datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        for ev in events
    }

    for s in samples:
        # Skip samples within 3 minutes of any HW/LW (effectively on the anchor).
        if any(abs((s["dt_utc"] - et).total_seconds()) < 180 for et in event_dts):
            skipped += 1
            continue
        # Skip samples whose bracket spans > 13h (gap between disjoint CSVs,
        # or single-sided coverage at the edge of a dataset). The interpolator
        # would still return a value but its meaning is dubious.
        if _bracket_too_wide(s["dt_utc"], events):
            skipped += 1
            continue

        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, events)
        if predicted is None:
            skipped += 1
            continue

        residual = predicted - s["height_m"]
        all_resid.append(residual)
        phase = classify_phase(s["dt_utc"], events)
        if phase == "flood":
            flood_resid.append(residual)
        elif phase == "ebb":
            ebb_resid.append(residual)

    print(f"  All:    {fmt_stats(stats(all_resid))}")
    print(f"  Flood:  {fmt_stats(stats(flood_resid))}")
    print(f"  Ebb:    {fmt_stats(stats(ebb_resid))}")
    print(f"  Skipped (anchor or no bracket): {skipped}")
    print()

    # Per-day breakdown for visibility.
    print("  Per-day breakdown (BST date):")
    by_day: dict[str, list[float]] = {}
    for s in samples:
        if any(abs((s["dt_utc"] - et).total_seconds()) < 180 for et in event_dts):
            continue
        if _bracket_too_wide(s["dt_utc"], events):
            continue
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, events)
        if predicted is None:
            continue
        local_date = s["dt_utc"].astimezone(LONDON).date().isoformat()
        by_day.setdefault(local_date, []).append(predicted - s["height_m"])
    for date in sorted(by_day):
        print(f"    {date}: {fmt_stats(stats(by_day[date]))}")
    print()


def run_harmonic_test(samples: list[dict]) -> None:
    print("=" * 76)
    print("HARMONIC TEST: app.harmonic.predict_height_at_time + production path")
    print("Three variants: raw, uniform-offset (test sim), production-equivalent.")
    print("=" * 76)

    raw_resid: list[float] = []
    offset_resid: list[float] = []

    for s in samples:
        raw_pred = predict_height_at_time(s["dt_utc"])
        raw_resid.append(raw_pred - s["height_m"])

        offset_pred = (
            predict_height_at_time(s["dt_utc"] - timedelta(minutes=SECONDARY_HW_MINUTES))
            + SECONDARY_HW_HEIGHT_M
        )
        offset_resid.append(offset_pred - s["height_m"])

    print(f"  (a) Raw (Portsmouth as Langstone, no offset):")
    print(f"      {fmt_stats(stats(raw_resid))}")
    print()
    print(f"  (b) Uniform offset (sampled -9min, +0.05m height):")
    print(f"      {fmt_stats(stats(offset_resid))}")
    print()

    # --- Variant (c): production-equivalent path ---
    # Mirrors what app/scheduler.py and app/main.py do at runtime:
    #   harmonic.predict_events(start, end)         # Portsmouth HW/LW events
    #   secondary_port.apply_offset(events)         # +9min/+0.05m to HW only
    #   access_calc.interpolate_height_at_time(t, events)  # mid-cycle heights
    #
    # The events are computed once for the whole corpus span (with a small
    # buffer either side so events bracketing the first/last sample are
    # available), then passed to the interpolator for each half-hourly
    # sample. This matches the production data flow precisely, including
    # the Admiralty-convention timing offsets baked into predict_events.
    if not samples:
        return
    span_start = samples[0]["dt_utc"] - timedelta(hours=12)
    span_end = samples[-1]["dt_utc"] + timedelta(hours=12)
    portsmouth_events = predict_events(span_start, span_end)
    langstone_events = apply_offset(portsmouth_events)

    production_resid: list[float] = []
    skipped_no_bracket = 0
    for s in samples:
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, langstone_events)
        if predicted is None:
            skipped_no_bracket += 1
            continue
        production_resid.append(predicted - s["height_m"])

    print(f"  (c) Production path (predict_events -> apply_offset -> interpolate):")
    print(f"      {fmt_stats(stats(production_resid))}")
    if skipped_no_bracket:
        print(f"      (skipped {skipped_no_bracket} samples with no harmonic event bracket)")
    print()

    # Comparison summary so the relationship between the three variants
    # is immediately readable. Negative delta = production is better than
    # uniform-offset; positive delta = production is worse.
    if production_resid:
        prod_rms = stats(production_resid)["rms"]
        offset_rms = stats(offset_resid)["rms"]
        raw_rms = stats(raw_resid)["rms"]
        print(f"  Comparison:")
        print(f"    production vs uniform-offset:  RMS {prod_rms:.3f}m vs {offset_rms:.3f}m "
              f"(delta {prod_rms - offset_rms:+.3f}m)")
        print(f"    production vs raw:             RMS {prod_rms:.3f}m vs {raw_rms:.3f}m "
              f"(delta {prod_rms - raw_rms:+.3f}m)")
        print()

    print("  Per-day breakdown (production path, BST date):")
    by_day: dict[str, list[float]] = {}
    # Re-iterate to align production residuals with samples; production_resid
    # may be shorter than samples if any were skipped.
    pi = 0
    for s in samples:
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, langstone_events)
        if predicted is None:
            continue
        local_date = s["dt_utc"].astimezone(LONDON).date().isoformat()
        by_day.setdefault(local_date, []).append(predicted - s["height_m"])
    for date in sorted(by_day):
        print(f"    {date}: {fmt_stats(stats(by_day[date]))}")
    print()

    print("  Production path by approximate cycle position:")
    print("  (height-bin grouping; raw residuals show whether bias is height-dependent)")
    bins: dict[str, list[float]] = {
        "very low (<1.5m)":     [],
        "low (1.5-2.5m)":       [],
        "mid (2.5-3.5m)":       [],
        "high (3.5-4.5m)":      [],
        "very high (>=4.5m)":   [],
    }
    for s in samples:
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, langstone_events)
        if predicted is None:
            continue
        residual = predicted - s["height_m"]
        h = s["height_m"]
        if h < 1.5:
            bins["very low (<1.5m)"].append(residual)
        elif h < 2.5:
            bins["low (1.5-2.5m)"].append(residual)
        elif h < 3.5:
            bins["mid (2.5-3.5m)"].append(residual)
        elif h < 4.5:
            bins["high (3.5-4.5m)"].append(residual)
        else:
            bins["very high (>=4.5m)"].append(residual)
    for label, vals in bins.items():
        print(f"    {label:22s}: {fmt_stats(stats(vals))}")
    print()


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

    run_curve_test(samples, events)
    run_harmonic_test(samples)

    print("=" * 76)
    print("DONE. No model parameters were modified.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
