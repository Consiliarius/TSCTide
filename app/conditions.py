"""
Current conditions at Langstone Harbour.

Provides a single async function get_current_conditions() that returns
combined tide and weather state. Designed to be callable by:
  - The /api/conditions endpoint (UI consumption)
  - The 15-minute scheduler refresh job (keeps the cache warm)
  - Other internal features (e.g. a future "is this mooring accessible
    right now?" check)

Tide data is computed locally from stored UKHO HW/LW events using the
calibrated tidal curve; no external API call is needed for tide state.
Weather data comes from OWM via app.wind.fetch_current_weather().

The module maintains an in-memory cache with a configurable TTL (default
15 minutes). Multiple callers within the TTL window share the same
result without re-fetching from OWM or re-computing the tide.

Pressure trend is derived from stored pressure_history rows (written
by each conditions refresh). The trend requires ~3 hours of history to
be meaningful; before that, "Unknown" is returned.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import to_utc_str
from app.database import (
    get_ukho_tide_events, get_pressure_history,
    store_pressure_reading, cleanup_old_pressure_history,
)

logger = logging.getLogger(__name__)

# --- Pressure trend thresholds (hPa over 3 hours) ---
#
# Converted from inches of mercury (source: standard barometric change
# classification). 1 inHg = 33.8639 hPa.
#
#   Steady:     < 0.003 inHg/3h  =  < 0.10 hPa/3h
#   Slow:       0.003 - 0.04     =  0.10 - 1.35
#   Moderate:   0.04  - 0.18     =  1.35 - 6.10
#   Rapid:      > 0.18           =  > 6.10
PRESSURE_STEADY_THRESHOLD = 0.10     # hPa / 3h
PRESSURE_SLOW_THRESHOLD = 1.35      # hPa / 3h
PRESSURE_RAPID_THRESHOLD = 6.10     # hPa / 3h
PRESSURE_TREND_WINDOW_HOURS = 3

# --- Cache ---
_cached_conditions: Optional[dict] = None
_cache_timestamp: Optional[datetime] = None
CACHE_TTL_MINUTES = 15


def _compute_pressure_trend(current_hpa: float) -> dict:
    """
    Compare current pressure against the reading closest to 3 hours ago.
    Returns a dict with trend label, arrow symbol, and delta.

    If insufficient history is available (< 2.5 hours of readings),
    returns trend "Unknown" so the UI can show a graceful placeholder
    rather than a potentially misleading "Steady".
    """
    history = get_pressure_history(hours=4)
    if not history:
        return {"trend": "Unknown", "arrow": "", "delta_hpa": None}

    now = datetime.now(timezone.utc)
    target_time = now - timedelta(hours=PRESSURE_TREND_WINDOW_HOURS)

    # Find the reading closest to 3 hours ago.
    best_row = None
    best_gap = None
    for row in history:
        ts = row["timestamp"]
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            row_dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        gap = abs((row_dt - target_time).total_seconds())
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_row = row

    # Require the comparison reading to be within 30 minutes of the
    # 3-hour-ago target. If the oldest reading is only 1 hour old,
    # the trend calculation would be meaningless.
    if best_row is None or best_gap > 1800:
        return {"trend": "Unknown", "arrow": "", "delta_hpa": None}

    delta = current_hpa - best_row["pressure_hpa"]
    abs_delta = abs(delta)

    if abs_delta < PRESSURE_STEADY_THRESHOLD:
        trend = "Steady"
        arrow = "\u2014"  # em-dash
    elif abs_delta < PRESSURE_SLOW_THRESHOLD:
        if delta > 0:
            trend = "Slowly rising"
            arrow = "\u2197"  # NE arrow
        else:
            trend = "Slowly falling"
            arrow = "\u2198"  # SE arrow
    elif abs_delta < PRESSURE_RAPID_THRESHOLD:
        if delta > 0:
            trend = "Rising"
            arrow = "\u2191"  # up arrow
        else:
            trend = "Falling"
            arrow = "\u2193"  # down arrow
    else:
        if delta > 0:
            trend = "Rapidly rising"
            arrow = "\u21d1"  # double up arrow
        else:
            trend = "Rapidly falling"
            arrow = "\u21d3"  # double down arrow

    return {
        "trend": trend,
        "arrow": arrow,
        "delta_hpa": round(delta, 1),
    }


def _compute_tide_state(now_utc: datetime) -> dict:
    """
    Compute current tide height, state, and next event from stored
    UKHO data.

    Uses the calibrated tidal curve (interpolate_height_at_time) to
    derive the instantaneous height from the bracketing HW/LW events.

    Returns a dict with height_m, state, state_description, next_event,
    and computed_at. If UKHO data is unavailable, the tide block is
    returned with null values and a descriptive error.
    """
    from app.access_calc import interpolate_height_at_time
    from dateutil import parser as dtparse

    now_iso = to_utc_str(now_utc)

    # Query a window from 12 hours before now to 24 hours after. The
    # lookback ensures at least one HW and one LW bracket "now" for the
    # interpolator. The 24-hour lookahead covers up to 3 future events
    # even in the worst case (each tidal half-cycle is ~6.2 hours at
    # Langstone, so 3 events can span up to ~18.6 hours when the first
    # is nearly a full half-cycle away).
    start = to_utc_str(now_utc - timedelta(hours=12))
    end = to_utc_str(now_utc + timedelta(hours=24))
    events = get_ukho_tide_events(start, end)

    result = {
        "height_m": None,
        "state": None,
        "state_description": "Tide data unavailable",
        "upcoming_events": [],
        "computed_at": now_iso,
    }

    if not events:
        return result

    # Compute current height via the calibrated tidal curve.
    height = interpolate_height_at_time(now_iso, events)
    if height is not None:
        result["height_m"] = round(height, 1)

    # Determine tide state from the bracketing events.
    before_ev = None
    after_ev = None
    for ev in events:
        ev_dt = dtparse.parse(ev["timestamp"])
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=timezone.utc)
        if ev_dt <= now_utc:
            before_ev = ev
        if ev_dt > now_utc and after_ev is None:
            after_ev = ev

    if before_ev and after_ev:
        before_type = before_ev["event_type"]
        after_type = after_ev["event_type"]

        # Check if we're within 30 minutes of a HW or LW event (stand).
        before_dt = dtparse.parse(before_ev["timestamp"])
        if before_dt.tzinfo is None:
            before_dt = before_dt.replace(tzinfo=timezone.utc)
        after_dt = dtparse.parse(after_ev["timestamp"])
        if after_dt.tzinfo is None:
            after_dt = after_dt.replace(tzinfo=timezone.utc)

        mins_since_before = (now_utc - before_dt).total_seconds() / 60
        mins_until_after = (after_dt - now_utc).total_seconds() / 60

        if before_type == "HighWater" and mins_since_before < 30:
            result["state"] = "high_water"
            result["state_description"] = "High water"
        elif after_type == "HighWater" and mins_until_after < 30:
            result["state"] = "high_water"
            result["state_description"] = "High water (approaching)"
        elif before_type == "LowWater" and mins_since_before < 30:
            result["state"] = "low_water"
            result["state_description"] = "Low water"
        elif after_type == "LowWater" and mins_until_after < 30:
            result["state"] = "low_water"
            result["state_description"] = "Low water (approaching)"
        elif before_type == "LowWater" and after_type == "HighWater":
            result["state"] = "flooding"
            result["state_description"] = "Flooding (tide rising)"
        elif before_type == "HighWater" and after_type == "LowWater":
            result["state"] = "ebbing"
            result["state_description"] = "Ebbing (tide falling)"

    # Upcoming events: next 3 future HW/LW from UKHO data. Gives the UI
    # a full HW/LW/HW (or LW/HW/LW) cycle to display.
    future_events = []
    for ev in events:
        ev_dt = dtparse.parse(ev["timestamp"])
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=timezone.utc)
        if ev_dt > now_utc:
            mins_until = (ev_dt - now_utc).total_seconds() / 60
            et_label = "HW" if ev["event_type"] == "HighWater" else "LW"
            future_events.append({
                "type": ev["event_type"],
                "type_label": et_label,
                "time": ev["timestamp"],
                "height_m": round(ev["height_m"], 1),
                "minutes_until": round(mins_until),
            })
            if len(future_events) >= 3:
                break
    result["upcoming_events"] = future_events

    # Spring / Neap / Mid classification for today (v2.8). Returns None
    # when there's insufficient stored UKHO history; the UI then omits
    # the chip rather than guessing.
    try:
        from app.tide_state import classify_spring_neap
        result["spring_neap"] = classify_spring_neap()
    except Exception as e:
        logger.warning(f"Spring/neap classification skipped: {e}")
        result["spring_neap"] = None

    return result


async def get_current_conditions(force_refresh: bool = False) -> dict:
    """
    Return the current tide and weather conditions at Langstone Harbour.

    Results are cached for CACHE_TTL_MINUTES (default 15). Pass
    force_refresh=True to bypass the cache (used by the scheduler
    refresh job to ensure fresh data is written and the pressure
    history is updated).

    The returned dict has two top-level keys:
      - "tide": current height, state, next event
      - "weather": wind, pressure (with trend), precipitation, visibility

    Both blocks include a timestamp indicating when the data was
    computed/fetched. The tide block is computed locally from stored
    UKHO data; the weather block is fetched from OWM.

    If OWM is unavailable, the weather block is returned as None and
    the tide block is still populated (it has no external dependency).

    If UKHO data is unavailable (e.g. first run before the daily
    scheduler has fetched), the tide block fields are null with a
    descriptive message.
    """
    global _cached_conditions, _cache_timestamp

    # Return cached result if within TTL and not forcing refresh.
    if not force_refresh and _cached_conditions and _cache_timestamp:
        age = (datetime.now(timezone.utc) - _cache_timestamp).total_seconds()
        if age < CACHE_TTL_MINUTES * 60:
            return _cached_conditions

    from app.wind import fetch_current_weather

    now_utc = datetime.now(timezone.utc)

    # Tide state (local computation, no API call).
    tide = _compute_tide_state(now_utc)

    # Weather (OWM API call).
    weather = await fetch_current_weather()

    # Store pressure reading and compute trend.
    if weather and weather.get("pressure", {}).get("hpa"):
        pressure_hpa = weather["pressure"]["hpa"]
        store_pressure_reading(to_utc_str(now_utc), pressure_hpa)
        cleanup_old_pressure_history(hours=24)
        trend = _compute_pressure_trend(pressure_hpa)
        weather["pressure"]["trend"] = trend["trend"]
        weather["pressure"]["trend_arrow"] = trend["arrow"]
        weather["pressure"]["delta_hpa_3h"] = trend["delta_hpa"]

    result = {
        "tide": tide,
        "weather": weather,
    }

    _cached_conditions = result
    _cache_timestamp = now_utc
    return result
