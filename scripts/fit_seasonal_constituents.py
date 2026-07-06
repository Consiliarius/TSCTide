"""
Fit the annual (Sa) and semiannual (Ssa) tidal constituents from a long
monthly-mean sea-level record, in the harmonic model's own phase convention.

Motivation
----------
The year-long validation (docs/HARMONIC_VALIDATION_2026-07.md) found the model's
dominant residual is a seasonal mean-sea-level cycle (bias −0.04 m in spring to
−0.29 m in January) that the current Sa/Ssa constituents under-capture. Sa and Ssa
are long-period (annual / semiannual) and are, physically, mostly the radiational
+ steric seasonal MSL cycle — so the right way to set them is to fit a *many-year*
observed monthly-mean record, which averages out interannual weather and leaves the
repeatable seasonal cycle.

Data source
-----------
PSMSL Portsmouth, station 350, RLR monthly means (open, no login), the longest
public monthly record for the harbour's standard port (1961–present). Value is
mm above the station's Revised Local Reference; the RLR datum is stable across the
whole record by construction, so the seasonal *variation* (what we fit) is clean.
The absolute datum is irrelevant — it folds into the model's Z0, which is unchanged.

Method
------
Ordinary least squares of monthly MSL against, at each month's mid-point time:

    const + trend·t + [Nodal: cosN, sinN] + [Sa: cos h, sin h] + [Ssa: cos 2h, sin 2h]

where h is the Sun's mean longitude and N the lunar node longitude, taken from the
model's own app.harmonic._astro so the recovered phases are in the model convention
(Sa contribution = A·cos(h − g), matching predict_height_at_time). The trend absorbs
secular sea-level rise; the nodal pair absorbs the 18.6-yr modulation; both keep the
annual/semiannual estimates clean. Amplitudes are corrected for the ~1–5% attenuation
from monthly averaging (sinc of the boxcar). Pure-Python solve, no numpy.

Validation
----------
With --corpus-cache-dir pointing at the cached EA Portsmouth gauge months from the
stress test, the script recomputes the model's per-month bias with the CURRENT vs the
NEWLY-FITTED Sa/Ssa (in-process override, no file edit) — showing whether the seasonal
bias actually collapses on that independent 2025–26 corpus.

Usage
-----
    python -m scripts.fit_seasonal_constituents \
        --corpus-cache-dir <scratchpad>/cache
"""

import argparse
import calendar
import math
import sys
from datetime import datetime, timedelta, timezone

import httpx

import app.harmonic
from app.harmonic import _astro, predict_height_at_time
from app.config import get_harmonics
from scripts.validate_barometric_k import load_sea_level_csv
from scripts.validate_harmonic_vs_measured import DATUM_OFFSET_M

PSMSL_URL = "https://psmsl.org/data/obtaining/rlr.monthly.data/350.rlrdata"
MISSING = -1000.0  # PSMSL missing code is -99999; any negative mm is missing here


def fetch_psmsl(url: str) -> list[tuple[float, float]]:
    """Return [(decimal_year, msl_mm)] of valid monthly means from a PSMSL
    .rlrdata file (semicolon-delimited: year; value_mm; missing_days; flag)."""
    r = httpx.get(url, timeout=60.0)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue
        try:
            dy = float(parts[0])
            val = float(parts[1])
        except ValueError:
            continue
        if val < MISSING:
            continue
        out.append((dy, val))
    return out


def dy_to_dt(dy: float) -> datetime:
    """PSMSL decimal year -> UTC datetime at the corresponding point in the year."""
    year = int(math.floor(dy))
    frac = dy - year
    days = 366 if calendar.isleap(year) else 365
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=frac * days)


def solve(ata: list[list[float]], atb: list[float]) -> list[float]:
    """Solve a small symmetric normal-equation system by Gaussian elimination
    with partial pivoting. Returns the coefficient vector."""
    n = len(atb)
    m = [row[:] + [atb[i]] for i, row in enumerate(ata)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        pv = m[c][c]
        if abs(pv) < 1e-15:
            raise ValueError("singular normal equations")
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / pv
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _sinc(x: float) -> float:
    """Normalised sinc: sin(pi x)/(pi x)."""
    if x == 0:
        return 1.0
    return math.sin(math.pi * x) / (math.pi * x)


def fit_constituents(rows: list[tuple[float, float]]) -> dict:
    """OLS fit; returns fitted Sa/Ssa amplitude (m) + phase (deg, model convention),
    plus trend, nodal amplitude, residual RMS and R^2."""
    mean_dy = sum(dy for dy, _ in rows) / len(rows)
    X, y = [], []
    for dy, val in rows:
        dt = dy_to_dt(dy)
        _tau, _s, h, _p, N, _p1 = _astro(dt)
        hr = math.radians(h)
        h2r = math.radians(2 * h)
        Nr = math.radians(N)
        X.append([
            1.0, dy - mean_dy,
            math.cos(Nr), math.sin(Nr),
            math.cos(hr), math.sin(hr),
            math.cos(h2r), math.sin(h2r),
        ])
        y.append(val)

    n_col = 8
    ata = [[0.0] * n_col for _ in range(n_col)]
    atb = [0.0] * n_col
    for xi, yi in zip(X, y):
        for a in range(n_col):
            atb[a] += xi[a] * yi
            for b in range(n_col):
                ata[a][b] += xi[a] * xi[b]
    coef = solve(ata, atb)

    # Residuals / goodness.
    ybar = sum(y) / len(y)
    ss_tot = sum((yi - ybar) ** 2 for yi in y)
    ss_res = 0.0
    for xi, yi in zip(X, y):
        pred = sum(c * v for c, v in zip(coef, xi))
        ss_res += (yi - pred) ** 2
    rms = math.sqrt(ss_res / len(y))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    # Extract Sa (cos h, sin h) and Ssa (cos 2h, sin 2h) with monthly-average
    # (sinc) amplitude correction. A = hypot(C, S); g = atan2(S, C).
    def amp_phase(ccoef, scoef, sinc_k):
        A = math.hypot(ccoef, scoef) / _sinc(sinc_k)
        g = math.degrees(math.atan2(scoef, ccoef)) % 360.0
        return A, g

    A_sa_mm, g_sa = amp_phase(coef[4], coef[5], 1.0 / 12.0)
    A_ssa_mm, g_ssa = amp_phase(coef[6], coef[7], 2.0 / 12.0)
    nodal_mm = math.hypot(coef[2], coef[3])

    return {
        "n": len(rows),
        "trend_mm_yr": coef[1],
        "nodal_amp_mm": nodal_mm,
        "sa_amp_m": A_sa_mm / 1000.0,
        "sa_phase_deg": g_sa,
        "ssa_amp_m": A_ssa_mm / 1000.0,
        "ssa_phase_deg": g_ssa,
        "resid_rms_mm": rms,
        "r2": r2,
    }


def observed_climatology(rows, fit):
    """Monthly (calendar) climatology of the *seasonal* part: observed minus
    (const+trend+nodal), grouped by month. Returns dict month->mean_m (anomaly)."""
    # Rebuild the non-seasonal baseline per point and take observed - baseline,
    # then group by calendar month. Baseline uses the same fitted trend/nodal.
    # We do not have coef here, so approximate the seasonal anomaly as observed
    # minus its own annual running mean is overkill; instead group raw and
    # subtract the grand mean + linear trend numerically.
    mean_dy = sum(dy for dy, _ in rows) / len(rows)
    ys = [v for _, v in rows]
    grand = sum(ys) / len(ys)
    # crude detrend using the fitted trend (mm/yr)
    trend = fit["trend_mm_yr"]
    buckets = {m: [] for m in range(1, 13)}
    for dy, val in rows:
        dt = dy_to_dt(dy)
        anom = (val - grand - trend * (dy - mean_dy)) / 1000.0
        buckets[dt.month].append(anom)
    return {m: (sum(v) / len(v) if v else float("nan")) for m, v in buckets.items()}


def model_seasonal(amp_sa, g_sa, amp_ssa, g_ssa):
    """Seasonal cycle (m) the model produces for each calendar month, from Sa/Ssa
    only, averaged over a representative span of years (2020–2025)."""
    out = {}
    for m in range(1, 13):
        vals = []
        for yr in range(2020, 2026):
            dt = datetime(yr, m, 15, tzinfo=timezone.utc)
            _t, _s, h, _p, _N, _p1 = _astro(dt)
            v = amp_sa * math.cos(math.radians(h) - math.radians(g_sa)) \
                + amp_ssa * math.cos(math.radians(2 * h) - math.radians(g_ssa))
            vals.append(v)
        out[m] = sum(vals) / len(vals)
    # express as anomaly from the 12-month mean
    mean = sum(out.values()) / 12
    return {m: out[m] - mean for m in out}


def validate_corpus(cache_dir, new_harmonics):
    """Per-month model bias on cached EA Portsmouth months, current vs new Sa/Ssa."""
    import glob
    import os
    paths = sorted(glob.glob(os.path.join(cache_dir, "portsmouth_*.csv")))
    if not paths:
        return None

    def label(p):
        return os.path.basename(p).replace("portsmouth_", "").replace(".csv", "")

    # Pass 1: current constituents.
    per_old, old_all = {}, []
    seas = {}
    for p in paths:
        sea = load_sea_level_csv(p)
        seas[p] = sea
        res = [predict_height_at_time(dt) - (v + DATUM_OFFSET_M) for dt, v in sea]
        per_old[label(p)] = sum(res) / len(res)
        old_all.extend(res)

    # Pass 2: override get_harmonics in-process (no file edit), recompute.
    orig = app.harmonic.get_harmonics
    app.harmonic.get_harmonics = lambda default=None: new_harmonics
    try:
        per_new, new_all = {}, []
        for p in paths:
            sea = seas[p]
            res = [predict_height_at_time(dt) - (v + DATUM_OFFSET_M) for dt, v in sea]
            per_new[label(p)] = sum(res) / len(res)
            new_all.extend(res)
    finally:
        app.harmonic.get_harmonics = orig

    def rms(xs):
        return math.sqrt(sum(x * x for x in xs) / len(xs))

    return {
        "labels": [label(p) for p in paths],
        "per_old": per_old, "per_new": per_new,
        "old_mean": sum(old_all) / len(old_all), "new_mean": sum(new_all) / len(new_all),
        "old_rms": rms(old_all), "new_rms": rms(new_all),
    }


def main():
    ap = argparse.ArgumentParser(description="Fit Sa/Ssa from PSMSL Portsmouth monthly means.")
    ap.add_argument("--url", default=PSMSL_URL)
    ap.add_argument("--corpus-cache-dir", help="dir of cached portsmouth_YYYY-MM.csv for validation")
    args = ap.parse_args()

    print("Sa/Ssa seasonal-constituent fit — PSMSL Portsmouth station 350")
    print("=" * 64)
    rows = fetch_psmsl(args.url)
    if len(rows) < 60:
        print(f"Only {len(rows)} valid monthly values — too few.")
        return 1
    yr0, yr1 = rows[0][0], rows[-1][0]
    print(f"Valid monthly means: {len(rows)}  ({yr0:.2f} .. {yr1:.2f}, {yr1 - yr0:.1f} yr span)")

    fit = fit_constituents(rows)
    cur = get_harmonics(app.harmonic.HARMONICS)
    sa_cur = cur.get("SA", (0.074, 186.7))
    ssa_cur = cur.get("SSA", (0.045, 5.3))

    print(f"Secular trend: {fit['trend_mm_yr']:+.2f} mm/yr   "
          f"nodal(18.6yr) amp: {fit['nodal_amp_mm']:.1f} mm   "
          f"fit resid RMS: {fit['resid_rms_mm']:.1f} mm   R^2: {fit['r2']:.3f}")
    print()
    print("Constituent      amplitude (m)      phase (deg)")
    print(f"  Sa  current      {sa_cur[0]:.4f}            {sa_cur[1]:.1f}")
    print(f"  Sa  fitted       {fit['sa_amp_m']:.4f}            {fit['sa_phase_deg']:.1f}")
    print(f"  Ssa current      {ssa_cur[0]:.4f}            {ssa_cur[1]:.1f}")
    print(f"  Ssa fitted       {fit['ssa_amp_m']:.4f}            {fit['ssa_phase_deg']:.1f}")
    print()

    # Climatology comparison.
    obs = observed_climatology(rows, fit)
    old_m = model_seasonal(sa_cur[0], sa_cur[1], ssa_cur[0], ssa_cur[1])
    new_m = model_seasonal(fit["sa_amp_m"], fit["sa_phase_deg"],
                           fit["ssa_amp_m"], fit["ssa_phase_deg"])
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    print("Monthly seasonal cycle (m, anomaly from annual mean):")
    print("  month   observed   old-model   new-model")
    for m in range(1, 13):
        print(f"   {names[m-1]}     {obs[m]:+.3f}      {old_m[m]:+.3f}      {new_m[m]:+.3f}")
    print()

    if args.corpus_cache_dir:
        new_h = dict(cur)
        new_h["SA"] = (round(fit["sa_amp_m"], 4), round(fit["sa_phase_deg"], 1))
        new_h["SSA"] = (round(fit["ssa_amp_m"], 4), round(fit["ssa_phase_deg"], 1))
        v = validate_corpus(args.corpus_cache_dir, new_h)
        if v:
            print("Validation on cached EA 2025-26 corpus (mean model bias, m):")
            print("  month     current    new")
            for lab in v["labels"]:
                print(f"  {lab}   {v['per_old'][lab]:+.3f}    {v['per_new'][lab]:+.3f}")
            print(f"  ALL        {v['old_mean']:+.3f}    {v['new_mean']:+.3f}   "
                  f"(point RMS {v['old_rms']:.3f} -> {v['new_rms']:.3f})")
        else:
            print(f"(no portsmouth_*.csv found in {args.corpus_cache_dir})")
    print()
    print("Note: fitted values are ready to drop into model_config.json "
          "harmonic_reference.constituents (SA / SSA). Not written by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
