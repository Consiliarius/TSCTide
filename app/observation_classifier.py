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
  3. A wind observation exists during the same tidal cycle (between the
     preceding HW and the following HW). Wind samples are now taken
     per-mooring at each vessel's worst-case grounding, so their exact
     time varies; matching is by cycle, not by a fixed nominal time.
     (Historical HW+4h samples still fall within their cycle, so this is
     backward-compatible with data recorded under the old scheduler.)
  4. That wind was pushing the boat toward the configured shallow side
     (same three-sector test as should_apply_offset).
  5. The observation's direction_of_lay is within one compass sector
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

# Average tidal cycle length, used to bound "this cycle" when the following
# HW is not present in the supplied tide data (edge of the data window).
_AVG_CYCLE_HOURS = 12.4167


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


def _find_following_hw(after_dt, sorted_hw_events):
    """Return the first HW dict strictly after after_dt, or None."""
    for hw in sorted_hw_events:
        if _parse_utc(hw["timestamp"]) > after_dt:
            return hw
    return None


def _find_cycle_wind_sample(obs_dt, cycle_start_dt, cycle_end_dt, wind_observations):
    """
    Return the wind observation taken during this tidal cycle
    [cycle_start_dt, cycle_end_dt) that is nearest to obs_dt, or None.

    Wind samples are taken per-mooring at each vessel's worst-case grounding,
    so the exact time varies; wind is ~constant across a cycle, so the cycle's
    sample nearest the observation is the right correlate regardless of which
    mooring's grounding produced it.
    """
    best = None
    best_diff = None
    for w in wind_observations:
        ts = w.get("timestamp")
        if not ts:
            continue
        w_dt = _parse_utc(ts)
        if w_dt < cycle_start_dt or w_dt >= cycle_end_dt:
            continue
        diff = abs((w_dt - obs_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best = w
            best_diff = diff
    return best


def classify_observation(obs, mooring, sorted_hw_events, wind_observations):
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

    # Bound this tidal cycle: from the preceding HW to the following HW (or
    # +1 average cycle if the next HW is beyond the supplied tide data).
    following_hw = _find_following_hw(preceding_hw_dt, sorted_hw_events)
    cycle_end_dt = (
        _parse_utc(following_hw["timestamp"]) if following_hw
        else preceding_hw_dt + timedelta(hours=_AVG_CYCLE_HOURS)
    )

    # Match the wind sample taken during this cycle, nearest the observation.
    # Samples are taken per-mooring at each vessel's worst-case grounding, so
    # the exact time varies; wind is ~constant across a cycle (the persistence
    # assumption the offset feature relies on), so the cycle's nearest sample
    # is the right correlate regardless of which mooring's grounding produced
    # it. Old HW+4h samples also fall within their cycle, so historical data
    # still classifies.
    wind_sample = _find_cycle_wind_sample(
        obs_dt, preceding_hw_dt, cycle_end_dt, wind_observations
    )
    if not wind_sample:
        result["reason"] = "no wind sample in this tidal cycle"
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


def classify_observations(observations, mooring, hw_events, wind_observations):
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
            obs, mooring, sorted_hws, wind_observations
        )
        out.append({"observation": obs, **c})
    return out
