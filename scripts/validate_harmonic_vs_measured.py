"""
Validate the harmonic tidal model against measured Portsmouth sea level.

Companion to ``scripts/validate_barometric_k.py``. Where that script regresses
the ``(measured - predicted)`` residual against pressure to recover the
barometric coefficient k, THIS script answers a simpler, more direct question:

    How well does the harmonic model reproduce the *actual* height of tide,
    for PAST tidal events, when checked against a public measured-water-level
    archive for the immediate area of the harbour?

The model runs happily for past epochs -- ``app.harmonic.predict_height_at_time``
derives its astronomical arguments from a Julian Date with no past/future guard,
so pointing it backwards is nothing special.

Reference gauge
---------------
``app.harmonic`` is *Portsmouth-native* (it returns metres above Portsmouth
Chart Datum, before the secondary-port shift). The reference here is the
Portsmouth tide gauge, which is the standard port for Langstone Harbour --
Langstone has no long-term public gauge of its own, and its own UKHO station is
itself a secondary port derived from Portsmouth. So NO secondary-port offset is
applied: this is a like-for-like check of the harmonic engine itself, at the
nearest place with a public height-of-water archive.

Datum
-----
The gauge reads metres above Ordnance Datum Newlyn (mAOD); the model reads
metres above Chart Datum (CD). Portsmouth CD is 2.73 m below OD(N) (Admiralty
NP201 / ATT), so:  height_above_CD = sea_level_mAOD + 2.73 . We apply that
nominal offset AND print the offset the data itself implies (mean predicted_CD -
mean measured_mAOD) as a units/datum cross-check -- they should agree to ~0.1 m.
Timing errors and tidal range (HW-LW) are datum-independent and reported as
such.

Weather caveat
--------------
Measured sea level = astronomical tide + surge (barometric + wind). The harmonic
model is astronomical only, so the residual scatter INCLUDES real weather and is
therefore an UPPER bound on the model's astronomical error. ``--barometric``
applies the v2.9 inverse-barometer correction (from ERA5 pressure) to strip the
pressure-driven part and show how much of the residual it explains.

Data sources (shared with validate_barometric_k)
------------------------------------------------
* Measured sea level at Portsmouth:
    - ``ea``          : EA flood-monitoring live API (station E71839, mAOD,
                        15-min). One request, rolling ~4 weeks. Fast default.
    - ``ea-archive``  : EA daily archive dumps over --start..--end. Reaches back
                        years; bandwidth-heavy over long spans.
    - ``csv``         : normalised BODC export (timestamp_utc,sea_level_m).
* Pressure (for --barometric): Open-Meteo ERA5 archive, free, no key.

Usage
-----
    # Fast: last 4 weeks of the live Portsmouth gauge (one request)
    python -m scripts.validate_harmonic_vs_measured --source ea --days 28

    # A specific past month from the archive
    python -m scripts.validate_harmonic_vs_measured --source ea-archive \
        --start 2026-05-01 --end 2026-05-31

    # Also strip the inverse-barometer part with ERA5 pressure
    python -m scripts.validate_harmonic_vs_measured --source ea --days 28 --barometric

Inside the container:
    docker exec -w /app tidal-access python -m scripts.validate_harmonic_vs_measured --source ea --days 28
"""

import argparse
import math
import sys
from datetime import datetime, timezone

from app.harmonic import predict_height_at_time

# Reuse the (already-reviewed) data loaders from the k-validation script so
# there is a single source of truth for the archive/gauge access.
from scripts.validate_barometric_k import (
    load_sea_level_ea,
    load_sea_level_ea_archive,
    load_sea_level_csv,
    fetch_pressure_wind,
    _interp,
    PORTSMOUTH_LAT,
    PORTSMOUTH_LON,
)

# Portsmouth Chart Datum is 2.73 m below Ordnance Datum Newlyn (Admiralty NP201).
# height_above_CD = sea_level_mAOD + DATUM_OFFSET_M.
DATUM_OFFSET_M = 2.73

# Peak-matching half-window: for each predicted HW/LW we search the measured
# series within +/- this many minutes for the corresponding physical extreme.
PEAK_MATCH_WINDOW_MIN = 120

# Fine grid (minutes) used to locate the model's own physical HW/LW turning
# points cleanly. The astronomical curve is smooth so this need not be dense.
PRED_GRID_MIN = 2


# --------------------------------------------------------------------------
# Small stats helpers (pure Python, no numpy)
# --------------------------------------------------------------------------

def _stats(xs: list[float]) -> dict:
    """count, mean, rms, stdev (about mean), max-abs, min, max of a list."""
    n = len(xs)
    if n == 0:
        return {"n": 0}
    mean = sum(xs) / n
    rms = math.sqrt(sum(x * x for x in xs) / n)
    var = sum((x - mean) ** 2 for x in xs) / n
    return {
        "n": n,
        "mean": mean,
        "rms": rms,
        "std": math.sqrt(var),
        "maxabs": max(abs(x) for x in xs),
        "min": min(xs),
        "max": max(xs),
    }


def _quad_refine(times, heights, i):
    """Parabolic refinement of a turning point at index i.

    Returns (refined_datetime, refined_height). Assumes near-uniform spacing
    around i (true for the 2-min predicted grid and the 15-min gauge)."""
    dt_s = (times[i] - times[i - 1]).total_seconds()
    y0, y1, y2 = heights[i - 1], heights[i], heights[i + 1]
    denom = 2 * (2 * y1 - y0 - y2)
    if abs(denom) < 1e-12:
        return times[i], heights[i]
    off = (y0 - y2) / denom  # fraction of a step, in [-0.5, 0.5]
    from datetime import timedelta
    return times[i] + timedelta(seconds=off * dt_s), y1 + 0.25 * (y0 - y2) * off


def predicted_turning_points(start: datetime, end: datetime, grid_min: int = PRED_GRID_MIN):
    """Physical HW/LW of the harmonic model itself (raw mathematical peaks, NOT
    the Admiralty-convention-shifted times from predict_events -- we compare
    against a real gauge whose peaks are physical, so raw peaks are correct).

    Returns list of (event_type, datetime, height_CD)."""
    from datetime import timedelta
    times, heights = [], []
    t = start
    step = timedelta(minutes=grid_min)
    while t <= end:
        times.append(t)
        heights.append(predict_height_at_time(t))
        t += step
    out = []
    for i in range(1, len(heights) - 1):
        y0, y1, y2 = heights[i - 1], heights[i], heights[i + 1]
        if y1 > y0 and y1 > y2:
            rt, rh = _quad_refine(times, heights, i)
            out.append(("HighWater", rt, rh))
        elif y1 < y0 and y1 < y2:
            rt, rh = _quad_refine(times, heights, i)
            out.append(("LowWater", rt, rh))
    return out


def measured_extreme_near(sea, lo_idx_hint, t_center, kind, offset_m,
                          window_min=PEAK_MATCH_WINDOW_MIN):
    """Find the measured physical extreme (HW=max, LW=min) within +/- window of
    t_center. ``sea`` is sorted [(dt, mAOD)]. Returns (datetime, height_CD,
    edge_flag) or None if too few samples in the window.

    edge_flag is True when the extreme sits on the window boundary (the true
    peak may be outside), so the caller can down-weight/ignore it."""
    from datetime import timedelta
    t0 = t_center - timedelta(minutes=window_min)
    t1 = t_center + timedelta(minutes=window_min)
    # Linear scan from the hint (series is time-ordered; hints advance).
    idxs = []
    i = max(0, lo_idx_hint)
    # rewind if the hint overshot
    while i > 0 and sea[i][0] > t0:
        i -= 1
    while i < len(sea) and sea[i][0] < t0:
        i += 1
    while i < len(sea) and sea[i][0] <= t1:
        idxs.append(i)
        i += 1
    if len(idxs) < 3:
        return None
    if kind == "HighWater":
        best = max(idxs, key=lambda j: sea[j][1])
    else:
        best = min(idxs, key=lambda j: sea[j][1])
    edge = best == idxs[0] or best == idxs[-1]
    # Parabolic refine if we have both neighbours.
    if 0 < best < len(sea) - 1:
        times = [sea[best - 1][0], sea[best][0], sea[best + 1][0]]
        vals = [sea[best - 1][1], sea[best][1], sea[best + 1][1]]
        rt, rv = _quad_refine(times, vals, 1)
    else:
        rt, rv = sea[best][0], sea[best][1]
    return rt, rv + offset_m, edge


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Validate harmonic tide heights against measured Portsmouth sea level.")
    ap.add_argument("--source", choices=["ea", "ea-archive", "csv"], default="ea")
    ap.add_argument("--file", help="CSV path for --source csv (timestamp_utc,sea_level_m)")
    ap.add_argument("--days", type=int, default=28, help="EA live lookback window (rolling ~4-week max)")
    ap.add_argument("--start", help="ea-archive start date YYYY-MM-DD")
    ap.add_argument("--end", help="ea-archive end date YYYY-MM-DD")
    ap.add_argument("--ref", default="E71839", help="EA station reference (default E71839 = Portsmouth mAOD)")
    ap.add_argument("--datum-offset", type=float, default=DATUM_OFFSET_M,
                    help="metres to add to gauge readings to reach Chart Datum (mAOD->CD = 2.73; "
                         "pass 0 if the CSV is already on Chart Datum)")
    ap.add_argument("--barometric", action="store_true",
                    help="also report residuals after the v2.9 inverse-barometer correction (ERA5 pressure)")
    ap.add_argument("--lat", type=float, default=PORTSMOUTH_LAT)
    ap.add_argument("--lon", type=float, default=PORTSMOUTH_LON)
    args = ap.parse_args()

    print("Harmonic model vs measured Portsmouth sea level")
    print("=" * 62)

    # ---- 1. Load measured sea level (mAOD) ------------------------------
    if args.source == "ea":
        print(f"Sea level: EA live gauge {args.ref} (mAOD), last {args.days} days")
        sea = load_sea_level_ea(args.days, args.ref)
    elif args.source == "ea-archive":
        if not (args.start and args.end):
            ap.error("--source ea-archive requires --start and --end (YYYY-MM-DD)")
        print(f"Sea level: EA archive {args.ref} (mAOD), {args.start}..{args.end}")
        sea = load_sea_level_ea_archive(args.start, args.end, args.ref)
    else:
        if not args.file:
            ap.error("--source csv requires --file")
        print(f"Sea level: CSV {args.file}")
        sea = load_sea_level_csv(args.file)

    if len(sea) < 50:
        print(f"Only {len(sea)} sea-level readings -- too few. Aborting.")
        return 1
    start, end = sea[0][0], sea[-1][0]
    span_days = (end - start).total_seconds() / 86400.0
    print(f"Readings: {len(sea)}  ({start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC, {span_days:.1f} d)")
    offset = args.datum_offset
    print(f"Datum offset applied to gauge: +{offset:.3f} m  (mAOD -> Chart Datum)")
    print()

    # ---- 2. Point-by-point height residual (headline) -------------------
    # residual = predicted_CD - measured_CD   (repo sign: + => model over-predicts)
    residuals = []
    pred_minus_gauge = []   # predicted_CD - measured_mAOD  -> implied datum offset
    for when, mAOD in sea:
        pred = predict_height_at_time(when)
        measured_CD = mAOD + offset
        residuals.append(pred - measured_CD)
        pred_minus_gauge.append(pred - mAOD)

    s = _stats(residuals)
    implied_offset = sum(pred_minus_gauge) / len(pred_minus_gauge)
    print("A. Point-by-point height  (predicted - measured, every 15 min)")
    print(f"   n              = {s['n']}")
    print(f"   mean bias      = {s['mean']:+.3f} m   (+ => model reads high)")
    print(f"   RMS error      = {s['rms']:.3f} m")
    print(f"   stdev (de-mean)= {s['std']:.3f} m   <- datum-independent tracking error")
    print(f"   max |error|    = {s['maxabs']:.3f} m")
    print(f"   implied datum  = {implied_offset:+.3f} m  (mean predicted_CD - gauge_mAOD; "
          f"expect ~{DATUM_OFFSET_M:.2f})")
    datum_gap = implied_offset - DATUM_OFFSET_M
    flag = "OK" if abs(datum_gap) < 0.15 else "CHECK units/datum!"
    print(f"                    -> differs from nominal by {datum_gap:+.3f} m  [{flag}]")
    print()

    # ---- 3. HW/LW events: timing, height, range -------------------------
    pred_events = predicted_turning_points(start, end)
    hw_time, lw_time = [], []      # minutes: predicted - measured (datum-independent)
    hw_h, lw_h = [], []            # metres:  predicted - measured (datum-dependent)
    matched = []                   # (type, t_pred, h_pred, t_meas, h_meas_CD) in time order
    hint = 0
    for et, tp, hp in pred_events:
        # advance hint near tp
        while hint < len(sea) - 1 and sea[hint][0] < tp:
            hint += 1
        m = measured_extreme_near(sea, hint, tp, et, offset)
        if m is None:
            continue
        tm, hm_CD, edge = m
        if edge:
            continue  # true peak may lie outside the gauge window; skip
        dmin = (tp - tm).total_seconds() / 60.0
        dh = hp - hm_CD
        if et == "HighWater":
            hw_time.append(dmin)
            hw_h.append(dh)
        else:
            lw_time.append(dmin)
            lw_h.append(dh)
        matched.append((et, tp, hp, tm, hm_CD))

    def show(label, xs, unit):
        st = _stats(xs)
        if st["n"] == 0:
            print(f"   {label}: no matches")
            return
        print(f"   {label:<22} n={st['n']:<4} mean {st['mean']:+.2f}  "
              f"stdev {st['std']:.2f}  rms {st['rms']:.2f}  max|{st['maxabs']:.2f}| {unit}")

    print("B. HW/LW turning points  (model physical peak vs gauge physical peak)")
    print("   Timing  (predicted - measured, minutes; + => model late):")
    show("HW timing", hw_time, "min")
    show("LW timing", lw_time, "min")
    print("   Height  (predicted - measured, metres; datum offset applied):")
    show("HW height", hw_h, "m")
    show("LW height", lw_h, "m")

    # Tidal range HW-LW is datum-independent: compare consecutive matched peaks.
    ranges = []
    for k in range(len(matched) - 1):
        et0, _, hp0, _, hm0 = matched[k]
        et1, _, hp1, _, hm1 = matched[k + 1]
        if et0 != et1:
            pr = abs(hp0 - hp1)
            mr = abs(hm0 - hm1)
            ranges.append(pr - mr)
    print("   Range   (predicted - measured HW-LW, metres; datum-INDEPENDENT):")
    show("tidal range", ranges, "m")
    print()

    # ---- 4. Optional: strip the inverse-barometer part ------------------
    if args.barometric:
        from app.barometric import correction_for_pressure
        print("C. After v2.9 inverse-barometer correction (ERA5 pressure)")
        print("   Fetching ERA5 pressure from Open-Meteo archive...")
        met = fetch_pressure_wind(args.lat, args.lon, start, end)
        if not met:
            print("   No pressure returned; skipping.")
        else:
            corr_res = []
            used = 0
            for when, mAOD in sea:
                mw = _interp(met, when)
                if mw is None:
                    continue
                p, _w = mw
                corr = correction_for_pressure(p)["correction_m"]
                pred = predict_height_at_time(when) + corr
                corr_res.append(pred - (mAOD + offset))
                used += 1
            cs = _stats(corr_res)
            print(f"   n              = {cs['n']}  (pressure-matched of {s['n']})")
            print(f"   mean bias      = {cs['mean']:+.3f} m")
            print(f"   RMS error      = {cs['rms']:.3f} m   (was {s['rms']:.3f} m uncorrected)")
            print(f"   stdev (de-mean)= {cs['std']:.3f} m   (was {s['std']:.3f} m uncorrected)")
            improve = (s['std'] - cs['std'])
            print(f"   -> pressure explains {improve:+.3f} m of the de-meaned scatter")
        print()

    # ---- 5. Caveats -----------------------------------------------------
    print("Notes")
    print("-----")
    print("* Model is astronomical only; the gauge includes surge, so residual")
    print("  scatter is an UPPER bound on the harmonic model's own error.")
    if span_days < 30:
        print("* Short span: one or two weather systems can dominate the numbers.")
        print("  Re-run with --source ea-archive --start .. --end .. for a longer span.")
    print("* HW timing at Portsmouth is noisy: the Solent HW 'stand' flattens the")
    print("  peak, so LW timing and the height/range figures are the sharper tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
