"""
Job scheduler for automated data fetching and recalculation.

Two APScheduler-registered job types:
1. Fixed: Daily UKHO fetch at 02:00 (configurable). See daily_ukho_fetch.
2. Dynamic: per-mooring OWM wind observation jobs at each vessel's *worst-case
   grounding* (drying + draught + shallow_extra_depth_m), enumerated by
   ensure_wind_jobs_scheduled from the daily fetch, manual refresh, startup,
   and the conditions-refresh safety net. See wind_observation_job.

A wind reading taken at a grounding adjusts only the START of that mooring's
*next* access window (vessel and tender); the next grounding -- already
enumerated -- drives the sample for the window after that, and so on. The
"reactive" recalculation is the body of wind_observation_job; it is not a
separately-registered job type.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import (to_utc_str,
    UKHO_FETCH_HOUR, UKHO_FETCH_MINUTE,
    UKHO_STATION_ID, UKHO_FALLBACK_STATION_ID, DEFAULT_TIMEZONE,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)

# When several moorings ground close together, reuse a recent stored wind
# reading rather than making a near-identical OWM call per mooring. Wind is
# assumed roughly constant over this span (consistent with the persistence
# assumption the whole offset feature relies on).
WIND_REUSE_MAX_AGE_MINUTES = 15

# How far ahead to enumerate worst-case groundings for wind sampling. UKHO
# data typically spans about a week; we schedule against whatever exists.
WIND_SCHEDULE_HORIZON_DAYS = 14


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
    scheduler.add_job(
        conditions_refresh,
        "interval",
        minutes=15,
        id="conditions_refresh",
        replace_existing=True,
        name="Current conditions refresh (weather + pressure history)",
    )
    scheduler.start()
    logger.info(
        f"Scheduler started. Daily UKHO fetch at "
        f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}. "
        f"Conditions refresh every 15 min."
    )
    log_activity(
        event_type="scheduler_start",
        message=(
            f"Application started. Daily UKHO fetch scheduled for "
            f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}. "
            f"Per-mooring wind checks are scheduled at each vessel's worst-case "
            f"grounding once UKHO tide data is fetched (at the next scheduled "
            f"run, on startup, or via manual 'Refresh UKHO Data')."
        ),
        severity="info",
        details={
            "daily_ukho_fetch_time_local": f"{UKHO_FETCH_HOUR:02d}:{UKHO_FETCH_MINUTE:02d}",
            "timezone": DEFAULT_TIMEZONE,
        },
    )


async def daily_ukho_fetch():
    """
    Daily job: fetch UKHO data, store it, schedule wind observation jobs,
    and update calendar feeds for all enabled moorings.
    """
    from app.ukho import fetch_tidal_events
    from app.secondary_port import apply_offset
    from app.database import (
        store_tide_events, get_calendar_enabled_moorings,
        get_mooring, calibrate_drying_height, cleanup_old_events,
        cleanup_old_tide_data,
        store_harmonic_predictions, cleanup_old_harmonic_predictions,
        compute_harmonic_residuals,
        log_activity, prune_activity_log,
    )
    from app.harmonic import predict_events as harmonic_predict_events
    from app.access_calc import compute_access_windows
    from app.ical_manager import (
        store_windows_as_events, generate_feed_for_mooring,
        generate_langstone_ukho_7d_feed, generate_langstone_harmonic_180d_feed,
        generate_langstone_ukho_7d_pressure_corrected_feed,
    )

    logger.info("Running daily UKHO fetch...")

    events, station_used = await fetch_tidal_events()
    if not events:
        logger.error("No events returned from UKHO API")
        log_activity(
            event_type="ukho_fetch",
            message="Daily UKHO fetch returned no events",
            severity="error",
        )
        return

    station_label = "langstone" if station_used == UKHO_STATION_ID else "portsmouth"
    store_tide_events(events, source="ukho", station=station_label)
    logger.info(f"Stored {len(events)} UKHO events (station: {station_label})")

    fallback_note = f" via Portsmouth fallback (station {station_used})" if station_label == "portsmouth" else ""
    log_activity(
        event_type="ukho_fetch",
        message=f"Fetched {len(events)} tidal events from UKHO{fallback_note}",
        severity="success" if station_label == "langstone" else "warning",
        details={
            "event_count": len(events),
            "station_used": station_used,
            "station_label": station_label,
        },
    )

    # For window calculation, apply Langstone offset if Portsmouth fallback was used.
    # The stored events are raw Portsmouth values; calc_events are Langstone-corrected.
    if station_label == "portsmouth":
        calc_events = apply_offset(events)
    else:
        calc_events = events

    # Schedule per-mooring wind observation jobs at each worst-case grounding.
    # Reads moorings + freshly-stored tide events from the DB, so no args.
    ensure_wind_jobs_scheduled()

    # Recalculate access windows for all calendar-enabled moorings
    moorings = get_calendar_enabled_moorings()

    updated_count = 0
    for m in moorings:
        try:
            windows = compute_access_windows(
                events=calc_events,
                draught_m=m["draught_m"],
                drying_height_m=m["drying_height_m"],
                safety_margin_m=m["safety_margin_m"],
                source="ukho",
            )
            tender_windows = None
            tender_depth = None
            if m.get("tender_access_enabled"):
                tender_depth = float(m.get("tender_min_depth_m") or 0.3)
                tender_windows = compute_access_windows(
                    events=calc_events,
                    draught_m=0.0,
                    drying_height_m=m["drying_height_m"],
                    safety_margin_m=tender_depth,
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
                tender_windows=tender_windows,
                tender_min_depth_m=tender_depth,
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

    # --- Harmonic prediction refresh + standalone Langstone feeds ---
    # The Langstone tide feeds (UKHO 7d + 180d) are not per-mooring and run
    # once per day regardless of how many moorings are configured. They live
    # at the bottom of the daily job so a per-mooring failure above does not
    # block them.
    try:
        harmonic_start = datetime.now(timezone.utc)
        harmonic_end = harmonic_start + timedelta(days=180)
        # predict_events returns Portsmouth values; apply Langstone offset
        # before storing so consumers (feed + UI) work with corrected values.
        raw_harmonic = harmonic_predict_events(harmonic_start, harmonic_end)
        langstone_harmonic = apply_offset(raw_harmonic) if raw_harmonic else []
        inserted = store_harmonic_predictions(langstone_harmonic)
        log_activity(
            event_type="harmonic_refresh",
            message=f"Stored {inserted} harmonic predictions for the next 180 days",
            severity="success" if inserted else "warning",
            details={
                "event_count": inserted,
                "window_days": 180,
            },
        )
    except Exception as e:
        logger.error(f"Harmonic prediction refresh failed: {e}")
        log_activity(
            event_type="harmonic_refresh",
            message=f"Harmonic prediction refresh failed: {e}",
            severity="error",
        )

    try:
        generate_langstone_ukho_7d_feed()
        generate_langstone_harmonic_180d_feed()
        log_activity(
            event_type="langstone_feed_refresh",
            message="Regenerated Langstone_UKHO_7d.ics and Langstone_Harmonic_180d.ics",
            severity="info",
        )
    except Exception as e:
        logger.error(f"Langstone feed regeneration failed: {e}")
        log_activity(
            event_type="langstone_feed_refresh",
            message=f"Langstone feed regeneration failed: {e}",
            severity="error",
        )

    # --- Barometric pressure forecast refresh (v2.9, store-only) ---
    # Fetch the OWM 5-day/3-hourly pressure forecast and store it for the
    # barometric correction. STORE ONLY: no correction is applied to any feed
    # or window here -- the read/feed paths consume this table later, and only
    # once the master flag is enabled. A failure must not affect tide or feed
    # generation, so it is isolated at the bottom of the job in its own guard.
    try:
        from app.wind import fetch_pressure_forecast
        from app.database import (
            store_pressure_forecast, cleanup_old_pressure_forecast,
        )
        forecast_steps = await fetch_pressure_forecast()
        if forecast_steps:
            fetched_at = to_utc_str(datetime.now(timezone.utc))
            stored = store_pressure_forecast(forecast_steps, fetched_at)
            cleanup_old_pressure_forecast()
            log_activity(
                event_type="pressure_forecast_refresh",
                message=f"Stored {stored} pressure-forecast steps "
                        f"({forecast_steps[0]['timestamp']} .. {forecast_steps[-1]['timestamp']})",
                severity="success",
                details={
                    "step_count": stored,
                    "horizon_start": forecast_steps[0]["timestamp"],
                    "horizon_end": forecast_steps[-1]["timestamp"],
                },
            )
        else:
            log_activity(
                event_type="pressure_forecast_refresh",
                message="Pressure-forecast fetch returned no data",
                severity="warning",
            )
    except Exception as e:
        logger.error(f"Pressure forecast refresh failed: {e}")
        log_activity(
            event_type="pressure_forecast_refresh",
            message=f"Pressure forecast refresh failed: {e}",
            severity="error",
        )

    # --- Standalone pressure-corrected tide feed (v2.9) ---
    # Regenerate AFTER the forecast refresh above so it uses today's forecast.
    # The correction is gated on the master flag inside the generator, so while
    # the feature is dark this feed is byte-equivalent to Langstone_UKHO_7d.ics.
    try:
        generate_langstone_ukho_7d_pressure_corrected_feed()
        log_activity(
            event_type="langstone_feed_refresh",
            message="Regenerated Langstone_UKHO_7d_PressureCorrected.ics",
            severity="info",
        )
    except Exception as e:
        logger.error(f"Pressure-corrected feed regeneration failed: {e}")
        log_activity(
            event_type="langstone_feed_refresh",
            message=f"Pressure-corrected feed regeneration failed: {e}",
            severity="error",
        )

    # --- Continuous harmonic-vs-UKHO residual monitoring ---
    # Compute residuals over three rolling windows so trends and stable
    # state are both visible. Logged once per daily run as a single
    # activity_log row with structured details JSON. The 30-day window
    # is the threshold-bearing one (see calibration_thresholds below);
    # it is long enough to suppress single-storm noise, short enough to
    # respond to a real model drift within ~a month.
    #
    # On a fresh deployment the harmonic_predictions table only contains
    # rows from the day of first run, so historical comparison data
    # accumulates over time. Until then, longer windows naturally return
    # smaller `matched` counts and stats remain `None`. The threshold
    # check is gated on having enough data; it does not warn on first
    # run before history exists.
    try:
        residuals_7d = compute_harmonic_residuals(days=7)
        residuals_30d = compute_harmonic_residuals(days=30)
        residuals_90d = compute_harmonic_residuals(days=90)

        # Threshold logic: warning if the 30-day stats breach any of
        #   |HW height mean| > 0.10m, HW height RMS > 0.25m
        #   |LW height mean| > 0.10m, LW height RMS > 0.25m
        # The 30-day window must contain at least 20 matches (~half of
        # the ~58 expected in 30 days) to count - below that, sampling
        # noise dominates and a warning would be premature.
        MIN_30D_MATCHES_FOR_WARNING = 20
        HEIGHT_MEAN_THRESHOLD = 0.10
        HEIGHT_RMS_THRESHOLD = 0.25

        threshold_breaches = []
        hw30 = residuals_30d["hw"]
        lw30 = residuals_30d["lw"]
        if hw30["count"] >= MIN_30D_MATCHES_FOR_WARNING:
            if (hw30["height_mean"] is not None
                    and abs(hw30["height_mean"]) > HEIGHT_MEAN_THRESHOLD):
                threshold_breaches.append(
                    f"HW mean {hw30['height_mean']:+.3f}m"
                )
            if (hw30["height_rms"] is not None
                    and hw30["height_rms"] > HEIGHT_RMS_THRESHOLD):
                threshold_breaches.append(
                    f"HW RMS {hw30['height_rms']:.3f}m"
                )
        if lw30["count"] >= MIN_30D_MATCHES_FOR_WARNING:
            if (lw30["height_mean"] is not None
                    and abs(lw30["height_mean"]) > HEIGHT_MEAN_THRESHOLD):
                threshold_breaches.append(
                    f"LW mean {lw30['height_mean']:+.3f}m"
                )
            if (lw30["height_rms"] is not None
                    and lw30["height_rms"] > HEIGHT_RMS_THRESHOLD):
                threshold_breaches.append(
                    f"LW RMS {lw30['height_rms']:.3f}m"
                )

        # Build the human-readable message. Always lead with 30-day
        # numbers because they are the threshold-bearing window.
        if hw30["count"] == 0 and lw30["count"] == 0:
            msg = (
                "Harmonic residuals: no matches in 30-day window "
                "(insufficient history)"
            )
            severity = "info"
        else:
            hw_str = (
                f"HW n={hw30['count']} "
                f"mean={hw30['height_mean']:+.3f}m "
                f"RMS={hw30['height_rms']:.3f}m"
                if hw30["count"] > 0 else "HW n=0"
            )
            lw_str = (
                f"LW n={lw30['count']} "
                f"mean={lw30['height_mean']:+.3f}m "
                f"RMS={lw30['height_rms']:.3f}m"
                if lw30["count"] > 0 else "LW n=0"
            )
            base_msg = f"Harmonic vs UKHO 30d: {hw_str}; {lw_str}"
            if threshold_breaches:
                msg = f"{base_msg} - threshold breach: {'; '.join(threshold_breaches)}"
                severity = "warning"
            else:
                msg = base_msg
                severity = "info"

        log_activity(
            event_type="harmonic_residuals",
            message=msg,
            severity=severity,
            details={
                "thresholds": {
                    "window_days": 30,
                    "min_matches_for_warning": MIN_30D_MATCHES_FOR_WARNING,
                    "height_mean_abs_max_m": HEIGHT_MEAN_THRESHOLD,
                    "height_rms_max_m": HEIGHT_RMS_THRESHOLD,
                },
                "breaches": threshold_breaches,
                "window_7d": residuals_7d,
                "window_30d": residuals_30d,
                "window_90d": residuals_90d,
            },
        )
    except Exception as e:
        logger.error(f"Harmonic residual monitoring failed: {e}")
        log_activity(
            event_type="harmonic_residuals",
            message=f"Harmonic residual monitoring failed: {e}",
            severity="error",
        )

    cleanup_old_events(days=14)
    # Tide data retained for 12 months for the historical Tides tab view.
    # Without this cleanup, tide_data would grow unbounded since events
    # are written-once and never updated by routine operation.
    cleanup_old_tide_data(days=365)
    cleanup_old_harmonic_predictions(days=365)
    prune_activity_log(system_days=30, mooring_days=7)


def _wind_enabled_moorings() -> list[dict]:
    """Calendar-enabled moorings with a usable wind/shallow-water offset."""
    from app.database import get_calendar_enabled_moorings

    out = []
    for m in get_calendar_enabled_moorings():
        if not m.get("wind_offset_enabled"):
            continue
        if not (m.get("shallow_direction") or ""):
            continue
        if float(m.get("shallow_extra_depth_m") or 0.0) <= 0:
            continue
        out.append(m)
    return out


def _purge_wind_jobs():
    """Remove all scheduled per-mooring wind-sample jobs."""
    for job in scheduler.get_jobs():
        if (job.id or "").startswith("wind_sample_"):
            try:
                scheduler.remove_job(job.id)
            except Exception:
                pass


def ensure_wind_jobs_scheduled() -> list[dict]:
    """
    (Re)schedule per-mooring wind-sample jobs at each future worst-case
    grounding. Idempotent: purges existing wind_sample_* jobs and rebuilds
    from current UKHO data + mooring config, so it is safe to call from
    startup, the daily fetch, a manual refresh, a mooring-config save, and
    the periodic conditions-refresh safety net.

    Trigger time per mooring/tide is the ebb crossing of
    ``drying + draught + shallow_extra_depth_m`` -- the *worst-case* grounding,
    the earliest the boat could touch if the wind pushes it into the shallows.
    Sampling there means we read the wind at the first moment grounding is
    possible; if the wind turns out favourable we simply record no offset.

    Threshold (config offset, always) vs effect (wind-conditional) is the key
    distinction that keeps trigger times deterministic: the schedule uses the
    configured offset unconditionally, while whether the offset is *applied* to
    the next window depends on the wind actually read at fire time.

    Where a tide is too low to reach the worst-case level but the boat still
    floats, we fall back to the real keel-line grounding (``drying + draught``)
    so the chain does not break on a no-access tide. A tide that never grounds
    even in the worst case (deep mooring, high neap LW) correctly gets no
    trigger -- there is no grounding risk to sample for.

    Returns a list of scheduled job descriptors (for logging/inspection).
    """
    from app.database import get_ukho_tide_events, log_activity
    from app.access_calc import compute_access_windows

    now = datetime.now(timezone.utc)
    events = get_ukho_tide_events(
        to_utc_str(now - timedelta(hours=12)),
        to_utc_str(now + timedelta(days=WIND_SCHEDULE_HORIZON_DAYS)),
    )
    moorings = _wind_enabled_moorings()

    _purge_wind_jobs()

    if not events or not moorings:
        return []

    sorted_hws = sorted(
        [e for e in events if e["event_type"] == "HighWater"],
        key=lambda e: e["timestamp"],
    )

    scheduled = []
    for m in moorings:
        draught = m["draught_m"]
        drying = m["drying_height_m"]
        offset = float(m["shallow_extra_depth_m"])

        # Worst-case groundings: ebb crossings of drying+draught+offset.
        # margin=0 windows give the real keel-line grounding for the fallback.
        worst = compute_access_windows(events, draught, drying, offset, source="ukho")
        real = compute_access_windows(events, draught, drying, 0.0, source="ukho")
        real_by_hw = {w["hw_timestamp"]: w for w in real}

        for w in worst:
            if w.get("always_accessible"):
                # Never grounds even in the worst case -> no grounding risk.
                continue

            ground_end = None
            if not w.get("below_threshold") and not w.get("incomplete_data"):
                ground_end = w.get("end_time")
            if not ground_end:
                # Worst-case threshold not reached this tide; fall back to the
                # real keel-line grounding so a low/no-access tide still fires.
                rw = real_by_hw.get(w["hw_timestamp"])
                if rw and not rw.get("always_accessible") and not rw.get("below_threshold"):
                    ground_end = rw.get("end_time")
            if not ground_end:
                continue

            ground_dt = dtparse.parse(ground_end)
            if ground_dt.tzinfo is None:
                ground_dt = ground_dt.replace(tzinfo=timezone.utc)
            if ground_dt <= now:
                continue

            # The wind read at this grounding adjusts the NEXT high water's
            # window start. Normalise to canonical UTC-Z form so it matches the
            # hw_timestamp that compute_access_windows produces internally.
            next_hw_ts = None
            for he in sorted_hws:
                he_dt = dtparse.parse(he["timestamp"])
                if he_dt.tzinfo is None:
                    he_dt = he_dt.replace(tzinfo=timezone.utc)
                if he_dt > ground_dt:
                    next_hw_ts = to_utc_str(he_dt)
                    break
            if next_hw_ts is None:
                continue

            job_id = (
                f"wind_sample_m{m['mooring_id']}_"
                f"{ground_dt.strftime('%Y%m%dT%H%M')}"
            )
            try:
                scheduler.add_job(
                    wind_observation_job,
                    DateTrigger(run_date=ground_dt),
                    id=job_id,
                    replace_existing=True,
                    name=(
                        f"Wind sample m{m['mooring_id']} @ worst-case grounding "
                        f"{ground_dt.strftime('%d %b %H:%M')}"
                    ),
                    kwargs={
                        "mooring_id": m["mooring_id"],
                        "next_hw_timestamp": next_hw_ts,
                    },
                )
                scheduled.append({
                    "mooring_id": m["mooring_id"],
                    "grounding": to_utc_str(ground_dt),
                    "next_hw": next_hw_ts,
                })
            except Exception as e:
                logger.warning(f"Could not schedule wind job {job_id}: {e}")

    if scheduled:
        logger.info(
            f"Scheduled {len(scheduled)} wind sample(s) at worst-case "
            f"groundings for {len(moorings)} mooring(s)"
        )
        log_activity(
            event_type="wind_schedule",
            message=(
                f"Scheduled {len(scheduled)} wind sample(s) at worst-case "
                f"groundings across {len(moorings)} wind-enabled mooring(s)"
            ),
            severity="info",
            details={"count": len(scheduled), "samples": scheduled[:20]},
        )
    return scheduled


async def _get_wind_for_sample():
    """
    Return a wind reading for an offset check, reusing a very recent stored
    observation when one exists (so several moorings grounding close together
    don't each trigger a near-identical OWM call), otherwise fetching fresh and
    storing it. Returns a dict with direction_compass / direction_deg /
    speed_ms / timestamp, or None on failure.
    """
    from app.wind import fetch_current_wind
    from app.database import get_latest_wind, store_wind_observation

    recent = get_latest_wind()
    if recent and recent.get("direction_compass"):
        try:
            ts = dtparse.parse(recent["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
            if 0 <= age_min <= WIND_REUSE_MAX_AGE_MINUTES:
                return {
                    "direction_deg": recent.get("direction_deg"),
                    "direction_compass": recent["direction_compass"],
                    "speed_ms": recent.get("speed_ms") or 0.0,
                    "timestamp": recent["timestamp"],
                }
        except (ValueError, TypeError):
            pass

    wind = await fetch_current_wind()
    if wind:
        store_wind_observation(
            timestamp=wind["timestamp"],
            direction_deg=wind["direction_deg"],
            direction_compass=wind["direction_compass"],
            speed_ms=wind["speed_ms"],
        )
    return wind


async def wind_observation_job(mooring_id: int, next_hw_timestamp: str):
    """
    Fired at a mooring's worst-case grounding: read the wind now and adjust the
    START of that mooring's NEXT access window (vessel and tender), leaving the
    ebb-side grounding at baseline.

    Writes exactly the one next-HW event (upsert by cycle UID) so it cannot
    clobber a sibling window already adjusted by an earlier grounding's job.
    """
    from app.wind import should_apply_offset
    from app.database import (
        get_mooring, get_ukho_tide_events, calibrate_drying_height, log_activity,
    )
    from app.access_calc import compute_next_window_with_wind
    from app.ical_manager import store_windows_as_events, generate_feed_for_mooring

    m = get_mooring(mooring_id)
    if not m or not m.get("wind_offset_enabled"):
        return
    shallow_dir = m.get("shallow_direction") or ""
    extra_depth = float(m.get("shallow_extra_depth_m") or 0.0)
    if not shallow_dir or extra_depth <= 0:
        # Config changed since the job was scheduled; nothing to do.
        return

    logger.info(
        f"Wind observation job: mooring {mooring_id}, next HW {next_hw_timestamp}"
    )

    wind = await _get_wind_for_sample()
    if not wind:
        log_activity(
            event_type="wind_check",
            message="Wind check failed — OWM API did not return data",
            severity="error",
            scope="mooring",
            mooring_id=mooring_id,
            details={"next_hw_timestamp": next_hw_timestamp},
        )
        return

    offset_triggered = should_apply_offset(wind["direction_compass"], shallow_dir)
    wind_offset = extra_depth if offset_triggered else 0.0
    wind_kts = (wind.get("speed_ms") or 0.0) * 1.944

    log_activity(
        event_type="wind_check",
        message=f"Wind at grounding: {wind['direction_compass']} {wind_kts:.0f}kts",
        severity="info",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "next_hw_timestamp": next_hw_timestamp,
            "direction_compass": wind["direction_compass"],
            "direction_deg": wind.get("direction_deg"),
            "speed_ms": wind.get("speed_ms"),
            "speed_kts": round(wind_kts, 1),
        },
    )

    if offset_triggered:
        logger.info(
            f"Mooring {mooring_id}: wind {wind['direction_compass']}, shallow to "
            f"{shallow_dir} — applying +{extra_depth}m to next window start"
        )
        log_activity(
            event_type="wind_offset",
            message=(
                f"Offset APPLIED: wind {wind['direction_compass']} "
                f"({wind_kts:.0f}kts) pushing toward shallow side ({shallow_dir}) "
                f"— +{extra_depth:.1f}m to the next window's start"
            ),
            severity="warning",
            scope="mooring",
            mooring_id=mooring_id,
            details={
                "wind_direction": wind["direction_compass"],
                "wind_speed_kts": round(wind_kts, 1),
                "shallow_direction": shallow_dir,
                "offset_m": extra_depth,
                "applied": True,
                "next_hw_timestamp": next_hw_timestamp,
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
            mooring_id=mooring_id,
            details={
                "wind_direction": wind["direction_compass"],
                "wind_speed_kts": round(wind_kts, 1),
                "shallow_direction": shallow_dir,
                "offset_m": 0,
                "applied": False,
                "next_hw_timestamp": next_hw_timestamp,
            },
        )

    # Fetch enough tide data to bracket the next HW (flood LW before, ebb LW
    # after). get_ukho_tide_events applies the Langstone offset to any
    # Portsmouth fallback data automatically.
    hw_dt = dtparse.parse(next_hw_timestamp)
    if hw_dt.tzinfo is None:
        hw_dt = hw_dt.replace(tzinfo=timezone.utc)
    events = get_ukho_tide_events(
        to_utc_str(hw_dt - timedelta(hours=8)),
        to_utc_str(hw_dt + timedelta(hours=8)),
    )
    if not events:
        logger.info("No tide events around next HW for wind adjustment")
        return

    try:
        window = compute_next_window_with_wind(
            events, m["draught_m"], m["drying_height_m"], m["safety_margin_m"],
            next_hw_timestamp, wind_offset, source="ukho",
        )
        if window is None:
            logger.info(
                f"Next HW {next_hw_timestamp} not found in events; skipping"
            )
            return

        tender_windows = None
        tender_depth = None
        if m.get("tender_access_enabled"):
            tender_depth = float(m.get("tender_min_depth_m") or 0.3)
            tw = compute_next_window_with_wind(
                events, 0.0, m["drying_height_m"], tender_depth,
                next_hw_timestamp, wind_offset, source="ukho",
            )
            if tw is not None:
                tender_windows = [tw]

        cal = calibrate_drying_height(mooring_id)
        calc_params = {
            "draught_m": m["draught_m"],
            "drying_height_m": m["drying_height_m"],
            "safety_margin_m": m["safety_margin_m"],
            "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
        }
        wind_details = {
            "direction": wind["direction_compass"],
            "speed_ms": wind.get("speed_ms"),
            "offset_m": wind_offset,
        }
        store_windows_as_events(
            [window], mooring_id, "ukho", m.get("boat_name", ""),
            calc_params=calc_params, wind_details=wind_details,
            tender_windows=tender_windows, tender_min_depth_m=tender_depth,
        )
        if cal.get("confidence") == "none":
            cal = None
        generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), cal)
    except Exception as e:
        logger.error(f"Wind recalc failed for mooring {mooring_id}: {e}")


async def conditions_refresh():
    """
    Scheduled every 15 minutes: fetches fresh weather from OWM, computes
    current tide state, stores the pressure reading for trend calculation,
    and warms the conditions cache so /api/conditions serves instantly.

    This job runs independently of any user viewing the page. Without it
    the pressure_history table would only accumulate readings when a user
    happens to visit, making the 3-hour pressure trend unreliable.
    """
    try:
        from app.conditions import get_current_conditions
        await get_current_conditions(force_refresh=True)
    except Exception as e:
        logger.error(f"Conditions refresh failed: {e}")

    # Safety net for the in-memory job store: if a restart wiped the
    # per-mooring wind-sample jobs, rebuild them. This is a cheap no-op when
    # they already exist, so it does not churn the schedule every 15 minutes.
    try:
        if not any(
            (j.id or "").startswith("wind_sample_") for j in scheduler.get_jobs()
        ):
            ensure_wind_jobs_scheduled()
    except Exception as e:
        logger.error(f"Wind-job safety-net reschedule failed: {e}")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
