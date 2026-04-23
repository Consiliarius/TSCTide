"""
Core access window calculation.

Given tide event data (HW/LW times and heights) and mooring configuration,
computes the time windows during which there is sufficient water depth
for the boat to be afloat.

Uses the Langstone asymmetric tidal curve to interpolate heights between
HW and LW events, then finds the threshold crossings.
"""

import math
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse
from typing import Optional

from app.config import load_model_config, to_utc_str

logger = logging.getLogger(__name__)

# Module-level cache for model config tidal curve parameters.
# load_model_config() parses a JSON file on every call; at 3-minute
# interpolation intervals over a 7-hour window this is called thousands
# of times per calculation. The config is effectively static between
# API calls, so cache it on first access.
# Call invalidate_model_config_cache() after save_model_config() to ensure
# the next calculation picks up any user edits.
_cached_curve_params: Optional[dict] = None


def invalidate_model_config_cache():
    """
    Clear the cached tidal curve parameters.
    Must be called after save_model_config() so that subsequent
    calculations use the updated values.
    """
    global _cached_curve_params
    _cached_curve_params = None


def _get_curve_params() -> dict:
    """Return tidal curve parameters, loading from config on first call."""
    global _cached_curve_params
    if _cached_curve_params is None:
        cfg = load_model_config()
        _cached_curve_params = cfg.get("tidal_curve", {})
    return _cached_curve_params


def _interpolate_from_parsed(target: datetime, parsed_events: list[tuple]) -> Optional[float]:
    """
    Fast interpolation using pre-parsed, pre-sorted events.
    parsed_events: list of (datetime, height_m, event_type) tuples, sorted by time.
    """
    before = None
    after = None
    for dt, h, et in parsed_events:
        if dt <= target:
            before = (dt, h, et)
        if dt > target and after is None:
            after = (dt, h, et)
            break  # No need to continue once we have the bracket

    if before is None or after is None:
        return None

    return _curve_interpolate(target, before, after)


def interpolate_height_at_time(target_iso: str, events: list[dict]) -> Optional[float]:
    """
    Interpolate tide height at a specific time using surrounding HW/LW events
    and the Langstone asymmetric tidal curve.

    This is the public API used by calibration. For repeated calls with the
    same event set, use _interpolate_from_parsed with pre-parsed data.
    """
    target = dtparse.parse(target_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    parsed = []
    for ev in events:
        ts = ev["timestamp"]
        dt = dtparse.parse(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append((dt, ev["height_m"], ev["event_type"]))

    parsed.sort(key=lambda x: x[0])

    return _interpolate_from_parsed(target, parsed)


def _curve_interpolate(target: datetime, before: tuple, after: tuple) -> float:
    """
    Interpolate height using the Langstone asymmetric tidal curve.

    before/after: (datetime, height_m, event_type) tuples

    Note: flood_duration_fraction is present in model_config.json for
    documentation purposes but is not applied here. The actual event
    timestamps already encode the asymmetric flood/ebb duration, so
    applying an additional fraction correction would double-count it.
    """
    curve = _get_curve_params()

    t_before, h_before, et_before = before
    t_after, h_after, et_after = after

    total_seconds = (t_after - t_before).total_seconds()
    if total_seconds <= 0:
        return h_before

    elapsed = (target - t_before).total_seconds()
    fraction = elapsed / total_seconds

    # Determine if this is a flooding or ebbing phase
    if et_before == "LowWater" and et_after == "HighWater":
        # Flooding tide
        return _cosine_interp(fraction, h_before, h_after)
    elif et_before == "HighWater" and et_after == "LowWater":
        # Ebbing tide - apply asymmetry via stand effect
        stand_mins = curve.get("stand_duration_minutes", 30)
        stand_frac = curve.get("stand_height_fraction", 0.95)

        if total_seconds > 0:
            stand_proportion = (stand_mins * 60) / total_seconds
        else:
            stand_proportion = 0

        if fraction < stand_proportion:
            # During the stand - height stays near HW
            stand_drop = h_before * (1 - stand_frac)
            return h_before - stand_drop * (fraction / stand_proportion)
        else:
            # After stand - cosine ebb from stand level to LW
            adjusted_fraction = (fraction - stand_proportion) / (1 - stand_proportion)
            stand_height = h_before * stand_frac
            return _cosine_interp(1 - adjusted_fraction, h_after, stand_height)
    else:
        # Same event types (shouldn't happen with clean data) - linear fallback
        return h_before + (h_after - h_before) * fraction


def _cosine_interp(fraction: float, h_low: float, h_high: float) -> float:
    """Cosine interpolation between low and high values. fraction 0 to 1 maps low to high."""
    fraction = max(0.0, min(1.0, fraction))
    # Cosine gives smooth curve: 0 at fraction=0, 1 at fraction=1
    t = (1 - math.cos(math.pi * fraction)) / 2.0
    return h_low + (h_high - h_low) * t


def compute_access_windows(
    events: list[dict],
    draught_m: float,
    drying_height_m: float,
    safety_margin_m: float,
    wind_offset_m: float = 0.0,
    wind_offset_hw_timestamp: Optional[str] = None,
    source: str = "ukho",
    interval_minutes: int = 3,
) -> list[dict]:
    """
    Compute access windows from tide events.

    An access window is the continuous period around each HW during which:
        tide_height > drying_height + draught + safety_margin [+ wind_offset]

    Wind offset scoping:
      - If ``wind_offset_hw_timestamp`` is provided, the ``wind_offset_m`` is
        applied ONLY to the HW whose timestamp matches that string. All other
        HW windows use the baseline threshold. This is the correct behaviour
        for a just-observed wind reading, which is only a good predictor for
        the next flood tide.
      - If ``wind_offset_hw_timestamp`` is None (default) and ``wind_offset_m``
        is non-zero, the offset is applied to every HW window. This preserves
        backward compatibility but is rarely what's wanted.

    Each returned window carries a ``wind_adjusted`` boolean indicating whether
    that specific HW had the offset applied, so the caller no longer needs to
    set this flag in a post-processing loop.

    Returns list of window dicts:
        hw_timestamp, hw_height_m, start_time, end_time, duration_minutes,
        source, wind_adjusted, below_threshold, incomplete_data,
        always_accessible

    Window states (mutually exclusive):
      - below_threshold=True: HW itself doesn't clear the threshold, no window
      - always_accessible=True: tide never drops below the threshold across
        the tidal cycle (both bracketing LWs are above threshold). Happens
        with low or negative drying heights or high-neap LWs.
      - incomplete_data=True: couldn't find one or both threshold crossings
        AND couldn't confirm "always accessible" (bracketing LWs missing from
        the event list, typically at edges of the data window).
      - otherwise a normal window with start_time / end_time populated.
    """
    base_threshold = drying_height_m + draught_m + safety_margin_m

    # Parse and sort events
    parsed = []
    for ev in events:
        ts = ev["timestamp"]
        dt = dtparse.parse(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append({
            "dt": dt,
            "height_m": ev["height_m"],
            "event_type": ev["event_type"],
        })
    parsed.sort(key=lambda x: x["dt"])

    if len(parsed) < 2:
        return []

    # Identify high waters
    high_waters = [p for p in parsed if p["event_type"] == "HighWater"]

    windows = []
    for hw in high_waters:
        hw_dt = hw["dt"]
        hw_height = hw["height_m"]
        hw_ts_str = to_utc_str(hw_dt)

        # Determine whether wind offset applies to THIS HW
        if wind_offset_hw_timestamp is not None:
            wind_applied_here = (hw_ts_str == wind_offset_hw_timestamp)
        else:
            wind_applied_here = (wind_offset_m > 0)

        threshold = base_threshold + (wind_offset_m if wind_applied_here else 0.0)

        # If HW height is below threshold, no access window
        if hw_height < threshold:
            windows.append({
                "hw_timestamp": hw_ts_str,
                "hw_height_m": hw_height,
                "start_time": None,
                "end_time": None,
                "duration_minutes": 0,
                "source": source,
                "below_threshold": True,
                "wind_adjusted": wind_applied_here,
            })
            continue

        # Search outward from HW to find threshold crossings
        # Search backward (before HW)
        start_time = _find_crossing(
            hw_dt, parsed, threshold, direction="backward",
            max_hours=7, interval_minutes=interval_minutes
        )
        # Search forward (after HW)
        end_time = _find_crossing(
            hw_dt, parsed, threshold, direction="forward",
            max_hours=7, interval_minutes=interval_minutes
        )

        # Distinguish "always accessible" from "incomplete data":
        #   - Always accessible: the bracketing LWs on either side of HW are
        #     themselves above the threshold, so the tide never drops low
        #     enough to ground the boat. Happens with low or negative drying
        #     heights, or on shallow neaps where LW is unusually high.
        #   - Incomplete data: one or both bracketing LWs are missing from
        #     the event list (edge of data window, typical for first/last HW).
        if not (start_time and end_time):
            # Find bracketing LWs within 8 hours either side of HW
            lw_before = next(
                (p for p in reversed(parsed)
                 if p["event_type"] == "LowWater"
                 and p["dt"] < hw_dt
                 and (hw_dt - p["dt"]).total_seconds() <= 8 * 3600),
                None,
            )
            lw_after = next(
                (p for p in parsed
                 if p["event_type"] == "LowWater"
                 and p["dt"] > hw_dt
                 and (p["dt"] - hw_dt).total_seconds() <= 8 * 3600),
                None,
            )
            both_lws_above = (
                lw_before is not None
                and lw_after is not None
                and lw_before["height_m"] >= threshold
                and lw_after["height_m"] >= threshold
            )

            if both_lws_above:
                # Always accessible across this tidal cycle. Report the span
                # from the trough of one side to the trough of the other so
                # downstream code has something sensible to work with if it
                # needs a duration; the UI should show this as a distinct
                # state rather than a numeric window.
                windows.append({
                    "hw_timestamp": hw_ts_str,
                    "hw_height_m": hw_height,
                    "start_time": to_utc_str(lw_before["dt"]),
                    "end_time": to_utc_str(lw_after["dt"]),
                    "duration_minutes": round(
                        (lw_after["dt"] - lw_before["dt"]).total_seconds() / 60
                    ),
                    "source": source,
                    "below_threshold": False,
                    "incomplete_data": False,
                    "always_accessible": True,
                    "wind_adjusted": wind_applied_here,
                })
                continue
            # else: fall through to the incomplete-data window below

        if start_time and end_time:
            duration = (end_time - start_time).total_seconds() / 60.0
        else:
            duration = 0

        windows.append({
            "hw_timestamp": hw_ts_str,
            "hw_height_m": hw_height,
            "start_time": to_utc_str(start_time) if start_time else None,
            "end_time": to_utc_str(end_time) if end_time else None,
            "duration_minutes": round(duration),
            "source": source,
            "below_threshold": False,
            "incomplete_data": not (start_time and end_time),
            "always_accessible": False,
            "wind_adjusted": wind_applied_here,
        })

    return windows


def _find_crossing(
    hw_dt: datetime,
    events: list[dict],
    threshold: float,
    direction: str,
    max_hours: float = 7,
    interval_minutes: int = 3,
) -> Optional[datetime]:
    """
    Find the time at which tide height crosses the threshold,
    searching outward from HW in the given direction.
    Uses pre-parsed event tuples for efficient repeated interpolation.
    """
    step = timedelta(minutes=interval_minutes)
    if direction == "backward":
        step = -step

    max_delta = timedelta(hours=max_hours)
    current = hw_dt

    # Pre-parse events once for fast interpolation
    parsed_tuples = [
        (e["dt"], e["height_m"], e["event_type"]) for e in events
    ]

    prev_height = None
    while abs(current - hw_dt) < max_delta:
        height = _interpolate_from_parsed(current, parsed_tuples)
        if height is None:
            break

        if prev_height is not None:
            if (direction == "backward" and height < threshold <= prev_height) or \
               (direction == "forward" and prev_height >= threshold > height):
                # Crossing found between current and previous step
                # Linear interpolation for precise crossing time
                if abs(prev_height - height) > 0.001:
                    frac = (threshold - height) / (prev_height - height)
                    crossing = current - step * frac
                    return crossing
                return current

        prev_height = height
        current += step

    # No crossing found - insufficient data or HW below threshold for full range
    return None


def generate_event_uid(mooring_id: int, hw_timestamp: str) -> str:
    """
    Generate a deterministic event UID for a given mooring and HW time.

    Uses a tidal cycle number rather than exact HW minutes, so the same
    physical tide gets the same UID regardless of which data source
    predicted it (harmonic +/- 30min vs UKHO). The average tidal cycle is
    12.42 hours; dividing hours-since-epoch by this and rounding gives
    a cycle ID that's stable for shifts of up to +/- 3 hours.
    """
    dt = dtparse.parse(hw_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hours_since_epoch = (dt - epoch).total_seconds() / 3600.0
    cycle_number = round(hours_since_epoch / 12.4167)
    return f"tidal-access-m{mooring_id:03d}-c{cycle_number:05d}@langstone"
