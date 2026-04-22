"""
Observation classifier for calibration routing.

Given a mooring's configuration, tide event data and historical wind
observations, classifies each observation as either:

  - "base":        contributes to base drying height calibration
  - "wind_offset": contributes to shallow-side wind offset calibration

Afloat observations are always "base". Aground observations are
classified "wind_offset" only when ALL of the following hold:

  1. Mooring has wind_offset_enabled and a shallow_direction set.
     Note: shallow_extra_depth_m is NOT required to be > 0 - this
     calibration is the mechanism by which that value is discovered,
     so requiring it pre-set would prevent bootstrapping.
  2. The observation has a direction_of_lay recorded.
  3. The observation falls within the interval [HW_n + 4h, HW_{n+1}]
     of the preceding high water HW_n, where HW_n + 4h is the nominal
     wind sample time.
  4. A wind observation exists within +/- 60 minutes of HW_n + 4h.
  5. That wind was pushing the boat toward the configured shallow side
     (same three-sector test as should_apply_offset).
  6. The observation's direction_of_lay is within one compass sector
     of the matched wind direction, confirming a wind-driven grounding.

The downstream calibration functions apply a further arithmetic test:
an aground observation only contributes to the wind-offset calibration
if its implied offset (h - draught - current_drying) is strictly
positive. Observations that meet the classifier's criteria but fail
the arithmetic test fall back to the base-drying calibration as
ordinary aground observations - see calibrate_drying_height.

This module is pure: it does not read from the database or mutate any
state. Callers are responsible for supplying the relevant data sets.
"""

from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse

from app.wind import COMPASS_POINTS, should_apply_offset
from app.config import WIND_SAMPLE_HW_OFFSET_HOURS

# Tolerance when matching an HW+offset nominal sample time to an actual
# stored wind observation. The scheduler normally fires on time, but
# OWM calls can be late, the server may have been down briefly, or the
# sample time may have shifted. 60 minutes covers these cases without
# letting a wind reading from a neighbouring cycle leak in, since
# successive HWs are ~12h25m apart.
WIND_SAMPLE_TOLERANCE_MINUTES = 60


def _parse_utc(ts):
    """Parse an ISO string (or pass through a datetime), assuming UTC if naive."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = dtparse.parse(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sector_index(compass):
    """Return 0-7 index of a compass point, or -1 if unknown/empty."""
    if not compass:
        return -1
    if compass in COMPASS_POINTS:
        return COMPASS_POINTS.index(compass)
    return -1


def within_one_sector(a, b):
    """
    Return True if compass points a and b are within one 45-degree sector
    of each other (inclusive). Unknown or empty inputs yield False.

    Uses the shorter arc distance on the 8-point compass, so N <-> NW
    is distance 1 (wraparound-aware).
    """
    ia = _sector_index(a)
    ib = _sector_index(b)
    if ia < 0 or ib < 0:
        return False
    d = abs(ia - ib) % 8
    return min(d, 8 - d) <= 1


def _find_preceding_hw(obs_dt, sorted_hw_events):
    """
    Return the HW dict immediately at or before obs_dt, or None if no
    HW in the supplied data precedes the observation.
    """
    preceding = None
    for hw in sorted_hw_events:
        hw_dt = _parse_utc(hw["timestamp"])
        if hw_dt <= obs_dt:
            preceding = hw
        else:
            break
    return preceding


def _find_nearest_wind_sample(target_dt, wind_observations, tolerance_minutes):
    """
    Return the wind observation closest to target_dt whose timestamp is
    within +/- tolerance_minutes, or None if nothing qualifies.
    """
    window = timedelta(minutes=tolerance_minutes)
    lo = target_dt - window
    hi = target_dt + window
    best = None
    best_diff = None
    for w in wind_observations:
        ts = w.get("timestamp")
        if not ts:
            continue
        w_dt = _parse_utc(ts)
        if w_dt < lo or w_dt > hi:
            continue
        diff = abs((w_dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best = w
            best_diff = diff
    return best


def classify_observation(obs, mooring, sorted_hw_events, wind_observations,
                         offset_hours=WIND_SAMPLE_HW_OFFSET_HOURS,
                         wind_tolerance_minutes=WIND_SAMPLE_TOLERANCE_MINUTES):
    """
    Classify a single observation as "base" or "wind_offset".

    Returns a dict:
      {
        "classification": "base" | "wind_offset",
        "reason":         short human-readable rationale,
        "hw_timestamp":   ISO string of the preceding HW (or None),
        "wind_compass":   direction of matched HW+offset wind (or None),
      }

    Afloat observations always classify as "base". Aground observations
    classify as "wind_offset" only when every condition in the module
    docstring is met, otherwise "base".
    """
    result = {
        "classification": "base",
        "reason": "",
        "hw_timestamp": None,
        "wind_compass": None,
    }

    state = obs.get("state")

    # Afloat observations always calibrate base drying directly.
    if state != "aground":
        result["reason"] = "afloat observation routes to base drying"
        return result

    # Wind-offset feature must be configured on the mooring.
    if not mooring.get("wind_offset_enabled"):
        result["reason"] = "wind offset not enabled for this mooring"
        return result

    shallow_dir = mooring.get("shallow_direction") or ""
    if not shallow_dir:
        result["reason"] = "no shallow_direction configured"
        return result

    # Note: shallow_extra_depth_m is intentionally NOT gated here. This
    # calibration is how that value gets discovered, so requiring it
    # pre-set would block bootstrapping. The arithmetic check on the
    # implied offset happens downstream in the calibration functions.

    # The observation must record a direction_of_lay to verify grounding
    # was wind-driven.
    lay_dir = obs.get("direction_of_lay") or ""
    if not lay_dir:
        result["reason"] = "no direction_of_lay recorded on observation"
        return result

    # Locate the preceding HW. Without it, no wind sample can be matched.
    obs_dt = _parse_utc(obs["timestamp"])
    preceding_hw = _find_preceding_hw(obs_dt, sorted_hw_events)
    if not preceding_hw:
        result["reason"] = "no preceding HW in available tide data"
        return result

    preceding_hw_dt = _parse_utc(preceding_hw["timestamp"])
    result["hw_timestamp"] = preceding_hw["timestamp"]

    # Observation must fall in [HW + offset, next HW]. Observations before
    # HW+offset cannot be classified against this cycle's wind sample,
    # which only exists from that point onwards.
    hw_plus_offset = preceding_hw_dt + timedelta(hours=offset_hours)
    if obs_dt < hw_plus_offset:
        result["reason"] = (
            f"observation before HW+{offset_hours:g}h sample window"
        )
        return result

    # Locate the HW+offset wind sample.
    wind_sample = _find_nearest_wind_sample(
        hw_plus_offset, wind_observations, wind_tolerance_minutes
    )
    if not wind_sample:
        result["reason"] = (
            f"no HW+{offset_hours:g}h wind sample within "
            f"+/-{wind_tolerance_minutes}min"
        )
        return result

    wind_compass = wind_sample.get("direction_compass") or ""
    result["wind_compass"] = wind_compass
    if not wind_compass:
        result["reason"] = "matched wind sample has no direction_compass"
        return result

    # Wind must have been pushing the boat toward the shallow side.
    if not should_apply_offset(wind_compass, shallow_dir):
        result["reason"] = (
            f"wind {wind_compass} not pushing toward shallow {shallow_dir}"
        )
        return result

    # Bow heading must be within +/-1 sector of wind direction
    # (a boat bow-to-wind on a swing mooring under wind-driven swing).
    if not within_one_sector(lay_dir, wind_compass):
        result["reason"] = (
            f"lay {lay_dir} not within +/-1 sector of wind {wind_compass}"
        )
        return result

    # All conditions met.
    result["classification"] = "wind_offset"
    result["reason"] = (
        f"aground, wind {wind_compass} toward shallow {shallow_dir}, "
        f"bow {lay_dir}"
    )
    return result


def classify_observations(observations, mooring, hw_events, wind_observations,
                          offset_hours=WIND_SAMPLE_HW_OFFSET_HOURS,
                          wind_tolerance_minutes=WIND_SAMPLE_TOLERANCE_MINUTES):
    """
    Classify all observations in bulk. Returns a list of dicts:
      [{"observation": <obs>, "classification": "...", "reason": "...",
        "hw_timestamp": "...", "wind_compass": "..."}, ...]

    The input hw_events may include all event types; only HighWater rows
    are used.
    """
    sorted_hws = sorted(
        [e for e in hw_events if e.get("event_type") == "HighWater"],
        key=lambda e: e["timestamp"],
    )
    out = []
    for obs in observations:
        c = classify_observation(
            obs, mooring, sorted_hws, wind_observations,
            offset_hours=offset_hours,
            wind_tolerance_minutes=wind_tolerance_minutes,
        )
        out.append({"observation": obs, **c})
    return out
