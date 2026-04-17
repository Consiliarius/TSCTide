"""
iCal feed generation and management.

Generates per-mooring .ics files served as subscribable feeds,
and standalone .ics exports for download. Handles event lifecycle
including source upgrades and wind-adjusted recalculations.

Event title formats:
  - No mooring: "Tidal Access (3½h)"
  - Mooring number only: "Access to #27 (3½h)"
  - Name present: "Kerry Dancer Afloat (3½h)"
  - Harmonic source prefixed with "est. "
"""

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from icalendar import Calendar, Event
from dateutil import parser as dtparse
import pytz

from app.config import FEEDS_DIR, DEFAULT_TIMEZONE, ensure_dirs
from app.database import get_calendar_events, upsert_calendar_event, cleanup_superseded_events

logger = logging.getLogger(__name__)

# Unicode fraction characters
_QUARTER_FRACTIONS = {0: "", 1: "¼", 2: "½", 3: "¾"}


def format_duration(minutes: int) -> str:
    """
    Format duration in minutes as hours with unicode fractions,
    rounded down to the nearest quarter-hour.

    Examples: 180→"3h", 195→"3¼h", 210→"3½h", 225→"3¾h", 183→"3h"
    """
    total_quarters = math.floor(minutes / 15)
    whole_hours = total_quarters // 4
    quarter = total_quarters % 4
    frac = _QUARTER_FRACTIONS[quarter]
    if whole_hours == 0 and frac:
        return f"{frac}h"
    return f"{whole_hours}{frac}h"


def build_event_title(window: dict, source: str,
                      boat_name: str = "", mooring_id: int = 0) -> str:
    """
    Build event title based on configuration state and duration.

    Title tiers:
      - No mooring, no name: "Tidal Access (Xh)"
      - Mooring number only: "Access to #N (Xh)"
      - Name present (with or without mooring): "<Name> Afloat (Xh)"
    Harmonic source prefixed with "est. "
    """
    prefix = "est. " if source == "harmonic" else ""
    duration = format_duration(window.get("duration_minutes", 0))

    if boat_name:
        return f"⚓ {prefix}{boat_name} Afloat ({duration})"
    elif mooring_id:
        return f"⚓ {prefix}Access to #{mooring_id} ({duration})"
    else:
        return f"⚓ {prefix}Tidal Access ({duration})"


def _build_description(ev_data: dict, tz, calibration: dict = None) -> str:
    """Build event description with HW details, calculation parameters, and wind info."""
    hw = dtparse.parse(ev_data["hw_timestamp"])
    if hw.tzinfo is None:
        hw = hw.replace(tzinfo=timezone.utc)
    local_hw = hw.astimezone(tz)
    tz_label = "BST" if local_hw.utcoffset().total_seconds() == 3600 else "GMT"

    lines = [f"HW at {local_hw.strftime('%H:%M')} {tz_label}"]

    if ev_data.get("hw_height_m") is not None:
        lines.append(f"Height: {ev_data['hw_height_m']:.1f}m")

    lines.append(f"Source: {ev_data.get('source', 'unknown')}")

    # Calculation parameters
    params = []
    if ev_data.get("draught_m") is not None:
        params.append(f"Draught: {ev_data['draught_m']:.1f}m")
    if ev_data.get("drying_height_m") is not None:
        params.append(f"Drying height: {ev_data['drying_height_m']:.1f}m")
    if ev_data.get("safety_margin_m") is not None:
        params.append(f"Safety margin: {ev_data['safety_margin_m']:.1f}m")
    if params:
        lines.append("")
        lines.extend(params)

    # Observational calibration
    obs_cal = ev_data.get("obs_calibrated", 0)
    lines.append(f"Observational data: {'Y' if obs_cal else 'N'}")

    if calibration:
        conf = calibration.get("confidence", "")
        if conf and conf != "none":
            conf_labels = {
                "high": "Calibration: high confidence",
                "medium": "Calibration: medium confidence",
                "low": "Calibration: low confidence",
                "partial-low": "Calibration: aground data only",
                "partial-high": "Calibration: afloat data only",
                "inconsistent": "Calibration: data inconsistent",
            }
            label = conf_labels.get(conf, "")
            if label:
                lines.append(label)

    # Wind details
    if ev_data.get("wind_adjusted"):
        lines.append("")
        wind_dir = ev_data.get("wind_direction", "unknown")
        wind_spd = ev_data.get("wind_speed_ms")
        wind_off = ev_data.get("wind_offset_m", 0)
        if wind_spd is not None:
            # Convert m/s to knots for sailors
            wind_kts = wind_spd * 1.944
            lines.append(f"Wind at ebb: {wind_dir} {wind_kts:.0f}kts")
        else:
            lines.append(f"Wind at ebb: {wind_dir}")
        if wind_off > 0:
            lines.append(f"Offset applied: +{wind_off:.1f}m drying height")
        else:
            lines.append("Offset: not applied (wind favourable)")

    return "\n".join(lines)


def _deduplicate_events(events: list[dict]) -> list[dict]:
    """
    Remove near-duplicate events (HW times within 90 minutes),
    keeping the most recently updated entry for each tidal cycle.
    """
    if len(events) < 2:
        return events

    # Sort by HW timestamp
    sorted_events = sorted(events, key=lambda e: e.get("hw_timestamp", ""))
    keep = []

    for ev in sorted_events:
        if not ev.get("hw_timestamp"):
            keep.append(ev)
            continue

        hw = dtparse.parse(ev["hw_timestamp"])
        merged = False
        for i, kept in enumerate(keep):
            if not kept.get("hw_timestamp"):
                continue
            kept_hw = dtparse.parse(kept["hw_timestamp"])
            gap = abs((hw - kept_hw).total_seconds()) / 60
            if gap < 90:
                # Same tidal cycle — keep the more recently updated one
                if ev.get("updated_at", "") >= kept.get("updated_at", ""):
                    keep[i] = ev
                merged = True
                break
        if not merged:
            keep.append(ev)

    return keep


def generate_feed_for_mooring(mooring_id: int, boat_name: str = "",
                              calibration: dict = None) -> Path:
    """
    Generate/regenerate the .ics feed file for a mooring from stored calendar events.
    Returns the path to the generated file.
    """
    ensure_dirs()
    events = get_calendar_events(mooring_id)

    cal = Calendar()
    cal.add("prodid", f"-//Tidal Access Mooring {mooring_id}//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    # No METHOD property — subscription feeds should omit it.
    # METHOD:PUBLISH causes some apps to treat it as a one-time import.

    feed_name = f"Mooring {mooring_id}"
    if boat_name:
        feed_name = f"{boat_name} (M{mooring_id})"
    cal.add("x-wr-calname", f"{feed_name} - Access Windows")
    cal.add("x-wr-timezone", DEFAULT_TIMEZONE)

    # Subscription refresh metadata (RFC 7986 + de facto standard)
    cal.add("refresh-interval;value=duration", "PT1H")
    cal.add("x-published-ttl", "PT1H")

    tz = pytz.timezone(DEFAULT_TIMEZONE)

    # Deduplicate: if multiple events have HW times within 90 minutes,
    # keep only the most recently updated. Defense-in-depth against
    # any duplication that slips past the DB-level cleanup.
    events = _deduplicate_events(events)

    for ev_data in events:
        if not ev_data.get("start_time") or not ev_data.get("end_time"):
            continue

        ical_event = Event()

        start = dtparse.parse(ev_data["start_time"])
        end = dtparse.parse(ev_data["end_time"])

        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        # Recalculate title from current start/end times rather than using
        # the stored title, which may be stale if parameters changed.
        duration_minutes = int((end - start).total_seconds() / 60)
        title_window = {"duration_minutes": duration_minutes}
        title = build_event_title(
            title_window, ev_data.get("source", "ukho"), boat_name, mooring_id
        )

        ical_event.add("summary", title)
        ical_event["uid"] = ev_data["event_uid"]

        ical_event.add("dtstart", start)
        ical_event.add("dtend", end)
        ical_event.add("description", _build_description(ev_data, tz, calibration))
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        ical_event.add("transp", "OPAQUE")
        ical_event.add("status", "CONFIRMED")
        cal.add_component(ical_event)

    feed_path = FEEDS_DIR / f"mooring_{mooring_id:03d}.ics"
    with open(feed_path, "wb") as f:
        f.write(cal.to_ical())

    logger.info(f"Generated feed for mooring {mooring_id}: {len(events)} events")
    return feed_path


def generate_export_ics(windows: list[dict], source: str,
                        boat_name: str = "", mooring_id: int = 0,
                        calibration: dict = None, calc_params: dict = None) -> bytes:
    """
    Generate a standalone .ics file for download from computed access windows.
    """
    cal = Calendar()
    cal.add("prodid", "-//Tidal Access Export//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    if boat_name:
        label = boat_name
    elif mooring_id:
        label = f"Mooring {mooring_id}"
    else:
        label = "Tidal Access"
    cal.add("x-wr-calname", f"{label} - Access Windows")
    cal.add("x-wr-timezone", DEFAULT_TIMEZONE)

    tz = pytz.timezone(DEFAULT_TIMEZONE)

    for w in windows:
        if not w.get("start_time") or not w.get("end_time"):
            continue
        if w.get("below_threshold"):
            continue

        ical_event = Event()
        title = build_event_title(w, source, boat_name, mooring_id)
        ical_event.add("summary", title)

        # Use the same cycle-based UID as the subscription feed.
        # This prevents duplicates when a user both subscribes to a feed
        # and imports an export for the same mooring.
        from app.access_calc import generate_event_uid
        uid = generate_event_uid(mooring_id or 0, w["hw_timestamp"])
        ical_event["uid"] = uid

        start = dtparse.parse(w["start_time"])
        end = dtparse.parse(w["end_time"])

        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        ical_event.add("dtstart", start)
        ical_event.add("dtend", end)

        ev_data = {
            "hw_timestamp": w["hw_timestamp"],
            "hw_height_m": w.get("hw_height_m"),
            "source": source,
            "wind_adjusted": w.get("wind_adjusted"),
        }
        if calc_params:
            ev_data.update(calc_params)
        ical_event.add("description", _build_description(ev_data, tz, calibration))
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(ical_event)

    return cal.to_ical()


def store_windows_as_events(windows: list[dict], mooring_id: int,
                            source: str, boat_name: str = "",
                            calc_params: dict = None, wind_details: dict = None):
    """Store computed access windows as calendar events in the database.

    calc_params: {draught_m, drying_height_m, safety_margin_m, obs_calibrated}
    wind_details: {direction, speed_ms, offset_m}
    """
    from app.access_calc import generate_event_uid

    for w in windows:
        if not w.get("start_time") or not w.get("end_time"):
            continue
        if w.get("below_threshold"):
            continue

        uid = generate_event_uid(mooring_id, w["hw_timestamp"])
        title = build_event_title(w, source, boat_name, mooring_id)

        event = {
            "event_uid": uid,
            "mooring_id": mooring_id,
            "hw_timestamp": w["hw_timestamp"],
            "hw_height_m": w.get("hw_height_m"),
            "start_time": w["start_time"],
            "end_time": w["end_time"],
            "source": source,
            "title": title,
            "wind_adjusted": w.get("wind_adjusted", 0),
        }

        if calc_params:
            event["draught_m"] = calc_params.get("draught_m")
            event["drying_height_m"] = calc_params.get("drying_height_m")
            event["safety_margin_m"] = calc_params.get("safety_margin_m")
            event["obs_calibrated"] = calc_params.get("obs_calibrated", 0)

        if wind_details and w.get("wind_adjusted"):
            event["wind_direction"] = wind_details.get("direction")
            event["wind_speed_ms"] = wind_details.get("speed_ms")
            event["wind_offset_m"] = wind_details.get("offset_m", 0)

        upsert_calendar_event(event)

    # Clean up any lower-priority events that were superseded but got
    # different cycle-based UIDs due to timing differences across sources
    cleanup_superseded_events(mooring_id)
