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

Since v2.11.1 a wind adjustment survives later recomputes of the same tide.
The daily refresh (and, in app.main, calibration Apply, Save Configuration
and Update Feed) passes its fresh baseline windows through
reapply_stored_wind, which re-applies each stored wind outcome to the fresh
tide data via compute_wind_window, the same computation the wind job uses.
Before this, a baseline write replaced the wind-adjusted row, and because the
wind job for that tide had already fired, the adjustment was lost for good.
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
        log_activity, prune_activity_log, get_calendar_events,
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

    # Barometric correction (v2.9): the correction value is system-level (one
    # pressure series, one k -- every opted-in mooring gets the identical shift
    # at a given tide), so build the corrected events ONCE and reuse them for
    # every opted-in mooring. Only the opt-in decision is per-mooring. Gated on
    # the system master, so while the feature is dark calc_events is used
    # unchanged and store_windows_as_events is called exactly as pre-v2.9.
    from app.config import (
        get_barometric_enabled, get_barometric_window_deadband_minutes,
        get_barometric_forecast_staleness_hours,
    )
    barometric_master = get_barometric_enabled(False)
    barometric_events = None
    barometric_deadband = None
    barometric_diags = None
    if barometric_master:
        from app.barometric import apply_barometric_correction, make_pressure_provider
        barometric_diags = []
        barometric_events = apply_barometric_correction(
            calc_events, make_pressure_provider(), diagnostics=barometric_diags
        )
        barometric_deadband = get_barometric_window_deadband_minutes(5)

    updated_count = 0
    for m in moorings:
        try:
            m_barometric = barometric_master and bool(m.get("barometric_enabled"))
            m_events = barometric_events if m_barometric else calc_events
            m_deadband = barometric_deadband if m_barometric else None

            windows = compute_access_windows(
                events=m_events,
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
                    events=m_events,
                    draught_m=0.0,
                    drying_height_m=m["drying_height_m"],
                    safety_margin_m=tender_depth,
                    source="ukho",
                )
            window_count = len(windows)
            # Keep any stored wind adjustment: re-apply it to these fresh
            # events rather than overwriting it with the baseline (v2.11.1).
            # Same events as the baseline, so the two cannot diverge.
            windows, tender_windows, wind_stats = reapply_stored_wind(
                m, windows, tender_windows,
                get_calendar_events(m["mooring_id"]), events=m_events,
            )
            cal = calibrate_drying_height(m["mooring_id"])
            calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            store_result = store_windows_as_events(
                windows, m["mooring_id"], "ukho", m.get("boat_name", ""),
                calc_params=calc_params,
                tender_windows=tender_windows,
                tender_min_depth_m=tender_depth,
                deadband_minutes=m_deadband,
            )
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(m["mooring_id"], m.get("boat_name", ""), cal)
            updated_count += 1
            log_activity(
                event_type="feed_generation",
                message=f"Calendar feed refreshed with {window_count} access windows",
                severity="warning" if wind_stats["failed"] else "info",
                scope="mooring",
                mooring_id=m["mooring_id"],
                details={
                    "window_count": window_count,
                    "trigger": "daily_scheduler",
                    "boat_name": m.get("boat_name", ""),
                    "barometric_applied": m_barometric,
                    "windows_written": store_result.get("written"),
                    "windows_unchanged_deadband": store_result.get("skipped"),
                    "windows_removed_below_threshold": store_result.get("removed"),
                    "windows_incomplete": store_result.get("incomplete"),
                    "windows_wind_preserved": wind_stats["preserved"],
                    "windows_wind_reverted": wind_stats["reverted"],
                    "windows_wind_failed": wind_stats["failed"],
                    "windows_wind_failed_deleted": wind_stats["deleted"],
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

    # --- Barometric correction operational record (v2.9, Session G) ---
    # One system-scoped entry per daily run summarising what the correction did
    # this cycle: the correction value is system-level (identical for every
    # opted-in mooring), so a single record is the right granularity. Logged
    # only when the master is on; while the feature is dark nothing is written.
    if barometric_master and barometric_diags is not None:
        from app.barometric import summarize_diagnostics
        summary = summarize_diagnostics(barometric_diags)
        opted_in = sum(1 for m in moorings if m.get("barometric_enabled"))
        staleness_h = get_barometric_forecast_staleness_hours(36)
        if summary["events_corrected"] > 0:
            msg = (
                f"Barometric correction: {summary['events_corrected']}/"
                f"{summary['events_total']} events corrected "
                f"({summary.get('pressure_min_hpa')}-{summary.get('pressure_max_hpa')} hPa, "
                f"max |delta| {summary.get('max_abs_correction_m')} m); "
                f"{opted_in} mooring(s) opted in"
            )
            if summary["clamp_fires"]:
                msg += f"; {summary['clamp_fires']} clamp fire(s)"
            severity = "warning" if summary["clamp_fires"] else "info"
        else:
            msg = (
                f"Barometric correction: no events corrected "
                f"(no forecast within {staleness_h:g}h); feeds at baseline"
            )
            severity = "info"
        log_activity(
            event_type="barometric_correction",
            message=msg,
            severity=severity,
            details={"opted_in_moorings": opted_in, **summary},
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
        # 180 days of synthesis is ~43k trig evaluations; offload it to a
        # worker thread so the 02:00 job does not block the event loop (and
        # any HTTP request handled on it) for the duration of the computation.
        raw_harmonic = await asyncio.to_thread(
            harmonic_predict_events, harmonic_start, harmonic_end
        )
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


def compute_wind_window(m: dict, hw_timestamp: str, wind_offset: float,
                        events: list[dict] = None, params: dict = None):
    """
    Compute the wind-adjusted window (vessel, and tender when enabled) for the
    high water at ``hw_timestamp`` on mooring ``m``, applying ``wind_offset``
    (metres; 0 for favourable wind) to the window START only.

    Shared by wind_observation_job and reapply_stored_wind, so a re-applied
    adjustment is computed exactly as the original was.

    events: tide events to compute from, used as given (the caller has already
        applied, or decided against, the barometric correction). When None,
        the events within 8 hours either side of the high water are read from
        the database and the barometric correction is applied if the system
        master is on and the mooring opted in; this is the wind job's path.
        Callers that hold the events their baseline was computed from should
        pass them, so the wind window and its neighbours share one data set
        (the database can hold a superseded row for a revised high-water time).
    params: optional {draught_m, drying_height_m, safety_margin_m} overriding
        the mooring's stored values for the vessel window (Update Feed). The
        tender pass always uses the stored drying height and tender depth, as
        /api/calculate does.

    Synchronous by design: callers rely on there being no await between
    reading stored rows and writing them.

    Returns (window, tender_windows or None, tender_depth or None), or None
    when no tide data covers the high water or it is not among the events.
    """
    from app.database import get_ukho_tide_events
    from app.access_calc import compute_next_window_with_wind

    if events is None:
        hw_dt = dtparse.parse(hw_timestamp)
        if hw_dt.tzinfo is None:
            hw_dt = hw_dt.replace(tzinfo=timezone.utc)
        # Enough tide data to bracket the HW (flood LW before, ebb LW after).
        # get_ukho_tide_events applies the Langstone offset to any Portsmouth
        # fallback data automatically.
        events = get_ukho_tide_events(
            to_utc_str(hw_dt - timedelta(hours=8)),
            to_utc_str(hw_dt + timedelta(hours=8)),
        )
        if not events:
            return None
        # Barometric correction (v2.9) composes with the wind offset:
        # barometric shifts event heights, wind shifts the threshold. Gated on
        # the system master AND this mooring's opt-in.
        from app.config import get_barometric_enabled
        if get_barometric_enabled(False) and m.get("barometric_enabled"):
            from app.barometric import apply_barometric_correction, make_pressure_provider
            events = apply_barometric_correction(events, make_pressure_provider())

    p = params or m
    window = compute_next_window_with_wind(
        events, p["draught_m"], p["drying_height_m"], p["safety_margin_m"],
        hw_timestamp, wind_offset, source="ukho",
    )
    if window is None:
        return None

    tender_windows = None
    tender_depth = None
    if m.get("tender_access_enabled"):
        tender_depth = float(m.get("tender_min_depth_m") or 0.3)
        tw = compute_next_window_with_wind(
            events, 0.0, m["drying_height_m"], tender_depth,
            hw_timestamp, wind_offset, source="ukho",
        )
        if tw is not None:
            tender_windows = [tw]
    return window, tender_windows, tender_depth


def _recompute_usable(w: dict) -> bool:
    """
    True when a recomputed wind window can be acted on: a below-threshold
    result (the stored window is then removed, whatever the wind) or a window
    with real start and end. An incomplete-data result is not usable; note
    that compute_next_window_with_wind can turn an incomplete baseline into a
    start==end marker that still carries incomplete_data, which this rejects.
    """
    if w.get("below_threshold"):
        return True
    if w.get("incomplete_data"):
        return False
    return bool(w.get("start_time") and w.get("end_time"))


def _same_tide(check_hw: str, window_hw: str, max_hours: float = 3.0) -> bool:
    """
    True when a recorded wind check belongs to the window's high water: both
    high waters within ``max_hours``. Cycle UIDs are stable for shifts of up
    to about 3 hours, so a revised HW time still matches, while two
    consecutive tides that happen to share a UID (from 2029, as high waters
    drift across the cycle grid) do not.
    """
    if not check_hw or not window_hw:
        return False
    try:
        a = dtparse.parse(check_hw)
        b = dtparse.parse(window_hw)
    except (ValueError, OverflowError, TypeError):
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return abs((a - b).total_seconds()) <= max_hours * 3600


def _stored_params_match(row: dict, vessel: dict) -> bool:
    """True when a stored row was computed with these vessel parameters."""
    for key in ("draught_m", "drying_height_m", "safety_margin_m"):
        a, b = row.get(key), vessel.get(key)
        if a is None or b is None:
            return False
        try:
            if abs(float(a) - float(b)) > 1e-6:
                return False
        except (TypeError, ValueError):
            return False
    return True


def reapply_stored_wind(m: dict, windows: list[dict], tender_windows,
                        stored_rows: list[dict], events: list[dict] = None,
                        params: dict = None):
    """
    Carry stored wind adjustments over a recompute of baseline windows
    (v2.11.1).

    For each fresh window whose tide (matched by cycle UID, never by raw HW
    timestamp) has a recorded wind check, the window is recomputed with that
    check's wind reading via compute_wind_window, so the adjustment is kept
    while fresh tide data and the barometric correction still land.

    Wind checks come from the wind_checks table, which keeps them even after
    the tide's window row is removed. A stored row with ``wind_adjusted = 1``
    and a wind direction but no recorded check (written before the table
    existed) is used as a fallback. A check is used only when its high water
    is within 3 hours of the window's (see _same_tide).

    Rules:

    - Baseline below threshold, or incomplete (no start/end at the edge of
      the tide data): passed through unchanged, with no recompute. Store then
      removes the row (below threshold: no access whatever the wind) or
      leaves it as is (incomplete). The wind check stays recorded, so a later
      recompute that finds the tide clearing again re-applies it.
    - Offset: re-derived from the recorded wind direction and the CURRENT
      config: the current shallow_extra_depth_m when should_apply_offset(
      recorded direction, current shallow_direction), else 0. So a
      calibration or settings change takes effect on the re-applied window.
    - Wind offset no longer configured on the mooring: not re-applied. The
      baseline window is written and the wind flags clear ("reverted").
    - Recompute fails although the baseline was usable (HW not found in the
      events, incomplete wind window, or an exception): the cycle is dropped
      from the write. The stored row is kept only when it was computed with
      the vessel parameters now in use; otherwise it is deleted, because a
      stale window is worse than none. Counted "failed" once (and "deleted"
      when removed).

    stored_rows: the mooring's stored calendar rows (for the parameter check
    and the fallback above). events: passed to compute_wind_window; see
    there. params: vessel parameter override (Update Feed); defaults to the
    mooring's own.

    Each cycle is handled in its own try/except, so one bad cycle cannot
    abort the refresh. Returns (windows, tender_windows, stats) with stats
    {preserved, reverted, failed, deleted}.
    """
    from app.wind import should_apply_offset
    from app.access_calc import generate_event_uid
    from app.database import delete_calendar_event, get_wind_checks

    stats = {"preserved": 0, "reverted": 0, "failed": 0, "deleted": 0}
    mooring_id = m["mooring_id"]
    if not windows:
        return windows, tender_windows, stats

    rows_by_uid = {r["event_uid"]: r for r in stored_rows or []}
    # Wind reading per tide: recorded checks first, then pre-v2.11.1 rows.
    checks = {}
    for r in rows_by_uid.values():
        if r.get("wind_adjusted") and r.get("wind_direction"):
            checks[r["event_uid"]] = {
                "hw_timestamp": r["hw_timestamp"],
                "wind_direction": r["wind_direction"],
                "wind_speed_ms": r.get("wind_speed_ms"),
            }
    for c in get_wind_checks(mooring_id):
        checks[c["event_uid"]] = c
    if not checks:
        return windows, tender_windows, stats

    shallow_dir = m.get("shallow_direction") or ""
    extra_depth = float(m.get("shallow_extra_depth_m") or 0.0)
    wind_configured = (
        bool(m.get("wind_offset_enabled")) and bool(shallow_dir) and extra_depth > 0
    )
    vessel = params or {
        "draught_m": m["draught_m"],
        "drying_height_m": m["drying_height_m"],
        "safety_margin_m": m["safety_margin_m"],
    }

    tender_by_hw = {
        tw["hw_timestamp"]: tw for tw in (tender_windows or []) if tw.get("hw_timestamp")
    }
    tender_touched = False
    out = []
    for w in windows:
        hw = w.get("hw_timestamp")
        uid = generate_event_uid(mooring_id, hw) if hw else None
        check = checks.get(uid)
        if check is not None and not _same_tide(check.get("hw_timestamp"), hw):
            # Same cycle UID but a different high water (UIDs can collide once
            # high waters drift across the cycle grid): not this tide's check.
            check = None
        if check is None:
            out.append(w)
            continue
        if w.get("below_threshold") or not _recompute_usable(w):
            # Nothing for the wind to adjust: store removes (below threshold)
            # or leaves (incomplete) the row. The check stays recorded.
            out.append(w)
            continue
        if not wind_configured:
            if (rows_by_uid.get(uid) or {}).get("wind_adjusted"):
                stats["reverted"] += 1
            out.append(w)
            continue

        offset = 0.0
        result = None
        reason = None
        try:
            if should_apply_offset(check["wind_direction"], shallow_dir):
                offset = extra_depth
            result = compute_wind_window(m, hw, offset, events=events, params=params)
            if result is None or not _recompute_usable(result[0]):
                reason = "no usable wind window"
        except Exception as e:
            reason = f"error: {e}"

        if reason is not None:
            # Same handling whatever the cause: keep the stored row only if it
            # was computed with the vessel parameters now in use.
            stats["failed"] += 1
            row = rows_by_uid.get(uid)
            keep = row is not None and _stored_params_match(row, vessel)
            outcome = ("window not rewritten (vessel parameters unchanged)" if keep
                       else "no stored window to keep" if row is None
                       else "stored window removed (vessel parameters changed)")
            if row is not None and not keep:
                try:
                    stats["deleted"] += delete_calendar_event(uid, mooring_id)
                except Exception as e:
                    outcome = f"stale stored window could not be removed ({e})"
            logger.warning(
                f"Mooring {mooring_id}: could not re-apply stored wind to HW {hw} "
                f"({reason}); {outcome}"
            )
            continue

        new_w, new_tender, _depth = result
        new_w = dict(new_w)
        new_w["wind_details"] = {
            "direction": check["wind_direction"],
            "speed_ms": check.get("wind_speed_ms"),
            "offset_m": offset,
        }
        out.append(new_w)
        if not new_w.get("below_threshold"):
            stats["preserved"] += 1
        if m.get("tender_access_enabled"):
            tender_touched = True
            if new_tender:
                tender_by_hw[hw] = new_tender[0]
            else:
                tender_by_hw.pop(hw, None)

    if tender_touched or tender_windows is not None:
        tender_windows = list(tender_by_hw.values())
    return out, tender_windows, stats


async def wind_observation_job(mooring_id: int, next_hw_timestamp: str):
    """
    Fired at a mooring's worst-case grounding: read the wind now and adjust the
    START of that mooring's NEXT access window (vessel and tender), leaving the
    ebb-side grounding at baseline.

    Touches only the one next-HW event (by cycle UID) so it cannot clobber a
    sibling window already adjusted by an earlier grounding's job: it writes
    that event, or removes it if the tide no longer clears the threshold.

    Since v2.11.1 the wind reading is also recorded in wind_checks before the
    window is computed, so later recomputes of that tide can re-apply it (see
    reapply_stored_wind), even if the window row is removed in between.
    """
    from app.wind import should_apply_offset
    from app.database import get_mooring, calibrate_drying_height, log_activity
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

    # Re-read the mooring: a Save during the OWM await above may have changed
    # its config, and the window must be computed with the config now stored.
    m = get_mooring(mooring_id)
    if not m or not m.get("wind_offset_enabled"):
        return
    shallow_dir = m.get("shallow_direction") or ""
    extra_depth = float(m.get("shallow_extra_depth_m") or 0.0)
    if not shallow_dir or extra_depth <= 0:
        return

    offset_triggered = should_apply_offset(wind["direction_compass"], shallow_dir)
    wind_offset = extra_depth if offset_triggered else 0.0
    wind_kts = (wind.get("speed_ms") or 0.0) * 1.944

    # Record the reading for this tide before computing anything, so later
    # recomputes can re-apply it whatever happens to the window row.
    from app.access_calc import generate_event_uid
    from app.database import record_wind_check
    try:
        record_wind_check(
            mooring_id, generate_event_uid(mooring_id, next_hw_timestamp),
            next_hw_timestamp, wind["direction_compass"], wind.get("speed_ms"),
        )
    except Exception as e:
        logger.error(f"Could not record wind check for mooring {mooring_id}: {e}")

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

    # Compute this one window from the tide data around the next HW, with the
    # barometric correction for an opted-in mooring (otherwise the wind job
    # would overwrite the daily job's pressure-corrected window with an
    # uncorrected one). No deadband here: the wind job is a deliberate,
    # reactive single-window write that should always land.
    try:
        computed = compute_wind_window(m, next_hw_timestamp, wind_offset)
        if computed is None:
            logger.info(
                f"Tide data does not cover next HW {next_hw_timestamp}; "
                f"skipping wind adjustment"
            )
            return
        window, tender_windows, tender_depth = computed

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
