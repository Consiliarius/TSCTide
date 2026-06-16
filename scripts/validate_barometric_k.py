"""
Offline validation of the barometric (inverse-barometer) coefficient k.

v2.9 Session H. This script does NOT touch the prediction path or the
database; it is a standalone analysis tool, run via:

    docker exec -w /app tidal-access python -m scripts.validate_barometric_k --source ea --days 28
    docker exec -w /app tidal-access python -m scripts.validate_barometric_k --source csv --file app/calibration_data/bodc_portsmouth.csv

What it does
------------
Estimates the empirical inverse-barometer coefficient k (metres of sea level
per hPa of pressure deviation) by regressing

    residual = measured_sea_level - astronomical_prediction

against the pressure deviation (P_ref - P), over a span of real measured sea
level at Portsmouth. The slope of that regression IS k; the gauge-vs-prediction
datum offset (gauge mAOD vs harmonic metres-above-Chart-Datum) and any constant
instrument bias fall into the regression INTERCEPT, so the slope is identifiable
without aligning datums. See docs/V2.9_BAROMETRIC_DESIGN.md section 6.

Data sources
------------
* Measured sea level at Portsmouth:
    - "ea": Defra/EA flood-monitoring Tide Gauge API (station E71839, mAOD,
      15-min). Rolling ~4 weeks only -- enough to validate the PIPELINE and
      get a preliminary number, NOT enough pressure range for a definitive k.
    - "csv": a normalised CSV exported from the BODC processed archive
      (multi-year). This is the definitive input. Format documented in
      app/calibration_data/BODC_PORTSMOUTH_README.md -- two columns:
          timestamp_utc,sea_level_m
* Astronomical prediction: app.harmonic.predict_height_at_time (Portsmouth,
  pre-secondary-port).
* Historical pressure + wind: Open-Meteo ERA5 archive (pressure_msl,
  wind_speed_10m, m/s), free, no key.

Why the harmonic model's own error does not bias k: the harmonic RMS (~0.2 m)
and its biases are not correlated with barometric pressure, so they add scatter
(lower R^2) but leave the slope unbiased. The real confound is storm surge,
which co-varies with low pressure -- hence the optional --wind-max filter and
the standing recommendation to fit against a multi-year archive.
"""

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import httpx

from app.harmonic import predict_height_at_time

EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EA_ARCHIVE = "https://environment.data.gov.uk/flood-monitoring/archive"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# Portsmouth tide-gauge location (for the regional pressure field). The exact
# point is immaterial -- the inverse-barometer effect is regional.
PORTSMOUTH_LAT = 50.80
PORTSMOUTH_LON = -1.11

DEFAULT_P_REF = 1013.25
DEFAULT_K_PRIOR = 0.00882
DEFAULT_MAX_CORRECTION_M = 0.30


def _parse_utc(s: str) -> datetime:
    """Parse an ISO timestamp to an aware UTC datetime. Accepts trailing 'Z'
    and naive strings (assumed UTC)."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Sea-level sources
# --------------------------------------------------------------------------

def load_sea_level_ea(days: int, ref: str = "E71839") -> list[tuple[datetime, float]]:
    """Fetch the last `days` days of 15-min sea level (metres) from the EA
    tide-gauge API for the given station reference. Returns (utc_dt, value)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{EA_BASE}/id/stations/{ref}/readings"
    r = httpx.get(url, params={"since": since, "_sorted": "", "_limit": 100000}, timeout=60.0)
    r.raise_for_status()
    items = r.json().get("items", [])
    out = []
    for it in items:
        ts = it.get("dateTime")
        val = it.get("value")
        if ts is None or val is None:
            continue
        # The API occasionally returns a list for value on multi-measure rows.
        if isinstance(val, list):
            val = val[0]
        try:
            out.append((_parse_utc(ts), float(val)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def load_sea_level_ea_archive(start_date: str, end_date: str, ref: str = "E71839"
                              ) -> list[tuple[datetime, float]]:
    """Pull historical 15-min sea level from the EA daily archive dumps over
    [start_date, end_date] inclusive (YYYY-MM-DD). Each daily file is an
    all-station dump (~0.5M rows, short format `dateTime,measure,value`); we
    stream it and keep only the rows for the given station's tidal_level
    measure. Slow over long spans (large files) but needs no registration and
    reaches back years -- this replaces the manual BODC download. Missing or
    not-yet-generated days (404) are skipped."""
    d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_date, "%Y-%m-%d").date()
    out: list[tuple[datetime, float]] = []
    day = d0
    files = 0
    while day <= d1:
        url = f"{EA_ARCHIVE}/readings-{day.isoformat()}.csv"
        try:
            with httpx.stream("GET", url, timeout=180.0) as r:
                if r.status_code != 200:
                    day += timedelta(days=1)
                    continue
                for i, line in enumerate(r.iter_lines()):
                    if i == 0 or ref not in line or "tidal_level" not in line:
                        continue
                    parts = line.split(",")
                    if len(parts) < 3:
                        continue
                    try:
                        out.append((_parse_utc(parts[0]), float(parts[-1])))
                    except (ValueError, TypeError):
                        continue
            files += 1
        except Exception as e:
            print(f"  (archive {day} failed: {e!r})")
        day += timedelta(days=1)
    out.sort(key=lambda x: x[0])
    print(f"  fetched {files} daily archive file(s)")
    return out


def load_sea_level_csv(path: str) -> list[tuple[datetime, float]]:
    """Load a normalised BODC export: header `timestamp_utc,sea_level_m`,
    one reading per line. Datum is irrelevant to the slope."""
    import csv
    out = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp_utc") or row.get("timestamp")
            val = row.get("sea_level_m") or row.get("sea_level")
            if not ts or val in (None, ""):
                continue
            try:
                out.append((_parse_utc(ts), float(val)))
            except (ValueError, TypeError):
                continue
    out.sort(key=lambda x: x[0])
    return out


# --------------------------------------------------------------------------
# Pressure + wind (Open-Meteo ERA5 archive), chunked by year
# --------------------------------------------------------------------------

def fetch_pressure_wind(lat: float, lon: float, start: datetime, end: datetime
                        ) -> list[tuple[datetime, float, float]]:
    """Return sorted (utc_dt, pressure_msl_hpa, wind_ms) hourly samples over
    [start, end], fetched in <=1-year chunks to stay within API limits."""
    samples: list[tuple[datetime, float, float]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=365), end)
        r = httpx.get(OPEN_METEO_ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "start_date": chunk_start.strftime("%Y-%m-%d"),
            "end_date": chunk_end.strftime("%Y-%m-%d"),
            "hourly": "pressure_msl,wind_speed_10m",
            "wind_speed_unit": "ms",
            "timezone": "GMT",
        }, timeout=120.0)
        r.raise_for_status()
        h = r.json().get("hourly", {})
        times = h.get("time", []) or []
        pmsl = h.get("pressure_msl", []) or []
        wind = h.get("wind_speed_10m", []) or []
        for i, t in enumerate(times):
            p = pmsl[i] if i < len(pmsl) else None
            w = wind[i] if i < len(wind) else None
            if p is None:
                continue
            samples.append((_parse_utc(t), float(p), float(w) if w is not None else float("nan")))
        chunk_start = chunk_end + timedelta(days=1)
    samples.sort(key=lambda x: x[0])
    return samples


def _interp(series: list[tuple[datetime, float, float]], when: datetime):
    """Linear-interpolate (pressure, wind) at `when` from sorted hourly series.
    Returns None if `when` is outside the series coverage."""
    if not series or when < series[0][0] or when > series[-1][0]:
        return None
    # Binary search for the bracketing pair.
    lo, hi = 0, len(series) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= when:
            lo = mid
        else:
            hi = mid
    t0, p0, w0 = series[lo]
    t1, p1, w1 = series[hi]
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return p0, w0
    f = (when - t0).total_seconds() / span
    return p0 + (p1 - p0) * f, w0 + (w1 - w0) * f


# --------------------------------------------------------------------------
# Ordinary least squares (pure Python, no numpy dependency)
# --------------------------------------------------------------------------

def ols(points: list[tuple[float, float]]):
    """Fit y = intercept + slope*x. Returns dict with slope, intercept, r2,
    slope_se, n. `points` is a list of (x, y)."""
    n = len(points)
    if n < 3:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    xbar = sx / n
    ybar = sy / n
    sxx = sum((p[0] - xbar) ** 2 for p in points)
    sxy = sum((p[0] - xbar) * (p[1] - ybar) for p in points)
    if sxx == 0:
        return None
    slope = sxy / sxx
    intercept = ybar - slope * xbar
    ss_res = sum((p[1] - (intercept + slope * p[0])) ** 2 for p in points)
    ss_tot = sum((p[1] - ybar) ** 2 for p in points)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    # Standard error of the slope.
    slope_se = math.sqrt((ss_res / (n - 2)) / sxx) if n > 2 else float("nan")
    return {"slope": slope, "intercept": intercept, "r2": r2,
            "slope_se": slope_se, "n": n}


def _report(label: str, fit, k_prior: float, max_corr: float):
    if fit is None:
        print(f"  {label}: insufficient data")
        return
    k = fit["slope"]
    pct = (k - k_prior) / k_prior * 100 if k_prior else float("nan")
    clamp_hpa = max_corr / abs(k) if k else float("nan")
    print(f"  {label} (n={fit['n']}):")
    print(f"    k (slope)  = {k:+.5f} m/hPa  (SE {fit['slope_se']:.5f})")
    print(f"    intercept  = {fit['intercept']:+.3f} m   (datum/bias offset)")
    print(f"    R^2        = {fit['r2']:.3f}")
    print(f"    vs prior   = {pct:+.1f}% of {k_prior:.5f}")
    print(f"    => clamp {max_corr:.2f} m reached at +/-{clamp_hpa:.0f} hPa from reference")


def main():
    ap = argparse.ArgumentParser(description="Validate barometric coefficient k against measured sea level.")
    ap.add_argument("--source", choices=["ea", "ea-archive", "csv"], default="ea")
    ap.add_argument("--file", help="CSV path for --source csv (timestamp_utc,sea_level_m)")
    ap.add_argument("--days", type=int, default=28, help="EA lookback window (rolling 4-week max)")
    ap.add_argument("--start", help="ea-archive start date YYYY-MM-DD")
    ap.add_argument("--end", help="ea-archive end date YYYY-MM-DD")
    ap.add_argument("--ref", default="E71839", help="EA station reference (default E71839 = Portsmouth mAOD)")
    ap.add_argument("--lat", type=float, default=PORTSMOUTH_LAT)
    ap.add_argument("--lon", type=float, default=PORTSMOUTH_LON)
    ap.add_argument("--p-ref", type=float, default=DEFAULT_P_REF)
    ap.add_argument("--k-prior", type=float, default=DEFAULT_K_PRIOR)
    ap.add_argument("--max-correction-m", type=float, default=DEFAULT_MAX_CORRECTION_M)
    ap.add_argument("--wind-max", type=float, default=8.0,
                    help="Also report a fit excluding samples with wind above this (m/s); 0 disables")
    args = ap.parse_args()

    print("Barometric coefficient validation (v2.9 Session H)")
    print("=" * 60)

    if args.source == "ea":
        print(f"Sea level: EA gauge {args.ref} (mAOD), last {args.days} days")
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
    print(f"Sea-level readings: {len(sea)} ({sea[0][0]:%Y-%m-%d} .. {sea[-1][0]:%Y-%m-%d})")

    print("Fetching ERA5 pressure + wind from Open-Meteo archive...")
    met = fetch_pressure_wind(args.lat, args.lon, sea[0][0], sea[-1][0])
    if not met:
        print("No pressure data returned. Aborting.")
        return 1
    print(f"Pressure/wind samples: {len(met)} ({met[0][0]:%Y-%m-%d} .. {met[-1][0]:%Y-%m-%d})")

    # Build regression points.
    all_pts: list[tuple[float, float]] = []
    calm_pts: list[tuple[float, float]] = []
    pressures, winds = [], []
    matched = 0
    for when, measured in sea:
        mw = _interp(met, when)
        if mw is None:
            continue
        p, w = mw
        predicted = predict_height_at_time(when)
        residual = measured - predicted          # y
        x = args.p_ref - p                        # pressure deviation
        all_pts.append((x, residual))
        pressures.append(p)
        winds.append(w)
        if args.wind_max and not math.isnan(w) and w <= args.wind_max:
            calm_pts.append((x, residual))
        matched += 1

    if matched < 50:
        print(f"Only {matched} readings matched with pressure -- too few. Aborting.")
        return 1

    p_lo, p_hi = min(pressures), max(pressures)
    print(f"Matched: {matched} | pressure range {p_lo:.1f}..{p_hi:.1f} hPa (span {p_hi - p_lo:.1f})")
    finite_w = [w for w in winds if not math.isnan(w)]
    if finite_w:
        print(f"Wind range: {min(finite_w):.1f}..{max(finite_w):.1f} m/s")
    print()

    _report("All points", ols(all_pts), args.k_prior, args.max_correction_m)
    if args.wind_max:
        print()
        _report(f"Wind <= {args.wind_max:.1f} m/s", ols(calm_pts), args.k_prior, args.max_correction_m)

    print()
    span_days = (sea[-1][0] - sea[0][0]).days
    if span_days < 120 or (p_hi - p_lo) < 30:
        print("CAVEAT: short span / narrow pressure range. Surge co-varies with low")
        print("pressure and independent weather systems are few, so this slope is")
        print("PRELIMINARY -- it validates the pipeline, not the coefficient. For a")
        print("definitive fit, run a long span via --source ea-archive --start ... --end ...")
        print("(>= 6-12 months, to span deep lows and strong highs).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
