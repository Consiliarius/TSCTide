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
    """
    cfg = load_model_config()
    curve = cfg.get("tidal_curve", {})
    flood_frac = curve.get("flood_duration_fraction", 0.42)

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
        # Ebbing tide — apply asymmetry via stand effect
        stand_mins = curve.get("stand_duration_minutes", 30)
        stand_frac = curve.get("stand_height_fraction", 0.95)

        if total_seconds > 0:
            stand_proportion = (stand_mins * 60) / total_seconds
        else:
            stand_proportion = 0

        if fraction < stand_proportion:
            # During the stand — height stays near HW
            stand_drop = h_before * (1 - stand_frac)
            return h_before - stand_drop * (fraction / stand_proportion)
        else:
            # After stand — cosine ebb from stand level to LW
            adjusted_fraction = (fraction - stand_proportion) / (1 - stand_proportion)
            stand_height = h_before * stand_frac
            return _cosine_interp(1 - adjusted_fraction, h_after, stand_height)
    else:
        # Same event types (shouldn't happen with clean data) — linear fallback
        return h_before + (h_after - h_before) * fraction


def _cosine_interp(fraction: float, h_low: float, h_high: float) -> float:
    """Cosine interpolation between low and high values. fraction 0→1 maps low→high."""
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
    source: str = "ukho",
    interval_minutes: int = 3,
) -> list[dict]:
    """
    Compute access windows from tide events.

    An access window is the continuous period around each HW during which:
        tide_height > drying_height + draught + safety_margin + wind_offset

    Returns list of window dicts:
        hw_timestamp, hw_height_m, start_time, end_time, duration_minutes, source
    """
    threshold = drying_height_m + draught_m + safety_margin_m + wind_offset_m

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

        # If HW height is below threshold, no access window
        if hw_height < threshold:
            windows.append({
                "hw_timestamp": to_utc_str(hw_dt),
                "hw_height_m": hw_height,
                "start_time": None,
                "end_time": None,
                "duration_minutes": 0,
                "source": source,
                "below_threshold": True,
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

        if start_time and end_time:
            duration = (end_time - start_time).total_seconds() / 60.0
        else:
            duration = 0

        windows.append({
            "hw_timestamp": to_utc_str(hw_dt),
            "hw_height_m": hw_height,
            "start_time": to_utc_str(start_time) if start_time else None,
            "end_time": to_utc_str(end_time) if end_time else None,
            "duration_minutes": round(duration),
            "source": source,
            "below_threshold": False,
            "incomplete_data": not (start_time and end_time),
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

    # No crossing found — insufficient data or HW below threshold for full range
    return None


def generate_event_uid(mooring_id: int, hw_timestamp: str) -> str:
    """
    Generate a deterministic event UID for a given mooring and HW time.

    Uses a tidal cycle number rather than exact HW minutes, so the same
    physical tide gets the same UID regardless of which data source
    predicted it (harmonic ±30min vs UKHO). The average tidal cycle is
    12.42 hours; dividing hours-since-epoch by this and rounding gives
    a cycle ID that's stable for shifts of up to ±3 hours.
    """
    dt = dtparse.parse(hw_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hours_since_epoch = (dt - epoch).total_seconds() / 3600.0
    cycle_number = round(hours_since_epoch / 12.4167)
    return f"tidal-access-m{mooring_id:03d}-c{cycle_number:05d}@langstone"
