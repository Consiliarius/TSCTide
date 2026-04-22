"""
Harmonic tidal prediction model for Portsmouth.

Uses full astronomical argument computation with Doodson numbers and nodal
corrections. Constituents and phases calibrated against KHM Portsmouth data.
Secondary port offset to Langstone must be applied after computation.

Accuracy (vs 355 Admiralty HW + 355 LW reference points, Jul 2026 – Dec 2027):
  HW timing stdev: ~15 min     HW height stdev: ~0.13 m
  LW timing stdev: ~19 min     LW height stdev: ~0.19 m
Published HW/LW times in predict_events() are shifted to match the Admiralty
convention (stand centre, not mathematical peak); see HW_ADMIRALTY_OFFSET_MINUTES.
"""

import math
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Mean level above Chart Datum (m)
Z0 = 2.8846

# Constituent speeds (degrees/hour)
SPEEDS = {
    "M2": 28.9841042, "S2": 30.0, "N2": 28.4397295, "K2": 30.0821373,
    "K1": 15.0410686, "O1": 13.9430356, "P1": 14.9589314, "Q1": 13.3986609,
    "M4": 57.9682084, "MS4": 58.9841042, "MN4": 57.4238337, "M6": 86.9523127,
    "2N2": 27.8953548, "MU2": 27.9682084, "NU2": 28.5125831, "L2": 29.5284789,
    "T2": 29.9589333, "SA": 0.0410686, "SSA": 0.0821373,
}

# Doodson multipliers: (n_tau, n_s, n_h, n_p, n_N', n_p1, phase_correction)
DOODSON = {
    "M2": (2, 0, 0, 0, 0, 0, 180), "S2": (2, 2, -2, 0, 0, 0, 180),
    "N2": (2, -1, 0, 1, 0, 0, 180), "K2": (2, 2, 0, 0, 0, 0, 180),
    "K1": (1, 1, 0, 0, 0, 0, 90), "O1": (1, -1, 0, 0, 0, 0, -90),
    "P1": (1, 1, -2, 0, 0, 0, -90), "Q1": (1, -2, 0, 1, 0, 0, -90),
    "M4": (4, 0, 0, 0, 0, 0, 0), "MS4": (4, 2, -2, 0, 0, 0, 0),
    "MN4": (4, -1, 0, 1, 0, 0, 0), "M6": (6, 0, 0, 0, 0, 0, 180),
    "2N2": (2, -2, 0, 2, 0, 0, 180), "MU2": (2, -2, 2, 0, 0, 0, 180),
    "NU2": (2, -1, 0, 1, 0, 0, 180), "L2": (2, 1, 0, -1, 0, 0, 0),
    "T2": (2, 2, -3, 0, 0, 1, 180), "SA": (0, 0, 1, 0, 0, 0, 0),
    "SSA": (0, 0, 2, 0, 0, 0, 0),
}

# Harmonic constants for Portsmouth (amplitude in metres, phase lag in degrees).
# Calibrated April 2026 against Admiralty reference data:
#   - 91 HW/LW points spanning May-December 2026
#   - 7 additional spring HW reference points
#   - 288 half-hourly points across April 21-26, 2026 (6 full tidal cycles)
# Total: 388 reference points over 8 months
# Fit quality:
#   - Overall height RMS: 0.22m (half-hourly: 0.10m; HW/LW: 0.39m)
#   - HW timing stdev: 17 min
#   - LW timing stdev: 19 min
# HW/LW RMS appears larger because it's dominated by the Solent HW stand
# (mathematical peak differs slightly from published HW time).
HARMONICS = {
    "M2": (1.464, 152.5), "S2": (0.372, 186.2), "N2": (0.204, 122.5), "K2": (0.139, 229.5),
    "K1": (0.085, 249.0), "O1": (0.050, 331.7), "P1": (0.045, 353.4), "Q1": (0.023, 164.0),
    "M4": (0.169, 32.0), "MS4": (0.109, 84.0), "MN4": (0.039, 315.9), "M6": (0.060, 311.9),
    "2N2": (0.027, 346.9), "MU2": (0.060, 19.8), "NU2": (0.034, 155.7), "L2": (0.075, 174.1),
    "T2": (0.045, 98.2), "SA": (0.074, 186.7), "SSA": (0.045, 5.3),
}


def _jd(dt: datetime) -> float:
    """Julian Date from datetime."""
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3
    return (
        dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400
        - 32045 + (dt.hour + dt.minute / 60.0 + dt.second / 3600.0) / 24.0 - 0.5
    )


def _astro(dt: datetime) -> tuple:
    """Compute astronomical arguments (degrees)."""
    JD = _jd(dt)
    T = (JD - 2451545.0) / 36525.0
    D = JD - 2451545.0
    theta = 280.46061837 + 360.98564736629 * D
    s = 218.3165 + 481267.8813 * T
    h = 280.4661 + 36000.7698 * T
    p = 83.3532 + 4069.0137 * T
    N = 125.0445 - 1934.1363 * T
    p1 = 282.9404 + 1.7195 * T
    tau = theta - s
    return tau, s, h, p, N, p1


def _nodal(dt: datetime) -> tuple:
    """Compute nodal corrections f (amplitude) and u (phase) for each constituent."""
    T = (_jd(dt) - 2451545.0) / 36525.0
    N = math.radians((125.0445 - 1934.1363 * T) % 360)
    cN, sN = math.cos(N), math.sin(N)
    fM2 = 1.0 - 0.037 * cN
    uM2 = -2.1 * sN

    f = {
        "M2": fM2, "S2": 1.0, "N2": fM2, "K2": 1.024 - 0.286 * cN,
        "K1": 1.006 + 0.115 * cN, "O1": 1.009 + 0.187 * cN, "P1": 1.0,
        "Q1": 1.009 + 0.187 * cN, "M4": fM2 ** 2, "MS4": fM2, "MN4": fM2 ** 2,
        "M6": fM2 ** 3, "2N2": fM2, "MU2": fM2, "NU2": fM2,
        "L2": 1.0 - 0.025 * cN, "T2": 1.0, "SA": 1.0, "SSA": 1.0,
    }
    u = {
        "M2": uM2, "S2": 0, "N2": uM2, "K2": -17.74 * sN,
        "K1": -8.86 * sN, "O1": 10.8 * sN, "P1": 0, "Q1": 10.8 * sN,
        "M4": 2 * uM2, "MS4": uM2, "MN4": 2 * uM2, "M6": 3 * uM2,
        "2N2": uM2, "MU2": uM2, "NU2": uM2, "L2": 0, "T2": 0, "SA": 0, "SSA": 0,
    }
    return f, u


def predict_height_at_time(dt: datetime) -> float:
    """Predict tide height at a specific datetime (UTC). Returns metres above CD."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tau, s, h, p, N, p1 = _astro(dt)
    f, u = _nodal(dt)
    height = Z0
    for name, (amp, g) in HARMONICS.items():
        d = DOODSON.get(name)
        if not d:
            continue
        V = d[0] * tau + d[1] * s + d[2] * h + d[3] * p + d[4] * (-N) + d[5] * p1 + d[6]
        height += f.get(name, 1.0) * amp * math.cos(math.radians(V + u.get(name, 0) - g))
    return height


def _refine(times, heights, i):
    """Refine a turning point using quadratic interpolation."""
    dt_s = (times[i] - times[i - 1]).total_seconds()
    y0, y1, y2 = heights[i - 1], heights[i], heights[i + 1]
    denom = 2 * (2 * y1 - y0 - y2)
    if abs(denom) < 1e-10:
        return times[i], heights[i]
    off = (y0 - y2) / denom
    return times[i] + timedelta(seconds=off * dt_s), y1 + 0.25 * (y0 - y2) * off


# Admiralty convention offsets (applied in predict_events).
# The mathematical peak of the harmonic curve occurs LATER than the Admiralty's
# published HW/LW time. Analysis against 355 HW and 355 LW reference points
# spanning Jul 2026 – Dec 2027 showed consistent offsets:
#   HW: mathematical peak is ~34.4 min later than Admiralty published time
#   LW: mathematical trough is ~27.5 min later than Admiralty published time
# These offsets are subtracted from predict_events output to align with the
# convention users see in Admiralty, UKHO, and KHM data. Heights are correct
# throughout the curve (mean ~0m bias, stdev 0.13m HW / 0.19m LW) and are not
# affected. After correction: HW timing stdev 15min, LW stdev 19min across the
# 710-point validation set.
HW_ADMIRALTY_OFFSET_MINUTES = 34
LW_ADMIRALTY_OFFSET_MINUTES = 28


def predict_events(start: datetime, end: datetime, step_min: int = 6) -> list[dict]:
    """
    Predict HW and LW events between start and end.
    Returns list of dicts with timestamp, height_m, event_type.
    All predictions are for Portsmouth — apply secondary port offset for Langstone.

    Timings are shifted to match Admiralty published HW/LW convention (which
    uses the stand centre rather than the mathematical peak). To widen the
    search window so that events close to `start`/`end` are still captured
    after shifting, the internal computation extends past each end by the
    larger of the HW/LW offset.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    # Extend computation range so events inside [start, end] post-shift are found.
    # Since we shift events EARLIER, the window must extend past `end` by the offset.
    max_offset = max(HW_ADMIRALTY_OFFSET_MINUTES, LW_ADMIRALTY_OFFSET_MINUTES)
    compute_start = start - timedelta(minutes=max_offset)
    compute_end = end + timedelta(minutes=max_offset)

    step = timedelta(minutes=step_min)
    times = []
    heights = []
    dt = compute_start
    while dt <= compute_end:
        times.append(dt)
        heights.append(predict_height_at_time(dt))
        dt += step

    if len(heights) < 3:
        return []

    # Find turning points
    raw = []
    for i in range(1, len(heights) - 1):
        if heights[i] > heights[i - 1] and heights[i] > heights[i + 1]:
            t, h = _refine(times, heights, i)
            raw.append(("HighWater", t, round(h, 2)))
        elif heights[i] < heights[i - 1] and heights[i] < heights[i + 1]:
            t, h = _refine(times, heights, i)
            raw.append(("LowWater", t, round(h, 2)))

    # Filter spurious events from double-HW / stand effects
    filtered = [raw[0]] if raw else []
    for i in range(1, len(raw)):
        pt, pp_t, pp_h = filtered[-1]
        ct, c_t, c_h = raw[i]
        gap = abs((c_t - pp_t).total_seconds()) / 3600
        if pt == ct and gap < 1.5 and abs(c_h - pp_h) < 0.15:
            if (ct == "HighWater" and c_h > pp_h) or (ct == "LowWater" and c_h < pp_h):
                filtered[-1] = raw[i]
            continue
        filtered.append(raw[i])

    # Apply Admiralty-convention time offsets to each event (shift earlier to
    # match published HW/LW times), then filter to the originally-requested window
    events = []
    for et, t, h in filtered:
        if et == "HighWater":
            shifted_t = t - timedelta(minutes=HW_ADMIRALTY_OFFSET_MINUTES)
        else:
            shifted_t = t - timedelta(minutes=LW_ADMIRALTY_OFFSET_MINUTES)
        if shifted_t < start or shifted_t > end:
            continue
        events.append({
            "timestamp": shifted_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "height_m": h,
            "event_type": et,
            "is_approximate_time": False,
            "is_approximate_height": False,
        })

    logger.info(f"Harmonic model predicted {len(events)} events from {start.date()} to {end.date()}")
    return events
