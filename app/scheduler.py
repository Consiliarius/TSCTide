"""
Job scheduler for automated data fetching and recalculation.

Three job types:
1. Fixed: Daily UKHO fetch at 02:00 (configurable)
2. Dynamic: OWM wind observation at HW+offset for each ebb tide
3. Reactive: Recalculate access windows after wind data arrives
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import (to_utc_str, 
    UKHO_FETCH_HOUR, UKHO_FETCH_MINUTE, WIND_SAMPLE_HW_OFFSET_HOURS,
    UKHO_STATION_ID, UKHO_FALLBACK_STATION_ID, DEFAULT_TIMEZONE,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)


def start_scheduler():
    """Start the scheduler and register the daily UKHO job."""
    from app.database import log_activity

    scheduler.add_job(
        daily_ukho_fetch,
        CronTrigger(hour=UKHO_FETCH_HOUR, minute=UKHO_FETCH_MINUTE),
        id="daily_ukho_fetch",
        replace_existing=True,
        name="Daily UKHO tide data fetch",
    )
    scheduler.start()
    logger.info(
        f"Scheduler started. Daily UKHO fetch at "
        f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}"
    )
    log_activity(
        event_type="scheduler_start",
        message=(
            f"Application started. Daily UKHO fetch scheduled for "
            f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}. "
            f"OWM wind checks will be scheduled at HW+{WIND_SAMPLE_HW_OFFSET_HOURS:g}h "
            f"once UKHO tide data is fetched (either at the next scheduled run "
            f"or via manual 'Refresh UKHO Data')."
        ),
        severity="info",
        details={
            "daily_ukho_fetch_time_local": f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}",
            "timezone": DEFAULT_TIMEZONE,
            "wind_sample_hw_offset_hours": WIND_SAMPLE_HW_OFFSET_HOURS,
        },
    )


async def daily_ukho_fetch():
    """
    Daily job: fetch UKHO data, store it, schedule wind observation jobs,
    and update calendar feeds for all enabled moorings.
    """
    from app.ukho import fetch_tidal_events
    from app.database import (
        store_tide_events, get_calendar_enabled_moorings,
        get_mooring, calibrate_drying_height, cleanup_old_events,
        log_activity, prune_activity_log,
    )
    from app.access_calc import compute_access_windows
    from app.ical_manager import store_windows_as_events, generate_feed_for_mooring

    logger.info("Running daily UKHO fetch...")

    # Fetch tidal events
    events = await fetch_tidal_events()
    if not events:
        logger.error("No events returned from UKHO API")
        log_activity(
            event_type="ukho_fetch",
            message="Daily UKHO fetch returned no events",
            severity="error",
        )
        return

    store_tide_events(events, source="ukho", station="langstone")
    logger.info(f"Stored {len(events)} UKHO events")
    log_activity(
        event_type="ukho_fetch",
        message=f"Fetched {len(events)} tidal events from UKHO",
        severity="success",
        details={"event_count": len(events), "station": UKHO_STATION_ID},
    )

    # Schedule wind observation jobs for today's ebbing tides
    await _schedule_wind_jobs(events)

    # Recalculate access windows for all calendar-enabled moorings
    moorings = get_calendar_enabled_moorings()
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)

    updated_count = 0
    for m in moorings:
        try:
            windows = compute_access_windows(
                events=events,
                draught_m=m["draught_m"],
                drying_height_m=m["drying_height_m"],
                safety_margin_m=m["safety_margin_m"],
                source="ukho",
            )
            cal = calibrate_drying_height(m["mooring_id"])
            calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            store_windows_as_events(
                windows, m["mooring_id"], "ukho", m.get("boat_name", ""),
                calc_params=calc_params,
            )
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(m["mooring_id"], m.get("boat_name", ""), cal)
            updated_count += 1
            log_activity(
                event_type="feed_generation",
                message=f"Calendar feed refreshed with {len(windows)} access windows",
                severity="info",
                scope="mooring",
                mooring_id=m["mooring_id"],
                details={
                    "window_count": len(windows),
                    "trigger": "daily_scheduler",
                    "boat_name": m.get("boat_name", ""),
                },
            )
        except Exception as e:
            logger.error(f"Failed to update mooring {m['mooring_id']}: {e}")
            log_activity(
                event_type="feed_generation",
                message=f"Failed to update feed: {e}",
                severity="error",
                scope="mooring",
                mooring_id=m["mooring_id"],
            )

    log_activity(
        event_type="daily_scheduler",
        message=f"Daily update complete: {updated_count}/{len(moorings)} moorings refreshed",
        severity="success" if updated_count == len(moorings) else "warning",
        details={"mooring_count": len(moorings), "updated": updated_count},
    )

    # Housekeeping: remove calendar events older than 14 days
    cleanup_old_events(days=14)
    prune_activity_log(system_days=30, mooring_days=7)


async def _schedule_wind_jobs(events: list[dict]) -> list[dict]:
    """
    Schedule OWM wind observation calls at HW+offset for each high water
    in the fetched data. Only schedules for future times.
    Returns a list of scheduled job details for logging.
    """
    from app.wind import fetch_current_wind
    from app.database import store_wind_observation, log_activity

    now = datetime.now(timezone.utc)
    offset_hours = WIND_SAMPLE_HW_OFFSET_HOURS

    hw_events = [e for e in events if e["event_type"] == "HighWater"]
    scheduled = []

    for hw in hw_events:
        hw_dt = dtparse.parse(hw["timestamp"])
        if hw_dt.tzinfo is None:
            hw_dt = hw_dt.replace(tzinfo=timezone.utc)

        sample_time = hw_dt + timedelta(hours=offset_hours)

        # Only schedule if in the future
        if sample_time <= now:
            continue

        job_id = f"wind_sample_{hw_dt.strftime('%Y%m%dT%H%M')}"

        try:
            scheduler.add_job(
                wind_observation_job,
                DateTrigger(run_date=sample_time),
                id=job_id,
                replace_existing=True,
                name=f"Wind sample at HW+{offset_hours}h ({hw_dt.strftime('%H:%M')})",
                kwargs={"hw_timestamp": hw["timestamp"]},
            )
            logger.info(f"Scheduled wind observation at {sample_time.isoformat()}")
            scheduled.append({
                "hw_timestamp": hw["timestamp"],
                "sample_time": to_utc_str(sample_time),
            })
        except Exception as e:
            logger.warning(f"Could not schedule wind job: {e}")

    # Summarise upcoming wind checks in activity log
    if scheduled:
        # Display up to 4 upcoming times in local format for readability
        import pytz
        tz = pytz.timezone(DEFAULT_TIMEZONE)
        display_count = min(4, len(scheduled))
        sample_times_local = []
        for s in scheduled[:display_count]:
            dt = dtparse.parse(s["sample_time"])
            local = dt.astimezone(tz)
            sample_times_local.append(local.strftime("%d %b %H:%M"))
        more = f" (+{len(scheduled) - display_count} more)" if len(scheduled) > display_count else ""
        log_activity(
            event_type="wind_schedule",
            message=(
                f"Scheduled {len(scheduled)} OWM wind check(s) at HW+{offset_hours:g}h. "
                f"Next: {', '.join(sample_times_local)}{more}"
            ),
            severity="info",
            details={
                "count": len(scheduled),
                "offset_hours": offset_hours,
                "upcoming_sample_times": [s["sample_time"] for s in scheduled],
            },
        )
    else:
        log_activity(
            event_type="wind_schedule",
            message=f"No future HW events to schedule wind checks against",
            severity="warning",
            details={"offset_hours": offset_hours},
        )
    return scheduled


async def wind_observation_job(hw_timestamp: str):
    """
    Triggered at HW+offset: fetch current wind and recalculate
    the next flood tide's access window for all wind-enabled moorings.
    """
    from app.wind import fetch_current_wind, should_apply_offset
    from app.database import (
        store_wind_observation, get_calendar_enabled_moorings,
        get_tide_events, calibrate_drying_height, log_activity,
    )
    from app.access_calc import compute_access_windows
    from app.ical_manager import store_windows_as_events, generate_feed_for_mooring

    logger.info(f"Running wind observation job for HW {hw_timestamp}")

    wind = await fetch_current_wind()
    if not wind:
        logger.warning("Could not fetch wind data")
        log_activity(
            event_type="wind_check",
            message="Wind check failed — OWM API did not return data",
            severity="error",
            details={"hw_timestamp": hw_timestamp},
        )
        return

    # Store the observation
    store_wind_observation(
        timestamp=wind["timestamp"],
        direction_deg=wind["direction_deg"],
        direction_compass=wind["direction_compass"],
        speed_ms=wind["speed_ms"],
    )

    wind_kts = wind["speed_ms"] * 1.944
    log_activity(
        event_type="wind_check",
        message=f"Wind at HW+4h: {wind['direction_compass']} {wind_kts:.0f}kts",
        severity="info",
        details={
            "hw_timestamp": hw_timestamp,
            "direction_compass": wind["direction_compass"],
            "direction_deg": wind["direction_deg"],
            "speed_ms": wind["speed_ms"],
            "speed_kts": round(wind_kts, 1),
        },
    )

    # Find the next HW after this one (the next flood tide)
    hw_dt = dtparse.parse(hw_timestamp)
    if hw_dt.tzinfo is None:
        hw_dt = hw_dt.replace(tzinfo=timezone.utc)

    # Search for tide events around the next expected HW (~12h25m later)
    search_start = to_utc_str(hw_dt + timedelta(hours=4))
    search_end = to_utc_str(hw_dt + timedelta(hours=18))
    next_events = get_tide_events(search_start, search_end, source="ukho")

    if not next_events:
        logger.info("No upcoming tide events found for wind adjustment")
        return

    # Recalculate for wind-enabled moorings
    moorings = get_calendar_enabled_moorings()
    for m in moorings:
        if not m.get("wind_offset_enabled"):
            continue

        shallow_dir = m.get("shallow_direction", "")
        extra_depth = m.get("shallow_extra_depth_m", 0.0)

        if not shallow_dir or extra_depth <= 0:
            continue

        offset_triggered = should_apply_offset(wind["direction_compass"], shallow_dir)
        wind_offset = extra_depth if offset_triggered else 0.0

        if offset_triggered:
            logger.info(
                f"Mooring {m['mooring_id']}: wind from {wind['direction_compass']}, "
                f"shallow to {shallow_dir} — applying +{extra_depth}m offset"
            )
            log_activity(
                event_type="wind_offset",
                message=(
                    f"Offset APPLIED: wind {wind['direction_compass']} "
                    f"({wind_kts:.0f}kts) pushing toward shallow side ({shallow_dir}) "
                    f"— added +{extra_depth:.1f}m to drying height"
                ),
                severity="warning",
                scope="mooring",
                mooring_id=m["mooring_id"],
                details={
                    "wind_direction": wind["direction_compass"],
                    "wind_speed_kts": round(wind_kts, 1),
                    "shallow_direction": shallow_dir,
                    "offset_m": extra_depth,
                    "applied": True,
                },
            )
        else:
            log_activity(
                event_type="wind_offset",
                message=(
                    f"Offset not applied: wind {wind['direction_compass']} "
                    f"({wind_kts:.0f}kts) not pushing toward shallow side ({shallow_dir})"
                ),
                severity="info",
                scope="mooring",
                mooring_id=m["mooring_id"],
                details={
                    "wind_direction": wind["direction_compass"],
                    "wind_speed_kts": round(wind_kts, 1),
                    "shallow_direction": shallow_dir,
                    "offset_m": 0,
                    "applied": False,
                },
            )

        try:
            windows = compute_access_windows(
                events=next_events,
                draught_m=m["draught_m"],
                drying_height_m=m["drying_height_m"],
                safety_margin_m=m["safety_margin_m"],
                wind_offset_m=wind_offset,
                source="ukho",
            )
            for w in windows:
                w["wind_adjusted"] = 1 if wind_offset > 0 else 0

            cal = calibrate_drying_height(m["mooring_id"])
            calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            wind_details = {
                "direction": wind["direction_compass"],
                "speed_ms": wind["speed_ms"],
                "offset_m": wind_offset,
            }
            store_windows_as_events(
                windows, m["mooring_id"], "ukho", m.get("boat_name", ""),
                calc_params=calc_params, wind_details=wind_details,
            )
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(m["mooring_id"], m.get("boat_name", ""), cal)
        except Exception as e:
            logger.error(f"Wind recalc failed for mooring {m['mooring_id']}: {e}")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
