"""
Tidal Access Window Predictor — FastAPI application.

Single-page web app with API routes for configuration, calculation,
data management, and iCal feed serving.
"""

import logging
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from dateutil import parser as dtparse

from app.config import (
    ensure_dirs, load_model_config, save_model_config,
    FEEDS_DIR, DEFAULT_TIMEZONE, UKHO_STATION_ID, OWM_API_KEY,
    to_utc_str,
)
from app.database import (
    init_db, save_mooring, get_mooring, get_all_moorings,
    add_observation, get_observations, delete_observation, clear_observations,
    store_tide_events, get_tide_events, calibrate_drying_height,
    delete_future_events, get_calendar_events, log_activity, get_activity_log,
)
from app.ukho import fetch_tidal_events
from app.khm_parser import parse_khm_paste
from app.harmonic import predict_events as harmonic_predict_events
from app.secondary_port import apply_offset
from app.wind import fetch_current_wind, should_apply_offset
from app.access_calc import compute_access_windows, generate_event_uid
from app.ical_manager import (
    generate_export_ics, store_windows_as_events,
    generate_feed_for_mooring,
)
from app.scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    ensure_dirs()
    init_db()
    load_model_config()  # Ensures default config is copied to data dir
    start_scheduler()
    logger.info("Tidal Access application started")
    yield
    shutdown_scheduler()
    logger.info("Tidal Access application stopped")


app = FastAPI(title="Tidal Access Window Predictor", lifespan=lifespan)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page web UI."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text())


# --- Mooring Configuration ---

@app.get("/api/moorings")
async def list_moorings():
    """List all configured moorings."""
    return get_all_moorings()


@app.get("/api/moorings/{mooring_id}")
async def get_mooring_config(mooring_id: int):
    """Get a specific mooring configuration."""
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")
    return m


@app.post("/api/moorings")
async def save_mooring_config(request: Request):
    """Save (create or update) a mooring configuration."""
    data = await request.json()
    if "mooring_id" not in data:
        raise HTTPException(400, "mooring_id is required")
    mid = int(data["mooring_id"])
    if mid < 1 or mid > 100:
        raise HTTPException(400, "mooring_id must be between 1 and 100")
    result = save_mooring(data)

    # Record configuration change to activity log
    log_activity(
        event_type="mooring_config",
        message=(
            f"Configuration saved for mooring #{mid}"
            + (f" ({result.get('boat_name')})" if result.get("boat_name") else "")
        ),
        severity="info",
        scope="mooring",
        mooring_id=mid,
        details={
            "boat_name": result.get("boat_name", ""),
            "draught_m": result.get("draught_m"),
            "drying_height_m": result.get("drying_height_m"),
            "safety_margin_m": result.get("safety_margin_m"),
            "calendar_enabled": bool(result.get("calendar_enabled")),
            "wind_offset_enabled": bool(result.get("wind_offset_enabled")),
            "shallow_direction": result.get("shallow_direction", ""),
            "shallow_extra_depth_m": result.get("shallow_extra_depth_m", 0),
            "use_observations": bool(result.get("use_observations")),
        },
    )

    # If calendar is enabled and events exist, regenerate the feed.
    # Don't generate an empty feed on first config save — let the
    # feed be generated when events are actually added.
    if result.get("calendar_enabled"):
        existing_events = get_calendar_events(mid)
        if existing_events:
            cal = calibrate_drying_height(mid)
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(mid, result.get("boat_name", ""), cal)

    return result


# --- Observations ---

@app.get("/api/moorings/{mooring_id}/observations")
async def list_observations(mooring_id: int):
    """Get all observations for a mooring."""
    return get_observations(mooring_id)


@app.delete("/api/moorings/{mooring_id}/observations/{observation_id}")
async def remove_observation(mooring_id: int, observation_id: int):
    """Delete a single observation. Triggers recalibration."""
    if not delete_observation(observation_id, mooring_id):
        raise HTTPException(404, "Observation not found")

    log_activity(
        event_type="observation_deleted",
        message=f"Observation #{observation_id} removed",
        severity="info",
        scope="mooring",
        mooring_id=mooring_id,
    )

    m = get_mooring(mooring_id)
    cal = calibrate_drying_height(mooring_id)
    if m and cal["best_estimate"] is not None:
        m["drying_height_m"] = cal["best_estimate"]
        save_mooring(m)

    return {"deleted": observation_id, "calibration": cal}


@app.delete("/api/moorings/{mooring_id}/observations")
async def clear_all_observations(mooring_id: int):
    """Delete all observations for a mooring. Resets calibration to manual estimate."""
    count = clear_observations(mooring_id)
    if count > 0:
        log_activity(
            event_type="observations_cleared",
            message=f"All {count} observations cleared",
            severity="warning",
            scope="mooring",
            mooring_id=mooring_id,
            details={"count": count},
        )
    return {"deleted_count": count, "calibration": calibrate_drying_height(mooring_id)}


@app.post("/api/moorings/{mooring_id}/observations")
async def add_mooring_observation(mooring_id: int, request: Request):
    """
    Add an observation and recalibrate drying height.
    Recalculates all future events if calibration changes.
    """
    data = await request.json()
    data["mooring_id"] = mooring_id

    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    obs = add_observation(data)

    log_activity(
        event_type="observation_added",
        message=f"Observation added: {obs.get('state')} at {obs.get('timestamp', '')[:16]}",
        severity="info",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "state": obs.get("state"),
            "timestamp": obs.get("timestamp"),
            "wind_direction": obs.get("wind_direction", ""),
            "direction_of_lay": obs.get("direction_of_lay", ""),
            "source": "manual",
        },
    )

    # Attempt recalibration using only this mooring's observations
    cal = calibrate_drying_height(mooring_id)
    recalibrated = False
    if (cal["best_estimate"] is not None and
            abs(cal["best_estimate"] - m["drying_height_m"]) > 0.01):
        previous_drying = m["drying_height_m"]
        m["drying_height_m"] = cal["best_estimate"]
        save_mooring(m)
        recalibrated = True

        log_activity(
            event_type="calibration_update",
            message=(
                f"Drying height recalibrated: {previous_drying:.2f}m → "
                f"{cal['best_estimate']:.2f}m ({cal['confidence']} confidence)"
            ),
            severity="success",
            scope="mooring",
            mooring_id=mooring_id,
            details={
                "previous_drying_height_m": previous_drying,
                "new_drying_height_m": cal["best_estimate"],
                "confidence": cal["confidence"],
                "lower_bound": cal.get("lower_bound"),
                "upper_bound": cal.get("upper_bound"),
                "afloat_count": cal.get("afloat_count"),
                "aground_count": cal.get("aground_count"),
            },
        )

        # Recalculate future events
        now = to_utc_str(datetime.now(timezone.utc))
        delete_future_events(mooring_id, now)

        query_start = to_utc_str(datetime.now(timezone.utc) - timedelta(hours=13))
        end = to_utc_str(datetime.now(timezone.utc) + timedelta(days=7))
        tide_data = get_tide_events(query_start, end, source="ukho")
        if tide_data:
            windows = compute_access_windows(
                events=tide_data,
                draught_m=m["draught_m"],
                drying_height_m=m["drying_height_m"],
                safety_margin_m=m["safety_margin_m"],
                source="ukho",
            )
            obs_calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            store_windows_as_events(windows, mooring_id, "ukho", m.get("boat_name", ""),
                                    calc_params=obs_calc_params)

        if m.get("calendar_enabled"):
            generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), cal)

    return {
        "observation": obs,
        "recalibrated": recalibrated,
        "calibration": cal,
    }


@app.post("/api/moorings/{mooring_id}/observations/upload")
async def upload_observations_xlsx(mooring_id: int, request: Request):
    """
    Import observations from an XLSX file. Observations are tied to this mooring only.
    Expected columns: Date, Time, State, Wind Direction, Direction of Lay, Notes
    """
    import io
    from openpyxl import load_workbook

    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    body = await request.body()
    if not body:
        raise HTTPException(400, "No file uploaded")

    try:
        wb = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(400, f"Could not read XLSX file: {e}")

    rows = list(ws.iter_rows(min_row=2, values_only=True))  # skip header
    imported = 0
    errors = 0

    for row in rows:
        if not row or len(row) < 3:
            errors += 1
            continue

        date_val, time_val, state_val = row[0], row[1], row[2]
        wind_dir = str(row[3]).strip() if len(row) > 3 and row[3] else ""
        lay_dir = str(row[4]).strip() if len(row) > 4 and row[4] else ""
        notes = str(row[5]).strip() if len(row) > 5 and row[5] else ""

        # Parse date and time
        try:
            if isinstance(date_val, datetime):
                dt = date_val
            elif isinstance(date_val, str):
                dt = dtparse.parse(date_val)
            else:
                errors += 1
                continue

            if time_val is not None and not isinstance(date_val, datetime):
                time_str = str(time_val).strip()
                if ":" in time_str:
                    parts = time_str.split(":")
                    dt = dt.replace(hour=int(parts[0]), minute=int(parts[1]))
        except (ValueError, TypeError):
            errors += 1
            continue

        # Parse state
        state_str = str(state_val).strip().lower()
        if state_str in ("afloat", "yes", "y", "true"):
            state = "afloat"
        elif state_str in ("aground", "no", "n", "false"):
            state = "aground"
        else:
            errors += 1
            continue

        # Store with mooring_id enforced
        # XLSX template tells users to enter local time (BST during sailing season).
        # If no timezone info, assume Europe/London and convert to UTC.
        if dt.tzinfo is None:
            import pytz
            local_tz = pytz.timezone("Europe/London")
            dt = local_tz.localize(dt)
        ts = to_utc_str(dt)
        add_observation({
            "mooring_id": mooring_id,
            "timestamp": ts,
            "state": state,
            "wind_direction": wind_dir,
            "direction_of_lay": lay_dir,
            "notes": notes,
        })
        imported += 1

    # Recalibrate after batch import
    cal = calibrate_drying_height(mooring_id)
    recalibrated = False
    if (cal["best_estimate"] is not None and
            abs(cal["best_estimate"] - m["drying_height_m"]) > 0.01):
        m["drying_height_m"] = cal["best_estimate"]
        save_mooring(m)
        recalibrated = True

        now = to_utc_str(datetime.now(timezone.utc))
        delete_future_events(mooring_id, now)
        query_start = to_utc_str(datetime.now(timezone.utc) - timedelta(hours=13))
        end = to_utc_str(datetime.now(timezone.utc) + timedelta(days=7))
        tide_data = get_tide_events(query_start, end, source="ukho")
        if tide_data:
            windows = compute_access_windows(
                events=tide_data,
                draught_m=m["draught_m"],
                drying_height_m=m["drying_height_m"],
                safety_margin_m=m["safety_margin_m"],
                source="ukho",
            )
            upload_calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            store_windows_as_events(windows, mooring_id, "ukho", m.get("boat_name", ""),
                                    calc_params=upload_calc_params)

        if m.get("calendar_enabled"):
            generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), cal)

    return {
        "imported": imported,
        "errors": errors,
        "recalibrated": recalibrated,
        "calibration": cal,
    }


@app.get("/api/observations/template")
async def download_observation_template():
    """Generate and serve an XLSX template for observation recording."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Observations"

    headers = ["Date", "Time", "State", "Wind Direction", "Direction of Lay", "Notes"]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1B2A4A", end_color="1B2A4A", fill_type="solid")

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    # Example row
    ws.cell(row=2, column=1, value="15/04/2026")
    ws.cell(row=2, column=2, value="10:30")
    ws.cell(row=2, column=3, value="afloat")
    ws.cell(row=2, column=4, value="SW")
    ws.cell(row=2, column=5, value="NE")
    ws.cell(row=2, column=6, value="Spring tide, good visibility")

    # Column widths
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 30

    # Add validation note
    ws2 = wb.create_sheet("Notes")
    ws2.cell(row=1, column=1, value="State: 'afloat' or 'aground'")
    ws2.cell(row=2, column=1, value="Wind Direction: N, NE, E, SE, S, SW, W, NW (optional)")
    ws2.cell(row=3, column=1, value="Direction of Lay: bow heading N, NE, E, SE, S, SW, W, NW (optional)")
    ws2.cell(row=4, column=1, value="Date format: DD/MM/YYYY")
    ws2.cell(row=5, column=1, value="Time format: HH:MM (local time)")
    ws2.cell(row=6, column=1, value="Times are assumed to be BST during sailing season")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=observation_template.xlsx"},
    )


@app.get("/api/moorings/{mooring_id}/calibration")
async def get_calibration_status(mooring_id: int):
    """Get current calibration status for a mooring."""
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")
    return calibrate_drying_height(mooring_id)


# --- UKHO Data ---

@app.post("/api/fetch-ukho")
async def trigger_ukho_fetch():
    """Manually trigger UKHO data fetch."""
    events = await fetch_tidal_events()
    if not events:
        log_activity(
            event_type="ukho_fetch",
            message="Manual UKHO fetch failed",
            severity="error",
        )
        raise HTTPException(502, "Failed to fetch UKHO data")

    store_tide_events(events, source="ukho", station="langstone")
    log_activity(
        event_type="ukho_fetch",
        message=f"Manual UKHO fetch: {len(events)} events stored",
        severity="success",
        details={"event_count": len(events), "trigger": "manual"},
    )

    # Also schedule wind observation jobs for the newly-fetched HW events
    from app.scheduler import _schedule_wind_jobs
    await _schedule_wind_jobs(events)

    return {"fetched": len(events), "source": "ukho"}


# --- KHM Data ---

@app.post("/api/parse-khm")
async def parse_khm_data(request: Request):
    """Parse pasted KHM tide table text. Langstone correction applied within parser."""
    data = await request.json()
    text = data.get("text", "")
    year = data.get("year", datetime.now().year)
    is_bst = data.get("is_bst", True)

    if not text.strip():
        raise HTTPException(400, "No text provided")

    # KHM parser applies Portsmouth→Langstone correction internally
    events = parse_khm_paste(text, year, is_bst=is_bst)
    if not events:
        raise HTTPException(400, "Could not parse any tide events from the pasted text")

    # Store as KHM source (corrections already applied)
    store_tide_events(events, source="khm", station="langstone")

    log_activity(
        event_type="khm_parse",
        message=f"KHM data parsed: {len(events)} events stored",
        severity="success",
        details={"event_count": len(events), "year": year, "is_bst": is_bst},
    )

    return {"parsed": len(events), "source": "khm"}


# --- Access Window Calculation ---

@app.post("/api/calculate")
async def calculate_access_windows(request: Request):
    """
    Calculate access windows for given parameters and data source.

    Body:
        source: "ukho" | "khm" | "harmonic"
        draught_m: float
        drying_height_m: float
        safety_margin_m: float
        mooring_id: int (optional - for persistent config)
        days: int (for harmonic, how far ahead)
        add_to_feed: bool (optional - add to mooring's iCal feed)
    """
    data = await request.json()
    source = data.get("source", "ukho")
    mooring_id = data.get("mooring_id")
    add_to_feed = data.get("add_to_feed", False)

    # Get mooring config if specified
    mooring = None
    if mooring_id:
        mooring = get_mooring(int(mooring_id))

    draught = data.get("draught_m") or (mooring["draught_m"] if mooring else 1.0)
    drying = data.get("drying_height_m") or (mooring["drying_height_m"] if mooring else 2.0)
    margin = data.get("safety_margin_m") or (mooring["safety_margin_m"] if mooring else 0.3)

    # Determine wind offset for next tide only
    wind_offset = 0.0
    wind_info = None
    wind_data = None
    if mooring and mooring.get("wind_offset_enabled") and source == "ukho":
        shallow_dir = mooring.get("shallow_direction", "")
        extra_depth = mooring.get("shallow_extra_depth_m", 0.0)
        if shallow_dir and extra_depth > 0:
            wind_data = await fetch_current_wind()
            if wind_data and should_apply_offset(wind_data["direction_compass"], shallow_dir):
                wind_offset = extra_depth
                wind_info = {
                    "applied": True,
                    "direction": wind_data["direction_compass"],
                    "shallow_side": shallow_dir,
                    "offset_m": extra_depth,
                }

    now = datetime.now(timezone.utc)
    # Query start: look back 13 hours to ensure we always have bracketing
    # events (preceding LW/HW) needed for tidal curve interpolation.
    # Without this, the first access window of the day has no preceding
    # event to interpolate the rising tide from, producing garbage results.
    query_start = now - timedelta(hours=13)

    if source == "ukho":
        end = now + timedelta(days=7)
        events = get_tide_events(to_utc_str(query_start), to_utc_str(end), source="ukho")
        if not events:
            # Try fetching fresh
            raw = await fetch_tidal_events()
            if raw:
                store_tide_events(raw, source="ukho", station="langstone")
                events = raw
        tide_source = "ukho"

    elif source == "khm":
        # Use stored KHM data only — no mixing with UKHO
        end = now + timedelta(days=60)
        events = get_tide_events(to_utc_str(query_start), to_utc_str(end), source="khm")
        tide_source = "khm"

    elif source == "harmonic":
        days = int(data.get("days", 30))
        end = now + timedelta(days=days)
        events = harmonic_predict_events(now, end)
        # Apply secondary port offset
        events = apply_offset(events)
        tide_source = "harmonic"

    else:
        raise HTTPException(400, f"Unknown source: {source}")

    if not events:
        return {"windows": [], "source": source, "event_count": 0, "message": "No tide data available"}

    windows = compute_access_windows(
        events=events,
        draught_m=float(draught),
        drying_height_m=float(drying),
        safety_margin_m=float(margin),
        wind_offset_m=wind_offset,
        source=tide_source,
    )

    # Filter out windows for HW events before 'now' — these were included
    # in the query only to provide interpolation context for the first real window.
    now_str = to_utc_str(now)
    windows = [w for w in windows if w["hw_timestamp"] >= now_str]

    # Mark wind adjustment
    if wind_offset > 0:
        for w in windows:
            w["wind_adjusted"] = True

    # Auto-update feed when mooring has calendar enabled, or when explicitly requested
    should_store = mooring_id and mooring and (
        add_to_feed or mooring.get("calendar_enabled")
    )
    if should_store:
        # Build metadata for event descriptions
        cal = calibrate_drying_height(int(mooring_id))
        calc_params = {
            "draught_m": float(draught),
            "drying_height_m": float(drying),
            "safety_margin_m": float(margin),
            "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
        }
        wind_details = None
        if wind_info and wind_info.get("applied"):
            wind_details = {
                "direction": wind_info["direction"],
                "speed_ms": wind_data.get("speed_ms") if wind_data else None,
                "offset_m": wind_info["offset_m"],
            }

        store_windows_as_events(
            windows, int(mooring_id), tide_source, mooring.get("boat_name", ""),
            calc_params=calc_params, wind_details=wind_details,
        )
        if mooring.get("calendar_enabled"):
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(int(mooring_id), mooring.get("boat_name", ""), cal)
            log_activity(
                event_type="feed_generation",
                message=f"Calendar feed updated ({len(windows)} windows from {tide_source.upper()})",
                severity="info",
                scope="mooring",
                mooring_id=int(mooring_id),
                details={
                    "window_count": len(windows),
                    "source": tide_source,
                    "trigger": "user_calculate",
                },
            )

    return {
        "windows": windows,
        "source": source,
        "parameters": {
            "draught_m": float(draught),
            "drying_height_m": float(drying),
            "safety_margin_m": float(margin),
            "wind_offset_m": wind_offset,
        },
        "wind_info": wind_info,
        "event_count": len([w for w in windows if not w.get("below_threshold")]),
        "feed_updated": bool(should_store and mooring.get("calendar_enabled")),
    }


# --- ICS Export ---

@app.post("/api/export-ics")
async def export_ics(request: Request):
    """Generate a downloadable .ics file from the last calculated windows."""
    data = await request.json()
    windows = data.get("windows", [])
    source = data.get("source", "ukho")
    boat_name = data.get("boat_name", "")
    mooring_id = data.get("mooring_id", 0)

    # Include calibration and calc params if mooring has config
    cal = None
    calc_params = None
    if mooring_id:
        m = get_mooring(int(mooring_id))
        if m:
            cal = calibrate_drying_height(int(mooring_id))
            calc_params = {
                "draught_m": m["draught_m"],
                "drying_height_m": m["drying_height_m"],
                "safety_margin_m": m["safety_margin_m"],
                "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
            }
            if cal.get("confidence") == "none":
                cal = None

    ics_bytes = generate_export_ics(windows, source, boat_name, mooring_id, cal, calc_params)

    return Response(
        content=ics_bytes,
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=tidal_access.ics"},
    )


# --- Activity Log ---

@app.get("/api/activity-log")
async def get_activity(
    scope: str = None,
    mooring_id: int = None,
    event_type: str = None,
    severity: str = None,
    limit: int = 500,
):
    """Query the activity log. Filter by scope (system/mooring), mooring_id, event_type, severity."""
    return get_activity_log(
        scope=scope,
        mooring_id=mooring_id,
        event_type=event_type,
        severity=severity,
        limit=min(limit, 1000),
    )


# --- iCal Feed ---

@app.get("/feeds/mooring_{mooring_id:int}.ics")
async def serve_feed(mooring_id: int):
    """Serve the subscribable iCal feed for a mooring.
    Always regenerates from DB to ensure freshness."""
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    # Always regenerate from current DB state — the feed is small
    # and this ensures calendar apps never receive stale data.
    cal = calibrate_drying_height(mooring_id)
    if cal.get("confidence") == "none":
        cal = None
    feed_path = generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), cal)

    return Response(
        content=feed_path.read_bytes(),
        media_type="text/calendar",
        headers={
            "Content-Type": "text/calendar; charset=utf-8",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# --- Model Config ---

@app.get("/api/model-config")
async def get_model_config():
    """Get the current model configuration."""
    return load_model_config()


@app.post("/api/model-config")
async def update_model_config(request: Request):
    """Update the model configuration."""
    data = await request.json()
    save_model_config(data)
    return {"status": "saved"}


# --- Tide Data ---

@app.get("/api/tide-data")
async def get_stored_tide_data(start: str = None, end: str = None):
    """Get stored tide data within a date range."""
    if not start:
        start = to_utc_str(datetime.now(timezone.utc))
    if not end:
        end = to_utc_str(datetime.now(timezone.utc) + timedelta(days=7))
    return get_tide_events(start, end)


# --- Wind ---

@app.get("/api/wind/current")
async def get_current_wind():
    """Fetch current wind conditions."""
    data = await fetch_current_wind()
    if not data:
        raise HTTPException(502, "Could not fetch wind data")
    return data


# --- Calendar Events ---

@app.get("/api/moorings/{mooring_id}/events")
async def get_mooring_events(mooring_id: int, start: str = None, end: str = None):
    """Get calendar events for a mooring."""
    return get_calendar_events(mooring_id, start, end)


# --- Config Status ---

@app.get("/api/config/status")
async def config_status():
    """Return which optional features are available based on env config."""
    owm_configured = bool(OWM_API_KEY) and OWM_API_KEY not in (
        "", "your_openweathermap_api_key_here"
    )
    return {
        "owm_available": owm_configured,
    }
