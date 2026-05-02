"""
iCal feed generation and management.

Generates per-mooring .ics files served as subscribable feeds,
and standalone .ics exports for download. Handles event lifecycle
including source upgrades and wind-adjusted recalculations.

Event title formats:
  - No mooring: "Tidal Access (3.5h)"
  - Mooring number only: "Access to #27 (3.5h)"
  - Name present: "Kerry Dancer Afloat (3.5h)"
  - Harmonic source prefixed with "est. "
  - Always-accessible cycles: "Always afloat" (no duration)
"""

import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from icalendar import Calendar, Event
from dateutil import parser as dtparse
import pytz

from app.config import FEEDS_DIR, DEFAULT_TIMEZONE, ensure_dirs, compute_cycle_number


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """
    Write bytes to path atomically via a sibling temp file plus os.replace.

    Subscription feeds are regenerated on every GET, so concurrent calendar
    clients can drive simultaneous writes against the same .ics path. A
    plain open()+write() leaves a window during which a third reader sees
    a truncated or partially-written file; os.replace is atomic on POSIX
    and on Windows for same-filesystem replacements, eliminating that
    window.
    """
    tmp_dir = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, str(path))
    except Exception:
        # Best-effort cleanup; on success the temp name no longer exists.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
from app.database import (
    get_calendar_events, upsert_calendar_event, cleanup_superseded_events,
    get_ukho_tide_events, get_harmonic_predictions,
)

logger = logging.getLogger(__name__)

# Unicode fraction characters for compact durations in event titles
_QUARTER_FRACTIONS = {0: "", 1: "\u00bc", 2: "\u00bd", 3: "\u00be"}


def format_duration(minutes: int) -> str:
    """
    Format duration in minutes as hours with unicode fractions,
    rounded down to the nearest quarter-hour.

    Examples: 180 -> "3h", 195 -> "3.25h", 210 -> "3.5h", 225 -> "3.75h".
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

    Title tiers (normal windows):
      - No mooring, no name: "Tidal Access (Xh)"
      - Mooring number only: "Access to #N (Xh)"
      - Name present (with or without mooring): "<n> Afloat (Xh)"
    Harmonic source prefixed with "est. "

    Always-accessible windows get a dedicated title with no duration
    (the concept doesn't apply to an unbounded window).
    """
    prefix = "est. " if source == "harmonic" else ""

    if window.get("always_accessible"):
        if boat_name:
            return f"\u2693 {prefix}{boat_name} - Always afloat"
        elif mooring_id:
            return f"\u2693 {prefix}#{mooring_id} - Always afloat"
        else:
            return f"\u2693 {prefix}Always afloat"

    duration = format_duration(window.get("duration_minutes", 0))

    if boat_name:
        return f"\u2693 {prefix}{boat_name} Afloat ({duration})"
    elif mooring_id:
        return f"\u2693 {prefix}Access to #{mooring_id} ({duration})"
    else:
        return f"\u2693 {prefix}Tidal Access ({duration})"


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

    if ev_data.get("always_accessible"):
        lines.append("Tide stays above threshold - always afloat this cycle")

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
                # Same tidal cycle - keep the more recently updated one
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
    # No METHOD property - subscription feeds should omit it.
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
        is_always = bool(ev_data.get("always_accessible"))

        start = dtparse.parse(ev_data["start_time"])
        end = dtparse.parse(ev_data["end_time"])

        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        # Recalculate title from current state. Pass always_accessible through
        # so the title can reflect it; pass duration in minutes for the normal
        # window case.
        duration_minutes = int((end - start).total_seconds() / 60)
        title_window = {
            "duration_minutes": duration_minutes,
            "always_accessible": is_always,
        }
        title = build_event_title(
            title_window, ev_data.get("source", "ukho"), boat_name, mooring_id
        )

        ical_event.add("summary", title)
        ical_event["uid"] = ev_data["event_uid"]

        if is_always:
            # Render as an all-day event on the HW's local date. The bounds
            # (LW-to-LW span, usually ~12h) are meaningless for "always
            # accessible", and a 12-hour time block would both look odd and
            # overlap with the next cycle at every LW. An all-day event is
            # a better fit semantically and avoids both problems.
            hw_local = dtparse.parse(ev_data["hw_timestamp"])
            if hw_local.tzinfo is None:
                hw_local = hw_local.replace(tzinfo=timezone.utc)
            hw_local = hw_local.astimezone(tz).date()
            ical_event.add("dtstart", hw_local)
            ical_event.add("dtend", hw_local + timedelta(days=1))
        else:
            ical_event.add("dtstart", start)
            ical_event.add("dtend", end)

        ical_event.add("description", _build_description(ev_data, tz, calibration))
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        # TRANSPARENT: the event does not block the user's time. Access windows
        # describe the opportunity to sail, not a commitment - they should
        # appear as free time in the user's calendar.
        ical_event.add("transp", "TRANSPARENT")
        ical_event.add("status", "CONFIRMED")
        cal.add_component(ical_event)

    feed_path = FEEDS_DIR / f"mooring_{mooring_id:03d}.ics"
    _atomic_write_bytes(feed_path, cal.to_ical())

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
        is_always = bool(w.get("always_accessible"))
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

        if is_always:
            # Always-accessible: render as an all-day event on HW's local date
            # (see feed generation for rationale).
            hw_local = dtparse.parse(w["hw_timestamp"])
            if hw_local.tzinfo is None:
                hw_local = hw_local.replace(tzinfo=timezone.utc)
            hw_local = hw_local.astimezone(tz).date()
            ical_event.add("dtstart", hw_local)
            ical_event.add("dtend", hw_local + timedelta(days=1))
        else:
            ical_event.add("dtstart", start)
            ical_event.add("dtend", end)

        ev_data = {
            "hw_timestamp": w["hw_timestamp"],
            "hw_height_m": w.get("hw_height_m"),
            "source": source,
            "wind_adjusted": w.get("wind_adjusted"),
            "always_accessible": is_always,
        }
        if calc_params:
            ev_data.update(calc_params)
        ical_event.add("description", _build_description(ev_data, tz, calibration))
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        # TRANSPARENT: see feed generation.
        ical_event.add("transp", "TRANSPARENT")
        cal.add_component(ical_event)

    return cal.to_ical()


def store_windows_as_events(windows: list[dict], mooring_id: int,
                            source: str, boat_name: str = "",
                            calc_params: dict = None, wind_details: dict = None):
    """Store computed access windows as calendar events in the database.

    calc_params: {draught_m, drying_height_m, safety_margin_m, obs_calibrated}
    wind_details: {direction, speed_ms, offset_m}

    Always-accessible windows are stored too, so that if the mooring is
    recalibrated later (e.g. a deeper-draught boat raises the threshold)
    the persisted events can be re-evaluated and updated on subsequent
    recalculation rather than being silently missing.
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
            "always_accessible": 1 if w.get("always_accessible") else 0,
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


# --- Standalone Langstone tide feeds (not per-mooring) ---
#
# Two feeds, both regenerated daily by the scheduler:
#   - Langstone_UKHO_7d.ics       UKHO Admiralty data, next 7 days
#   - Langstone_Harmonic_180d.ics UKHO for days 0-7, harmonic for days 7-180
#
# These differ from per-mooring feeds in three ways:
#   1. They list tide events (HW/LW), not access windows.
#   2. Each event is rendered as a 1-hour calendar slot centred on the tide
#      event time. Zero-duration events render poorly in many calendar apps;
#      a 1-hour slot is short enough to read "around now" and long enough
#      to be visible.
#   3. Event UIDs use cycle-number-since-epoch (matching the existing
#      access_calc.generate_event_uid pattern) so that when an event
#      transitions from harmonic to UKHO at the day-7 boundary, calendar
#      apps see an updated event rather than a delete-and-add.

# Reference defaults. The values actually used at runtime come from
# model_config.json (loaded via app.config.compute_cycle_number). These
# constants are kept here as readable documentation and as a fallback
# if the JSON is missing or malformed. To change the model behaviour,
# edit the JSON; do not edit these.
#
# Epoch for cycle numbering. Must match access_calc.generate_event_uid
# and the harmonic_predictions cycle_number column in database.py so
# that any cross-feed UID work is consistent.
_TIDE_UID_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_AVG_CYCLE_HOURS = 12.4167


def _tide_event_uid(timestamp_iso: str, event_type: str) -> str:
    """
    Build a stable UID for a Langstone tide event.

    Uses tide-cycle number since the fixed epoch, dividing hours-since-epoch
    by the average cycle length and rounding. Tolerant to source drift of up
    to about half a cycle (~6h) before the rounded cycle number changes;
    UKHO vs harmonic typically differ by a few tens of minutes at most, so
    the UID is stable across the harmonic->UKHO refinement.

    HW and LW within the same cycle share the same cycle number but get
    distinct UIDs via the event_type tag in the local part.

    Cycle epoch and length come from the shared helper in app.config
    (compute_cycle_number) so this UID is bit-for-bit identical to the
    one generated by access_calc.generate_event_uid for the same tide,
    and matches the cycle_number column in harmonic_predictions.
    """
    cycle = compute_cycle_number(timestamp_iso)
    et_short = "hw" if event_type == "HighWater" else "lw"
    return f"langstone-{et_short}-c{cycle:05d}@langstone"


def _build_tide_event(ev: dict, source_label: str, tz, is_estimate: bool = False) -> Event:
    """
    Build a single iCal Event for an HW/LW tide event.

    source_label: short string for the description body, e.g.
        'UKHO Langstone', 'UKHO Portsmouth (offset applied)', 'Harmonic model'.
    is_estimate: when True, the title is prefixed with 'est.' so the user
        can see at a glance which events are model-derived.
    """
    ical_event = Event()
    ts_iso = ev["timestamp"]
    dt = dtparse.parse(ts_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    et = ev["event_type"]
    et_label = "HW" if et == "HighWater" else "LW"
    height = ev["height_m"]

    # Title format: "⚓ HW 4.2m" or "⚓ est. LW ~1.0m".
    # The tilde and 'est.' prefix make the harmonic-derived events visually
    # distinct in the user's calendar without lengthening the title.
    if is_estimate:
        title = f"⚓ est. {et_label} ~{height:.1f}m"
    else:
        title = f"⚓ {et_label} {height:.1f}m"
    ical_event.add("summary", title)
    ical_event["uid"] = _tide_event_uid(ts_iso, et)

    # Event window: 1 hour centred on the tide event time. Calendar apps
    # render zero-duration events poorly; 30 minutes either side gives the
    # event a visible block that approximates "slack water either side of HW".
    start = dt - timedelta(minutes=30)
    end = dt + timedelta(minutes=30)
    ical_event.add("dtstart", start)
    ical_event.add("dtend", end)

    # Description: include local time, height, source. Keep it brief.
    local_dt = dt.astimezone(tz)
    tz_label = "BST" if local_dt.utcoffset().total_seconds() == 3600 else "GMT"
    desc_lines = [
        f"{et_label} at {local_dt.strftime('%H:%M')} {tz_label}",
        f"Height: {height:.1f}m",
        f"Source: {source_label}",
    ]
    if is_estimate:
        desc_lines.append(
            "Times typically accurate to +/-15-20min, heights to +/-0.15m."
        )
    ical_event.add("description", "\n".join(desc_lines))
    ical_event.add("dtstamp", datetime.now(timezone.utc))
    ical_event.add("transp", "TRANSPARENT")
    ical_event.add("status", "CONFIRMED")
    return ical_event


def generate_langstone_ukho_7d_feed() -> Path:
    """
    Regenerate the Langstone_UKHO_7d.ics feed file from stored UKHO data.

    Always returns the path; if no UKHO data is stored, the feed is written
    with no events but valid metadata so subscribers don't see a 404.

    Source-station handling:
      - get_ukho_tide_events() prefers native Langstone data.
      - If only Portsmouth fallback data is stored, the secondary-port offset
        is already applied by get_ukho_tide_events. The description string
        records which station the data originated from so the user can see
        whether their feed reflects native or fallback data.
    """
    ensure_dirs()
    from app.config import to_utc_str
    from app.database import get_tide_events

    now = datetime.now(timezone.utc)
    start = to_utc_str(now)
    end = to_utc_str(now + timedelta(days=7))

    # Determine which station provided the data (for the description string).
    # get_ukho_tide_events transparently applies the Portsmouth offset, but
    # we want the *user-visible* source label to be honest about provenance.
    native_check = get_tide_events(start, end, source="ukho", station="langstone")
    if native_check:
        source_label = "UKHO Langstone"
        events = native_check
    else:
        portsmouth = get_tide_events(start, end, source="ukho", station="portsmouth")
        if portsmouth:
            source_label = "UKHO Portsmouth (Langstone offset applied)"
            events = get_ukho_tide_events(start, end)  # applies the offset
        else:
            source_label = "UKHO"
            events = []

    cal = Calendar()
    cal.add("prodid", "-//Tidal Access Langstone UKHO 7d//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Langstone Tides - UKHO 7d")
    cal.add("x-wr-timezone", DEFAULT_TIMEZONE)
    cal.add("refresh-interval;value=duration", "PT12H")
    cal.add("x-published-ttl", "PT12H")

    tz = pytz.timezone(DEFAULT_TIMEZONE)
    for ev in events:
        cal.add_component(_build_tide_event(ev, source_label, tz, is_estimate=False))

    feed_path = FEEDS_DIR / "Langstone_UKHO_7d.ics"
    _atomic_write_bytes(feed_path, cal.to_ical())
    logger.info(
        f"Generated Langstone_UKHO_7d.ics: {len(events)} events ({source_label})"
    )
    return feed_path


def generate_langstone_harmonic_180d_feed() -> Path:
    """
    Regenerate the Langstone_Harmonic_180d.ics feed file.

    Composition:
      - Days 0..7:   UKHO data (preferred via get_ukho_tide_events).
      - Days 7..180: Latest stored harmonic predictions (Langstone-corrected
        already, since the scheduler applies the offset before storage).

    Where a harmonic event lies within 90 minutes of a UKHO event of the
    same type, the UKHO event wins (deduplicated). This handles the boundary
    fuzz where a tide ~7 days out is covered by both sources.

    Always returns the path. Empty data still produces a valid (empty) feed.
    """
    ensure_dirs()
    from app.config import to_utc_str

    now = datetime.now(timezone.utc)
    start = to_utc_str(now)
    end_ukho = to_utc_str(now + timedelta(days=7))
    end_180 = to_utc_str(now + timedelta(days=180))

    # Use get_ukho_tide_events so Portsmouth fallback is offset-corrected.
    ukho_events = get_ukho_tide_events(start, end_ukho)
    # Determine source label for UKHO portion as in the 7d feed.
    from app.database import get_tide_events
    native_check = get_tide_events(start, end_ukho, source="ukho", station="langstone")
    if native_check:
        ukho_label = "UKHO Langstone"
    elif get_tide_events(start, end_ukho, source="ukho", station="portsmouth"):
        ukho_label = "UKHO Portsmouth (Langstone offset applied)"
    else:
        ukho_label = "UKHO"

    # Harmonic predictions: pull only days 7..180 to avoid double-feeding
    # the days-0..7 region. Sort and de-duplicate against UKHO afterwards.
    harmonic_start = to_utc_str(now + timedelta(days=7))
    harmonic_events = get_harmonic_predictions(harmonic_start, end_180, latest_only=True)

    # Defensive deduplication: drop any harmonic event whose timestamp is
    # within 90 minutes of a UKHO event of the same type. UKHO wins.
    ukho_index = []
    for ue in ukho_events:
        ue_dt = dtparse.parse(ue["timestamp"])
        if ue_dt.tzinfo is None:
            ue_dt = ue_dt.replace(tzinfo=timezone.utc)
        ukho_index.append((ue_dt, ue["event_type"]))

    filtered_harmonic = []
    for he in harmonic_events:
        he_dt = dtparse.parse(he["timestamp"])
        if he_dt.tzinfo is None:
            he_dt = he_dt.replace(tzinfo=timezone.utc)
        clash = False
        for ukho_dt, ukho_et in ukho_index:
            if ukho_et != he["event_type"]:
                continue
            if abs((he_dt - ukho_dt).total_seconds()) < 5400:  # 90 min
                clash = True
                break
        if not clash:
            filtered_harmonic.append(he)

    cal = Calendar()
    cal.add("prodid", "-//Tidal Access Langstone Harmonic 180d//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Langstone Tides - 180d (UKHO + Harmonic est.)")
    cal.add("x-wr-timezone", DEFAULT_TIMEZONE)
    cal.add("refresh-interval;value=duration", "PT12H")
    cal.add("x-published-ttl", "PT12H")

    tz = pytz.timezone(DEFAULT_TIMEZONE)
    for ev in ukho_events:
        cal.add_component(_build_tide_event(ev, ukho_label, tz, is_estimate=False))
    for ev in filtered_harmonic:
        cal.add_component(_build_tide_event(
            ev, "Harmonic model (Langstone)", tz, is_estimate=True
        ))

    feed_path = FEEDS_DIR / "Langstone_Harmonic_180d.ics"
    _atomic_write_bytes(feed_path, cal.to_ical())
    logger.info(
        f"Generated Langstone_Harmonic_180d.ics: "
        f"{len(ukho_events)} UKHO + {len(filtered_harmonic)} harmonic events"
    )
    return feed_path
