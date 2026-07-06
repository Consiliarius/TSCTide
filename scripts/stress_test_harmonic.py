"""
Full-corpus stress test of the harmonic model vs measured Portsmouth sea level.

Runs the harmonic-vs-measured check (see validate_harmonic_vs_measured.py) for a
sequence of whole months, one at a time, then reports a per-month table AND a
pooled combined-corpus aggregate. Default corpus: August 2025 .. June 2026
inclusive (11 months).

Each month's measured sea level is pulled from the Environment Agency
flood-monitoring ARCHIVE (Portsmouth gauge E71839, mAOD, 15-min). That is
bandwidth-heavy (a full all-station daily dump per day, tens of MB each, ~30 per
month), so each month's extracted Portsmouth series is CACHED to
<out-dir>/cache/portsmouth_YYYY-MM.csv. Re-runs load the cache and cost nothing;
a job interrupted partway resumes cheaply.

The model is Portsmouth-native (app.harmonic, metres above Chart Datum), the
gauge is Portsmouth (mAOD) -- so NO secondary-port offset, a like-for-like check
of the engine. Gauge is converted to Chart Datum with the fixed +2.73 m offset
(Admiralty NP201). Timing and tidal range are datum-independent.

Progress is written incrementally (flushed) to <out-dir>/stress_report.txt and to
stdout, so the run can be watched while it proceeds. A machine-readable
<out-dir>/stress_report.json is written at the end.

Usage
-----
    python -m scripts.stress_test_harmonic --out-dir /path/to/scratchpad
    python -m scripts.stress_test_harmonic --out-dir . --months 2025-08:2026-06
    docker exec -w /app tidal-access python -m scripts.stress_test_harmonic --out-dir /app/data/stress
"""

import argparse
import calendar
import json
import os
import sys
from datetime import datetime, timezone

from app.harmonic import predict_height_at_time
from app.barometric import correction_for_pressure
from scripts.validate_barometric_k import (
    load_sea_level_ea_archive,
    load_sea_level_csv,
    fetch_pressure_wind,
    _interp,
    PORTSMOUTH_LAT,
    PORTSMOUTH_LON,
)
from scripts.validate_harmonic_vs_measured import (
    _stats,
    predicted_turning_points,
    measured_extreme_near,
    DATUM_OFFSET_M,
)

DEFAULT_MONTHS = [
    (2025, 8), (2025, 9), (2025, 10), (2025, 11), (2025, 12),
    (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6),
]


def parse_months(spec: str) -> list[tuple[int, int]]:
    """'2025-08:2026-06' -> inclusive list of (year, month)."""
    a, b = spec.split(":")
    ya, ma = (int(x) for x in a.split("-"))
    yb, mb = (int(x) for x in b.split("-"))
    out = []
    y, m = ya, ma
    while (y, m) <= (yb, mb):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def month_bounds(y: int, m: int) -> tuple[str, str]:
    last = calendar.monthrange(y, m)[1]
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"


def get_month_sea(y: int, m: int, cache_dir: str, ref: str, log) -> list[tuple[datetime, float]]:
    """Measured Portsmouth series for month (y, m): from cache if present, else
    pull the EA archive and write the cache. Returns sorted [(utc_dt, mAOD)]."""
    cache_path = os.path.join(cache_dir, f"portsmouth_{y}-{m:02d}.csv")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        sea = load_sea_level_csv(cache_path)
        log(f"  cache hit: {len(sea)} readings from {os.path.basename(cache_path)}")
        return sea
    start, end = month_bounds(y, m)
    log(f"  fetching EA archive {start}..{end} (this is the slow part)...")
    sea = load_sea_level_ea_archive(start, end, ref)
    if sea:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", newline="") as f:
            f.write("timestamp_utc,sea_level_m\n")
            for dt, val in sea:
                f.write(f"{dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{val}\n")
        log(f"  cached {len(sea)} readings -> {os.path.basename(cache_path)}")
    return sea


def month_metrics(sea: list[tuple[datetime, float]]) -> dict:
    """Compute all residual series for one month's measured data."""
    residuals, pmg = [], []
    for dt, mAOD in sea:
        pred = predict_height_at_time(dt)
        residuals.append(pred - (mAOD + DATUM_OFFSET_M))
        pmg.append(pred - mAOD)

    start, end = sea[0][0], sea[-1][0]
    pred_events = predicted_turning_points(start, end)
    hw_t, lw_t, hw_h, lw_h = [], [], [], []
    matched = []
    hint = 0
    for et, tp, hp in pred_events:
        while hint < len(sea) - 1 and sea[hint][0] < tp:
            hint += 1
        found = measured_extreme_near(sea, hint, tp, et, DATUM_OFFSET_M)
        if found is None:
            continue
        tm, hm_CD, edge = found
        if edge:
            continue
        dmin = (tp - tm).total_seconds() / 60.0
        dh = hp - hm_CD
        if et == "HighWater":
            hw_t.append(dmin)
            hw_h.append(dh)
        else:
            lw_t.append(dmin)
            lw_h.append(dh)
        matched.append((et, hp, hm_CD))

    ranges = []
    for k in range(len(matched) - 1):
        e0, hp0, hm0 = matched[k]
        e1, hp1, hm1 = matched[k + 1]
        if e0 != e1:
            ranges.append(abs(hp0 - hp1) - abs(hm0 - hm1))

    return {"residuals": residuals, "pmg": pmg, "hw_t": hw_t, "lw_t": lw_t,
            "hw_h": hw_h, "lw_h": lw_h, "ranges": ranges}


def _fmt_month_row(label, days, mm) -> str:
    r = _stats(mm["residuals"])
    hwh = _stats(mm["hw_h"])
    lwh = _stats(mm["lw_h"])
    rng = _stats(mm["ranges"])
    hwt = _stats(mm["hw_t"])
    lwt = _stats(mm["lw_t"])
    implied = sum(mm["pmg"]) / len(mm["pmg"]) if mm["pmg"] else float("nan")
    return (f"{label}  {days:>2}d  n={r['n']:>4}  "
            f"bias {r['mean']:+.3f}  RMS {r['rms']:.3f}  sd {r['std']:.3f}  "
            f"| HWh {hwh.get('rms', float('nan')):.3f}  LWh {lwh.get('rms', float('nan')):.3f}  "
            f"rng {rng.get('rms', float('nan')):.3f}  "
            f"| HWt sd {hwt.get('std', float('nan')):.1f}  LWt sd {lwt.get('std', float('nan')):.1f}  "
            f"| datum {implied:+.3f}")


def _block(title, xs, unit, sign_note="") -> str:
    st = _stats(xs)
    if st["n"] == 0:
        return f"  {title:<26} (no data)"
    return (f"  {title:<26} n={st['n']:<5} mean {st['mean']:+.3f}  "
            f"stdev {st['std']:.3f}  rms {st['rms']:.3f}  max|{st['maxabs']:.3f}| {unit} {sign_note}")


def main():
    ap = argparse.ArgumentParser(description="Full-corpus harmonic stress test vs measured Portsmouth sea level.")
    ap.add_argument("--out-dir", default=".", help="where to write cache/ and reports")
    ap.add_argument("--months", help="range 'YYYY-MM:YYYY-MM' inclusive (default Aug 2025..Jun 2026)")
    ap.add_argument("--ref", default="E71839")
    ap.add_argument("--no-barometric", action="store_true", help="skip the ERA5 pressure-corrected pass")
    ap.add_argument("--lat", type=float, default=PORTSMOUTH_LAT)
    ap.add_argument("--lon", type=float, default=PORTSMOUTH_LON)
    args = ap.parse_args()

    months = parse_months(args.months) if args.months else DEFAULT_MONTHS
    out_dir = os.path.abspath(args.out_dir)
    cache_dir = os.path.join(out_dir, "cache")
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, "stress_report.txt")
    json_path = os.path.join(out_dir, "stress_report.json")

    rf = open(report_path, "w", buffering=1)  # line-buffered

    def log(msg=""):
        print(msg, flush=True)
        rf.write(msg + "\n")
        rf.flush()

    started = datetime.now(timezone.utc)
    log("Harmonic model stress test vs measured Portsmouth sea level (EA archive E71839)")
    log("=" * 92)
    log(f"Corpus: {months[0][0]}-{months[0][1]:02d} .. {months[-1][0]}-{months[-1][1]:02d}  "
        f"({len(months)} months)   started {started:%Y-%m-%d %H:%M:%SZ}")
    log(f"Datum offset (mAOD->CD): +{DATUM_OFFSET_M:.2f} m   |   out-dir: {out_dir}")
    log("")

    metrics_by_month = {}
    sea_by_month = {}
    pooled = {"residuals": [], "pmg": [], "hw_t": [], "lw_t": [],
              "hw_h": [], "lw_h": [], "ranges": []}
    failures = []

    # -------- per-month pass (the slow, network-bound loop) --------
    for i, (y, m) in enumerate(months, 1):
        label = f"{y}-{m:02d}"
        days = calendar.monthrange(y, m)[1]
        log(f"[{i}/{len(months)}] {label}  ({days} days)")
        try:
            sea = get_month_sea(y, m, cache_dir, args.ref, log)
        except Exception as e:
            log(f"  FETCH FAILED: {e!r}")
            failures.append((label, repr(e)))
            log("")
            continue
        if len(sea) < 50:
            log(f"  only {len(sea)} readings -- skipping this month")
            failures.append((label, f"too few readings: {len(sea)}"))
            log("")
            continue
        mm = month_metrics(sea)
        metrics_by_month[label] = mm
        sea_by_month[label] = sea
        for key in pooled:
            pooled[key].extend(mm[key])
        log("  " + _fmt_month_row(label, days, mm).strip())
        log("")

    if not metrics_by_month:
        log("No months succeeded. Aborting.")
        rf.close()
        return 1

    # -------- per-month summary table --------
    log("=" * 92)
    log("PER-MONTH SUMMARY  (heights in m, timing sd in min; RMS unless noted)")
    log("  month     days   n     bias    RMS    sd   | HWh    LWh    rng   | HWt    LWt   | datum")
    log("-" * 92)
    for (y, m) in months:
        label = f"{y}-{m:02d}"
        if label in metrics_by_month:
            log("  " + _fmt_month_row(label, calendar.monthrange(y, m)[1], metrics_by_month[label]).strip())
    log("")

    # -------- combined corpus (raw / astronomical only) --------
    log("=" * 92)
    log("COMBINED CORPUS  (all months pooled, harmonic astronomical only)")
    total_days = sum(calendar.monthrange(y, m)[1] for (y, m) in months if f"{y}-{m:02d}" in metrics_by_month)
    log(f"  months used: {len(metrics_by_month)}   ~{total_days} days   "
        f"point samples: {len(pooled['residuals'])}")
    log("  A. Point-by-point height (predicted - measured):")
    log(_block("all states", pooled["residuals"], "m", "(+ => model reads high)"))
    implied = sum(pooled["pmg"]) / len(pooled["pmg"])
    log(f"     implied datum offset  = {implied:+.3f} m   (nominal {DATUM_OFFSET_M:.2f}; "
        f"gap {implied - DATUM_OFFSET_M:+.3f} m)")
    log("  B. HW/LW turning points:")
    log(_block("HW timing", pooled["hw_t"], "min", "(+ => model peak later)"))
    log(_block("LW timing", pooled["lw_t"], "min", "(+ => model peak later)"))
    log(_block("HW height", pooled["hw_h"], "m"))
    log(_block("LW height", pooled["lw_h"], "m"))
    log(_block("tidal range (HW-LW)", pooled["ranges"], "m", "(datum-independent)"))
    log("")

    # -------- optional barometric pass (one ERA5 fetch for the whole span) --------
    baro_summary = None
    if not args.no_barometric:
        log("=" * 92)
        log("COMBINED CORPUS  after v2.9 inverse-barometer correction (ERA5 pressure)")
        gmin = min(s[0][0] for s in sea_by_month.values())
        gmax = max(s[-1][0] for s in sea_by_month.values())
        log(f"  fetching ERA5 pressure {gmin:%Y-%m-%d}..{gmax:%Y-%m-%d} from Open-Meteo...")
        try:
            met = fetch_pressure_wind(args.lat, args.lon, gmin, gmax)
        except Exception as e:
            log(f"  pressure fetch failed: {e!r} -- skipping barometric pass")
            met = None
        if met:
            log(f"  pressure samples: {len(met)}")
            corr_res = []
            pressures = []
            for label, sea in sea_by_month.items():
                for dt, mAOD in sea:
                    mw = _interp(met, dt)
                    if mw is None:
                        continue
                    p, _w = mw
                    corr = correction_for_pressure(p)["correction_m"]
                    corr_res.append((predict_height_at_time(dt) + corr) - (mAOD + DATUM_OFFSET_M))
                    pressures.append(p)
            raw = _stats(pooled["residuals"])
            cs = _stats(corr_res)
            ps = _stats(pressures)
            log(f"  pressure range: {ps['min']:.1f}..{ps['max']:.1f} hPa (span {ps['max'] - ps['min']:.1f})")
            log(_block("corrected residual", corr_res, "m"))
            log(f"     vs uncorrected:  RMS {cs['rms']:.3f} (was {raw['rms']:.3f})   "
                f"stdev {cs['std']:.3f} (was {raw['std']:.3f})   "
                f"scatter explained by pressure: {raw['std'] - cs['std']:+.3f} m")
            baro_summary = {"n": cs["n"], "rms": cs["rms"], "std": cs["std"],
                            "mean": cs["mean"], "pressure_span_hpa": ps["max"] - ps["min"],
                            "raw_rms": raw["rms"], "raw_std": raw["std"]}
        log("")

    # -------- caveats --------
    log("Notes")
    log("-----")
    log("* Harmonic model is astronomical only; the gauge includes surge, so residual")
    log("  scatter is an UPPER bound on the model's own error.")
    log("* HW timing at Portsmouth is noisy (Solent HW 'stand'); the model publishes HW/LW")
    log("  34/28 min earlier than its math peak, so the RAW-peak timing MEAN carries that")
    log("  convention offset. The timing STDEV is the meaningful model-timing figure.")
    if failures:
        log(f"* Months not included: {', '.join(l for l, _ in failures)}")
    ended = datetime.now(timezone.utc)
    log(f"* Finished {ended:%Y-%m-%d %H:%M:%SZ}  (elapsed {(ended - started).total_seconds() / 60:.1f} min)")

    # -------- machine-readable dump --------
    def stat_or_none(xs):
        return _stats(xs) if xs else {"n": 0}
    report = {
        "corpus": [f"{y}-{m:02d}" for (y, m) in months],
        "months_used": sorted(metrics_by_month.keys()),
        "failures": failures,
        "combined": {
            "point": stat_or_none(pooled["residuals"]),
            "implied_datum_offset_m": implied,
            "hw_timing_min": stat_or_none(pooled["hw_t"]),
            "lw_timing_min": stat_or_none(pooled["lw_t"]),
            "hw_height_m": stat_or_none(pooled["hw_h"]),
            "lw_height_m": stat_or_none(pooled["lw_h"]),
            "range_m": stat_or_none(pooled["ranges"]),
            "barometric": baro_summary,
        },
        "per_month": {
            label: {
                "point": stat_or_none(mm["residuals"]),
                "implied_datum_offset_m": (sum(mm["pmg"]) / len(mm["pmg"])) if mm["pmg"] else None,
                "hw_height_m": stat_or_none(mm["hw_h"]),
                "lw_height_m": stat_or_none(mm["lw_h"]),
                "range_m": stat_or_none(mm["ranges"]),
                "hw_timing_min": stat_or_none(mm["hw_t"]),
                "lw_timing_min": stat_or_none(mm["lw_t"]),
            }
            for label, mm in metrics_by_month.items()
        },
    }
    with open(json_path, "w") as jf:
        json.dump(report, jf, indent=2, default=str)
    log(f"* Wrote {report_path} and {json_path}")
    rf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
