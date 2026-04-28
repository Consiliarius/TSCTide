"""
Tidal Access Window Predictor - FastAPI application.

Single-page web app with API routes for configuration, calculation,
data management, and iCal feed serving.
"""

import logging
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from dateutil import parser as dtparse

from app.config import (
    ensure_dirs, load_model_config, save_model_config,
    FEEDS_DIR, DEFAULT_TIMEZONE, UKHO_STATION_ID, OWM_API_KEY,
    to_utc_str,
)
from app.database import (
    init_db, save_mooring, get_mooring, get_all_moorings, delete_mooring,
    add_observation, get_observations, delete_observation, clear_observations,
    store_tide_events, get_tide_events, get_ukho_tide_events,
    calibrate_drying_height, calibrate_wind_offset,
    load_classification_inputs,
    get_wind_observations_in_range,
    delete_future_events, get_calendar_events, log_activity, get_activity_log,
    get_mooring_pin_hash, set_mooring_pin_hash,
    check_pin_lockout, record_failed_pin_attempt, clear_failed_pin_attempts,
    get_harmonic_predictions,
)
from app.pin import (
    hash_pin, verify_pin, is_valid_pin_format,
    MAX_PIN_ATTEMPTS, PIN_ATTEMPT_WINDOW_MINUTES, PIN_LOCKOUT_MINUTES,
)
from app.observation_classifier import classify_observations
from app.ukho import fetch_tidal_events
from app.khm_parser import parse_khm_paste
from app.harmonic import predict_events as harmonic_predict_events
from app.secondary_port import apply_offset
from app.wind import fetch_current_wind, should_apply_offset
from app.access_calc import compute_access_windows, generate_event_uid, invalidate_model_config_cache
from app.ical_manager import (
    generate_export_ics, store_windows_as_events,
    generate_feed_for_mooring,
    generate_langstone_ukho_7d_feed, generate_langstone_harmonic_180d_feed,
)
from app.scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _coalesce(*values, default):
    """
    Return the first value in *values that is not None, otherwise default.

    Purpose: replaces truthiness-based `a or b` idiom where a legitimate
    zero/empty/False value must not be overridden by a fallback. For example,
    a drying_height_m of 0.0 or a negative value must survive unchanged.

    Usage: _coalesce(data.get("x"), mooring["x"] if mooring else None, default=1.0)
    """
    for v in values:
        if v is not None:
            return v
    return default


def _public_mooring(m):
    """
    Return a copy of a mooring dict with the sensitive pin_hash field
    stripped out. Use this when returning mooring data to API clients.
    A None input returns None so call sites can use this uniformly.
    """
    if m is None:
        return None
    return {k: v for k, v in m.items() if k != "pin_hash"}


def _verify_pin_from_request(
    mooring_id: int,
    request: Request,
    allow_unclaimed: bool = False,
    allow_nonexistent: bool = False,
) -> None:
    """
    Verify the X-Mooring-PIN header against the stored hash for the given
    mooring. Raises HTTPException on any failure path; returns None on
    success.

    Failure responses (as HTTPException with structured detail):
      - 429 if the mooring is currently locked out. Sends Retry-After.
      - 404 if the mooring does not exist and allow_nonexistent is False.
      - 403 with pin_required=claim if the mooring exists but has no PIN
        set and allow_unclaimed is False. The UI interprets this as a
        prompt to call POST /pin to claim.
      - 401 with pin_required=verify if the header is missing.
      - 401 with pin_required=verify and attempts_remaining on PIN
        mismatch. The failed-attempt counter is incremented.
      - 429 if the wrong-PIN attempt is the one that trips the lockout.

    Side effects:
      - On success: clears any stored failed-attempt counter.
      - On PIN mismatch: increments the failed-attempt counter and may
        start a lockout window per the policy in app.pin.

    The two allow_* flags exist to support POST /api/moorings:
      - allow_nonexistent=True lets the first-ever save on a fresh
        mooring_id pass through (there is no row yet to read a hash from).
      - allow_unclaimed=True lets a save on an existing-but-unclaimed
        mooring pass through (the UI will follow up with a claim call).
    All other callers use the strict defaults.
    """
    lockout = check_pin_lockout(mooring_id)
    if lockout:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many failed PIN attempts",
                "locked_until": lockout["locked_until"],
                "seconds_remaining": lockout["seconds_remaining"],
            },
            headers={"Retry-After": str(lockout["seconds_remaining"])},
        )

    stored_hash = get_mooring_pin_hash(mooring_id)
    if stored_hash is None:
        if allow_nonexistent:
            return
        raise HTTPException(404, "Mooring not found")

    if stored_hash == "":
        if allow_unclaimed:
            return
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Mooring has no PIN set",
                "pin_required": "claim",
            },
        )

    pin = request.headers.get("X-Mooring-PIN")
    if not pin:
        raise HTTPException(
            status_code=401,
            detail={
                "message": "PIN required",
                "pin_required": "verify",
            },
        )

    if verify_pin(pin, stored_hash):
        clear_failed_pin_attempts(mooring_id)
        return

    state = record_failed_pin_attempt(
        mooring_id,
        MAX_PIN_ATTEMPTS,
        PIN_ATTEMPT_WINDOW_MINUTES,
        PIN_LOCKOUT_MINUTES,
    )
    if state["locked_until"]:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many failed attempts; locked out",
                "locked_until": state["locked_until"],
                "attempts_remaining": 0,
            },
            headers={"Retry-After": str(PIN_LOCKOUT_MINUTES * 60)},
        )
    raise HTTPException(
        status_code=401,
        detail={
            "message": "Incorrect PIN",
            "pin_required": "verify",
            "attempts_remaining": state["attempts_remaining"],
        },
    )


async def require_mooring_pin(mooring_id: int, request: Request) -> None:
    """
    FastAPI dependency wrapping _verify_pin_from_request with the strict
    defaults: rejects non-existent moorings (404) and unclaimed moorings
    (403 with pin_required=claim). Use via
    `dependencies=[Depends(require_mooring_pin)]` on write endpoints
    where the mooring must already exist and already be claimed.
    """
    _verify_pin_from_request(
        mooring_id, request, allow_unclaimed=False, allow_nonexistent=False
    )


def _build_calibration_response(mooring_id: int) -> dict:
    """
    Build the combined calibration response: base drying fields at top
    level (for back-compat), wind_offset sub-object, and a classifications
    map keyed by observation id. Used by GET /calibration and as the
    response body for observation mutations.

    All three components (drying calibration, wind offset calibration,
    classifications map) share a single data-loading pass via
    load_classification_inputs, avoiding redundant DB queries and
    classifier passes.
    """
    preloaded = load_classification_inputs(mooring_id)
    base = calibrate_drying_height(mooring_id, _preloaded=preloaded)
    wind = calibrate_wind_offset(mooring_id, _preloaded=preloaded)

    # Build classifications map from the pre-loaded classifier output
    classifications_map = {}
    _mooring, _observations, _tide_events, _wind_obs, classified = preloaded
    for entry in classified:
        obs_id = entry["observation"].get("id")
        if obs_id is None:
            continue
        classifications_map[str(obs_id)] = {
            "classification": entry["classification"],
            "reason": entry["reason"],
            "hw_timestamp": entry["hw_timestamp"],
            "wind_compass": entry["wind_compass"],
        }

    response = dict(base)
    response["wind_offset"] = wind
    response["classifications"] = classifications_map
    return response


def _recompute_future_windows(mooring_id: int):
    """
    After a mooring configuration change, clear stored future events and
    recompute them from currently-stored UKHO tide data, then regenerate
    the iCal feed if calendar subscription is enabled.

    Does nothing if no UKHO data is available. Uses the mooring's current
    stored configuration. Uses get_ukho_tide_events() so that Portsmouth
    fallback data receives the secondary port correction transparently.
    """
    m = get_mooring(mooring_id)
    if not m:
        return

    now = datetime.now(timezone.utc)
    now_str = to_utc_str(now)
    delete_future_events(mooring_id, now_str)

    query_start = to_utc_str(now - timedelta(hours=13))
    end = to_utc_str(now + timedelta(days=7))
    tide_data = get_ukho_tide_events(query_start, end)
    if not tide_data:
        return

    windows = compute_access_windows(
        events=tide_data,
        draught_m=m["draught_m"],
        drying_height_m=m["drying_height_m"],
        safety_margin_m=m["safety_margin_m"],
        source="ukho",
    )
    # Filter out windows for HW events in the 13h lookback window — those
    # were included only for interpolation context, not as real future windows.
    windows = [w for w in windows if w["hw_timestamp"] >= now_str]

    cal = calibrate_drying_height(mooring_id)
    calc_params = {
        "draught_m": m["draught_m"],
        "drying_height_m": m["drying_height_m"],
        "safety_margin_m": m["safety_margin_m"],
        "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
    }
    store_windows_as_events(
        windows, mooring_id, "ukho", m.get("boat_name", ""),
        calc_params=calc_params,
    )

    if m.get("calendar_enabled"):
        feed_cal = cal if cal.get("confidence") != "none" else None
        generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), feed_cal)


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
    """List all configured moorings with sensitive fields (pin_hash) stripped."""
    return [_public_mooring(m) for m in get_all_moorings()]


@app.get("/api/moorings/{mooring_id}")
async def get_mooring_config(mooring_id: int):
    """Get a specific mooring configuration with pin_hash stripped."""
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")
    return _public_mooring(m)


@app.post("/api/moorings")
async def save_mooring_config(request: Request):
    """
    Save (create or update) a mooring configuration.

    PIN gating: Existing moorings with a non-empty pin_hash require a
    valid X-Mooring-PIN header. New moorings (no row yet) and existing
    moorings with no PIN set (unclaimed) are allowed through; the UI
    should follow up with a call to POST /api/moorings/{id}/pin to
    claim the PIN.
    """
    data = await request.json()
    if "mooring_id" not in data:
        raise HTTPException(400, "mooring_id is required")
    mid = int(data["mooring_id"])
    if mid < 1 or mid > 100:
        raise HTTPException(400, "mooring_id must be between 1 and 100")

    # PIN gate for claimed moorings only. Brand-new mooring_ids and
    # unclaimed existing ones pass through so the UI can immediately
    # prompt to claim via POST /pin.
    _verify_pin_from_request(
        mid, request, allow_unclaimed=True, allow_nonexistent=True
    )

    result = save_mooring(data)

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

    if result.get("calendar_enabled"):
        existing_events = get_calendar_events(mid)
        if existing_events:
            cal = calibrate_drying_height(mid)
            if cal.get("confidence") == "none":
                cal = None
            generate_feed_for_mooring(mid, result.get("boat_name", ""), cal)

    return _public_mooring(result)


@app.post("/api/moorings/{mooring_id}/pin")
async def set_or_change_pin(mooring_id: int, request: Request):
    """
    Claim or change the PIN for a mooring.

    For unclaimed moorings (no PIN set): only new_pin is required.
    For claimed moorings: both current_pin (for verification) and new_pin
    are required. The same rate-limit policy applies as for other PIN
    operations - a wrong current_pin counts toward the lockout.

    Body: {"new_pin": "123456", "current_pin": "654321" (optional)}
    Returns: {"status": "ok", "was_claimed": bool}
    """
    data = await request.json()
    new_pin = data.get("new_pin", "") or ""
    current_pin = data.get("current_pin", "") or ""

    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    if not is_valid_pin_format(new_pin):
        raise HTTPException(400, "PIN must be exactly six numeric digits")

    # Respect the lockout timer for any PIN operation.
    lockout = check_pin_lockout(mooring_id)
    if lockout:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many failed PIN attempts",
                "locked_until": lockout["locked_until"],
                "seconds_remaining": lockout["seconds_remaining"],
            },
            headers={"Retry-After": str(lockout["seconds_remaining"])},
        )

    stored_hash = get_mooring_pin_hash(mooring_id) or ""
    was_claimed = bool(stored_hash)

    if was_claimed:
        if not current_pin:
            raise HTTPException(
                status_code=401,
                detail={
                    "message": "current_pin is required to change an existing PIN",
                    "pin_required": "verify",
                },
            )
        if not verify_pin(current_pin, stored_hash):
            state = record_failed_pin_attempt(
                mooring_id,
                MAX_PIN_ATTEMPTS,
                PIN_ATTEMPT_WINDOW_MINUTES,
                PIN_LOCKOUT_MINUTES,
            )
            if state["locked_until"]:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "message": "Too many failed attempts; locked out",
                        "locked_until": state["locked_until"],
                        "attempts_remaining": 0,
                    },
                    headers={"Retry-After": str(PIN_LOCKOUT_MINUTES * 60)},
                )
            raise HTTPException(
                status_code=401,
                detail={
                    "message": "Incorrect current PIN",
                    "pin_required": "verify",
                    "attempts_remaining": state["attempts_remaining"],
                },
            )
        clear_failed_pin_attempts(mooring_id)

    new_hash = hash_pin(new_pin)
    if not set_mooring_pin_hash(mooring_id, new_hash):
        # Should not happen - we just confirmed the row exists above.
        raise HTTPException(500, "Failed to update PIN")

    log_activity(
        event_type="pin_changed" if was_claimed else "pin_claimed",
        message=(
            f"PIN changed for mooring #{mooring_id}"
            if was_claimed
            else f"PIN claimed for mooring #{mooring_id}"
        ),
        severity="success",
        scope="mooring",
        mooring_id=mooring_id,
    )

    return {"status": "ok", "was_claimed": was_claimed}


@app.delete("/api/moorings/{mooring_id}",
            dependencies=[Depends(require_mooring_pin)])
async def delete_mooring_config(mooring_id: int):
    """
    Permanently delete a mooring and its associated per-mooring data.
    PIN-gated. Returns 404 if the mooring does not exist (the dependency
    handles this before the handler runs).

    Database state cleared: moorings row, observations, calendar_events,
    pin_failed_attempts. Activity log entries are preserved as an audit
    trail (and age out automatically per prune_activity_log retention).

    On-disk state cleared: feeds/mooring_NN.ics if present.

    Returns a summary dict with deletion counts.
    """
    m = get_mooring(mooring_id)
    boat_name = m.get("boat_name") if m else ""

    counts = delete_mooring(mooring_id)

    # Remove the on-disk feed file. Filename pattern matches
    # generate_feed_for_mooring (zero-padded to 3 digits, hence :03d).
    # If the file does not exist, this is a no-op - users may have
    # never enabled calendar subscription.
    feed_path = FEEDS_DIR / f"mooring_{mooring_id:03d}.ics"
    feed_removed = False
    try:
        if feed_path.exists():
            feed_path.unlink()
            feed_removed = True
    except OSError as e:
        # Log but do not fail the deletion - the database row is gone, the
        # feed file is now orphaned, and serve_feed will 404 since the
        # mooring no longer exists. Manual cleanup of the file is possible
        # if needed.
        logger.warning(
            f"Failed to remove feed file {feed_path} during mooring delete: {e}"
        )

    log_activity(
        event_type="mooring_deleted",
        message=(
            f"Mooring #{mooring_id}"
            + (f" ({boat_name})" if boat_name else "")
            + " permanently deleted"
        ),
        severity="warning",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "boat_name": boat_name,
            "db_counts": counts,
            "feed_file_removed": feed_removed,
        },
    )

    return {
        "deleted": True,
        "mooring_id": mooring_id,
        "db_counts": counts,
        "feed_file_removed": feed_removed,
    }


# --- Observations ---

@app.get("/api/moorings/{mooring_id}/observations")
async def list_observations(mooring_id: int):
    """Get all observations for a mooring."""
    return get_observations(mooring_id)


@app.delete("/api/moorings/{mooring_id}/observations/{observation_id}",
            dependencies=[Depends(require_mooring_pin)])
async def remove_observation(mooring_id: int, observation_id: int):
    """
    Delete a single observation. Does NOT auto-apply calibration changes -
    the updated calibration is returned for the UI to display as a
    suggestion. The user applies it explicitly via the Apply endpoints.
    """
    if not delete_observation(observation_id, mooring_id):
        raise HTTPException(404, "Observation not found")

    log_activity(
        event_type="observation_deleted",
        message=f"Observation #{observation_id} removed",
        severity="info",
        scope="mooring",
        mooring_id=mooring_id,
    )

    return {
        "deleted": observation_id,
        "calibration": _build_calibration_response(mooring_id),
    }


@app.delete("/api/moorings/{mooring_id}/observations",
            dependencies=[Depends(require_mooring_pin)])
async def clear_all_observations(mooring_id: int):
    """
    Delete all observations for a mooring. Does NOT auto-apply calibration
    changes - the mooring's stored drying height and shallow_extra_depth
    are left untouched; the UI can prompt the user to re-set them manually.
    """
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
    return {
        "deleted_count": count,
        "calibration": _build_calibration_response(mooring_id),
    }


@app.post("/api/moorings/{mooring_id}/observations",
          dependencies=[Depends(require_mooring_pin)])
async def add_mooring_observation(mooring_id: int, request: Request):
    """
    Add an observation. Does NOT auto-apply calibration changes; the
    returned calibration payload is a suggestion for the UI to render
    with Apply buttons.
    """
    data = await request.json()
    data["mooring_id"] = mooring_id

    # Existence check is handled by the require_mooring_pin dependency,
    # which returns 404 before this handler runs.
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

    return {
        "observation": obs,
        "calibration": _build_calibration_response(mooring_id),
    }


@app.post("/api/moorings/{mooring_id}/observations/upload",
          dependencies=[Depends(require_mooring_pin)])
async def upload_observations_xlsx(mooring_id: int, request: Request):
    """
    Import observations from an XLSX file. Observations are tied to this
    mooring only. Expected columns: Date, Time, State, Wind Direction,
    Direction of Lay, Notes.

    Does NOT auto-apply calibration - the returned calibration payload
    is a suggestion.
    """
    import io
    from openpyxl import load_workbook

    # Existence check is handled by the require_mooring_pin dependency.
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

        state_str = str(state_val).strip().lower()
        if state_str in ("afloat", "yes", "y", "true"):
            state = "afloat"
        elif state_str in ("aground", "no", "n", "false"):
            state = "aground"
        else:
            errors += 1
            continue

        if dt.tzinfo is None:
            import pytz
            local_tz = pytz.timezone(DEFAULT_TIMEZONE)
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

    if imported > 0:
        log_activity(
            event_type="observations_uploaded",
            message=f"Imported {imported} observations from XLSX ({errors} rows skipped)",
            severity="info",
            scope="mooring",
            mooring_id=mooring_id,
            details={"imported": imported, "errors": errors},
        )

    return {
        "imported": imported,
        "errors": errors,
        "calibration": _build_calibration_response(mooring_id),
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

    ws.cell(row=2, column=1, value="15/04/2026")
    ws.cell(row=2, column=2, value="10:30")
    ws.cell(row=2, column=3, value="afloat")
    ws.cell(row=2, column=4, value="SW")
    ws.cell(row=2, column=5, value="NE")
    ws.cell(row=2, column=6, value="Spring tide, good visibility")

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 30

    ws2 = wb.create_sheet("Notes")
    ws2.cell(row=1, column=1, value="State: 'afloat' or 'aground'")
    ws2.cell(row=2, column=1, value="Wind Direction: N, NE, E, SE, S, SW, W, NW (optional)")
    ws2.cell(row=3, column=1, value="Direction of Lay: bow heading N, NE, E, SE, S, SW, W, NW (optional)")
    ws2.cell(row=4, column=1, value="Date format: DD/MM/YYYY")
    ws2.cell(row=5, column=1, value="Time format: HH:MM (local time)")
    ws2.cell(row=6, column=1, value="Times are assumed to be local time (BST during sailing season)")

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
    """
    Get calibration status for a mooring.

    Response shape:
      - Base drying fields at the top level: best_estimate, lower_bound,
        upper_bound, confidence, matched, unmatched, afloat_count,
        aground_count, excluded_wind_offset_count.
      - `wind_offset`: suggested shallow-side offset with confidence and
        current stored values.
      - `classifications`: map from observation id (string) to its
        classifier output (classification, reason, hw_timestamp,
        wind_compass).
    """
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")
    return _build_calibration_response(mooring_id)


@app.post("/api/moorings/{mooring_id}/calibration/apply-drying-height",
          dependencies=[Depends(require_mooring_pin)])
async def apply_drying_height_calibration(mooring_id: int):
    """
    Apply the current base-drying-height suggestion to the mooring's
    stored configuration, then recompute future access windows and
    regenerate the feed.
    """
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    cal = calibrate_drying_height(mooring_id)
    best = cal.get("best_estimate")
    if best is None:
        raise HTTPException(400, "No drying height suggestion available to apply")

    previous = m["drying_height_m"]
    if abs(best - previous) < 0.01:
        return {
            "applied": False,
            "reason": "suggestion matches current stored value",
            "previous": previous,
            "new": previous,
            "mooring": _public_mooring(m),
            "calibration": _build_calibration_response(mooring_id),
        }

    m["drying_height_m"] = best
    save_mooring(m)

    log_activity(
        event_type="calibration_apply",
        message=(
            f"Drying height applied from observations: "
            f"{previous:.2f}m -> {best:.2f}m ({cal.get('confidence')})"
        ),
        severity="success",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "field": "drying_height_m",
            "previous": previous,
            "new": best,
            "confidence": cal.get("confidence"),
            "lower_bound": cal.get("lower_bound"),
            "upper_bound": cal.get("upper_bound"),
            "afloat_count": cal.get("afloat_count"),
            "aground_count": cal.get("aground_count"),
        },
    )

    _recompute_future_windows(mooring_id)

    return {
        "applied": True,
        "previous": previous,
        "new": best,
        "mooring": _public_mooring(get_mooring(mooring_id)),
        "calibration": _build_calibration_response(mooring_id),
    }


@app.post("/api/moorings/{mooring_id}/calibration/apply-wind-offset",
          dependencies=[Depends(require_mooring_pin)])
async def apply_wind_offset_calibration(mooring_id: int):
    """
    Apply the current shallow-side wind offset suggestion to the mooring's
    stored configuration (updates shallow_extra_depth_m), then recompute
    future access windows and regenerate the feed.
    """
    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    cal = calibrate_wind_offset(mooring_id)
    suggested = cal.get("suggested_offset_m")
    if suggested is None:
        raise HTTPException(400, "No wind offset suggestion available to apply")

    try:
        previous = float(m.get("shallow_extra_depth_m") or 0.0)
    except (TypeError, ValueError):
        previous = 0.0

    if suggested <= previous + 0.01:
        return {
            "applied": False,
            "reason": (
                "current stored offset already meets or exceeds the "
                "observation-derived lower bound; no increase required"
            ),
            "previous": previous,
            "new": previous,
            "mooring": _public_mooring(m),
            "calibration": _build_calibration_response(mooring_id),
        }

    m["shallow_extra_depth_m"] = suggested
    save_mooring(m)

    log_activity(
        event_type="calibration_apply",
        message=(
            f"Wind offset applied from observations: "
            f"+{previous:.2f}m -> +{suggested:.2f}m ({cal.get('confidence')}, "
            f"{cal.get('observation_count')} observations)"
        ),
        severity="success",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "field": "shallow_extra_depth_m",
            "previous": previous,
            "new": suggested,
            "confidence": cal.get("confidence"),
            "observation_count": cal.get("observation_count"),
            "baseline_drying_height_m": cal.get("current_drying_height_m"),
        },
    )

    _recompute_future_windows(mooring_id)

    return {
        "applied": True,
        "previous": previous,
        "new": suggested,
        "mooring": _public_mooring(get_mooring(mooring_id)),
        "calibration": _build_calibration_response(mooring_id),
    }


# --- UKHO Data ---

@app.post("/api/fetch-ukho")
async def trigger_ukho_fetch():
    """Manually trigger UKHO data fetch."""
    events, station_used = await fetch_tidal_events()
    if not events:
        log_activity(
            event_type="ukho_fetch",
            message="Manual UKHO fetch failed",
            severity="error",
        )
        raise HTTPException(502, "Failed to fetch UKHO data")

    station_label = "langstone" if station_used == UKHO_STATION_ID else "portsmouth"
    store_tide_events(events, source="ukho", station=station_label)

    fallback_note = f" (Portsmouth fallback, station {station_used})" if station_label == "portsmouth" else ""
    log_activity(
        event_type="ukho_fetch",
        message=f"Manual UKHO fetch: {len(events)} events stored{fallback_note}",
        severity="success" if station_label == "langstone" else "warning",
        details={
            "event_count": len(events),
            "trigger": "manual",
            "station_used": station_used,
            "station_label": station_label,
        },
    )

    from app.scheduler import _schedule_wind_jobs
    await _schedule_wind_jobs(events)

    # Regenerate the standalone Langstone tide feeds so manual refresh has
    # the same effect on those feeds as the daily 02:00 job. The harmonic
    # 180d feed regenerates from currently-stored harmonic predictions, so
    # it picks up the new UKHO data for days 0-7 even though the harmonic
    # set has not been recomputed by this manual trigger.
    try:
        generate_langstone_ukho_7d_feed()
        generate_langstone_harmonic_180d_feed()
    except Exception as e:
        logger.warning(f"Langstone feed regeneration after manual fetch failed: {e}")

    return {"fetched": len(events), "source": "ukho", "station": station_label}


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

    events = parse_khm_paste(text, year, is_bst=is_bst)
    if not events:
        raise HTTPException(400, "Could not parse any tide events from the pasted text")

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
    READ-ONLY: returns windows but does NOT store events or regenerate
    the feed. To push a calculation result into the mooring's stored
    events and iCal feed, call POST /api/moorings/{id}/feed/update
    afterwards (PIN-gated).

    Body:
        source: "ukho" | "khm" | "harmonic"
        draught_m: float
        drying_height_m: float
        safety_margin_m: float
        mooring_id: int (optional - loads stored params when fields omitted)
        days: int (for harmonic, how far ahead)
        wind_offset_enabled: bool (optional - overrides stored mooring setting)
        shallow_direction: str (optional - overrides stored mooring setting)
        shallow_extra_depth_m: float (optional - overrides stored mooring setting)
    """
    data = await request.json()
    source = data.get("source", "ukho")
    mooring_id = data.get("mooring_id")

    mooring = None
    if mooring_id:
        mooring = get_mooring(int(mooring_id))

    draught = _coalesce(
        data.get("draught_m"),
        mooring["draught_m"] if mooring else None,
        default=1.0,
    )
    drying = _coalesce(
        data.get("drying_height_m"),
        mooring["drying_height_m"] if mooring else None,
        default=2.0,
    )
    margin = _coalesce(
        data.get("safety_margin_m"),
        mooring["safety_margin_m"] if mooring else None,
        default=0.3,
    )

    wind_offset = 0.0
    wind_info = None
    wind_data = None
    if source == "ukho":
        # Wind offset is only considered when a mooring ID is present.
        # Without a mooring ID the user is doing an anonymous calculation
        # and there is no stored mooring config to define which side is
        # shallow. Even if the UI sends wind-offset fields in the body
        # (because the user previously loaded a mooring and then cleared
        # the ID), those values must be ignored.
        if not mooring_id:
            wind_enabled = False
            shallow_dir = ""
            extra_depth = 0.0
        else:
            posted_enabled = data.get("wind_offset_enabled")
            if posted_enabled is None:
                wind_enabled = bool(mooring.get("wind_offset_enabled")) if mooring else False
            else:
                wind_enabled = bool(posted_enabled)

            posted_dir = data.get("shallow_direction")
            if posted_dir is None:
                shallow_dir = (mooring.get("shallow_direction", "") if mooring else "")
            else:
                shallow_dir = str(posted_dir)

            posted_extra = data.get("shallow_extra_depth_m")
            if posted_extra is None:
                extra_depth = float(mooring.get("shallow_extra_depth_m", 0.0)) if mooring else 0.0
            else:
                try:
                    extra_depth = float(posted_extra)
                except (TypeError, ValueError):
                    extra_depth = 0.0

        if wind_enabled and shallow_dir and extra_depth > 0:
            wind_data = await fetch_current_wind()
            if wind_data and should_apply_offset(wind_data["direction_compass"], shallow_dir):
                wind_offset = extra_depth
                wind_info = {
                    "applied": True,
                    "direction": wind_data["direction_compass"],
                    "shallow_side": shallow_dir,
                    "offset_m": extra_depth,
                    # speed_ms round-trips to /feed/update so that calendar
                    # event descriptions generated later can include wind
                    # speed, matching v1 behaviour. The UI preserves this
                    # value verbatim between /calculate and /feed/update.
                    "speed_ms": wind_data.get("speed_ms"),
                }

    now = datetime.now(timezone.utc)
    # Look back 13 hours to ensure bracketing events for interpolation context.
    query_start = now - timedelta(hours=13)

    if source == "ukho":
        end = now + timedelta(days=7)
        # get_ukho_tide_events handles station preference and applies the
        # Portsmouth->Langstone offset transparently if only fallback data exists.
        events = get_ukho_tide_events(to_utc_str(query_start), to_utc_str(end))
        if not events:
            # Try fetching fresh from the API
            raw, station_used = await fetch_tidal_events()
            if raw:
                station_label = "langstone" if station_used == UKHO_STATION_ID else "portsmouth"
                store_tide_events(raw, source="ukho", station=station_label)
                # Re-query via get_ukho_tide_events so the 13h lookback window
                # is covered and the offset is applied if Portsmouth data was stored.
                events = get_ukho_tide_events(to_utc_str(query_start), to_utc_str(end))
        tide_source = "ukho"

    elif source == "khm":
        end = now + timedelta(days=60)
        events = get_tide_events(to_utc_str(query_start), to_utc_str(end), source="khm")
        tide_source = "khm"

    elif source == "harmonic":
        days = int(data.get("days", 30))
        end = now + timedelta(days=days)
        events = harmonic_predict_events(now, end)
        events = apply_offset(events)
        tide_source = "harmonic"

    else:
        raise HTTPException(400, f"Unknown source: {source}")

    if not events:
        return {"windows": [], "source": source, "event_count": 0, "message": "No tide data available"}

    next_hw_ts = None
    if wind_offset > 0:
        now_str = to_utc_str(now)
        for e in sorted(events, key=lambda ev: ev["timestamp"]):
            if e["event_type"] == "HighWater" and e["timestamp"] >= now_str:
                next_hw_ts = e["timestamp"]
                break

    windows = compute_access_windows(
        events=events,
        draught_m=float(draught),
        drying_height_m=float(drying),
        safety_margin_m=float(margin),
        wind_offset_m=wind_offset,
        wind_offset_hw_timestamp=next_hw_ts,
        source=tide_source,
    )

    now_str = to_utc_str(now)
    windows = [w for w in windows if w["hw_timestamp"] >= now_str]

    # v2: This endpoint is read-only. Storing events and regenerating the
    # iCal feed now happens via POST /api/moorings/{id}/feed/update, which
    # is PIN-gated. The UI passes this response body back to that endpoint
    # verbatim when the user clicks "Update Feed".

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


# --- Feed Update (v2: decoupled from calculate) ---

@app.post("/api/moorings/{mooring_id}/feed/update",
          dependencies=[Depends(require_mooring_pin)])
async def update_mooring_feed(mooring_id: int, request: Request):
    """
    PIN-gated. Push a set of previously-calculated windows into the
    mooring's stored events, and regenerate the .ics feed file if the
    mooring has calendar_enabled. Separated from POST /api/calculate so
    that calculation can be a read-only operation: anyone can calculate
    windows or export a one-shot .ics, but only the PIN-holder can
    overwrite what the subscribed calendar feed serves.

    If calendar_enabled is false on the mooring, events are still stored
    (so a later re-enable picks them up) but the .ics file is not
    regenerated. The UI should hide the "Update Feed" button in this
    case; a direct call that bypasses the UI returns a feed_generated
    flag of False so the caller knows.

    Body (matches the shape returned by /api/calculate):
        source: str (ukho | khm | harmonic)
        windows: [{...}, ...]
        parameters: {draught_m, drying_height_m, safety_margin_m, ...}
        wind_info: {direction, offset_m, shallow_side, applied} or null
    """
    data = await request.json()
    source = data.get("source", "")
    windows = data.get("windows", [])
    params = data.get("parameters", {}) or {}
    wind_info = data.get("wind_info") or None

    m = get_mooring(mooring_id)
    if not m:
        raise HTTPException(404, "Mooring not found")

    if not windows:
        raise HTTPException(400, "No windows supplied to update feed")
    if source not in ("ukho", "khm", "harmonic"):
        raise HTTPException(400, f"Invalid source: {source}")

    cal = calibrate_drying_height(mooring_id)
    calc_params = {
        "draught_m": params.get("draught_m", m["draught_m"]),
        "drying_height_m": params.get("drying_height_m", m["drying_height_m"]),
        "safety_margin_m": params.get("safety_margin_m", m["safety_margin_m"]),
        "obs_calibrated": 1 if cal.get("confidence", "none") != "none" else 0,
    }
    wind_details = None
    if wind_info and wind_info.get("applied"):
        wind_details = {
            "direction": wind_info.get("direction"),
            # Forwarded from /calculate so calendar event descriptions can
            # include wind speed. Only present if the originating /calculate
            # call actually fetched wind data.
            "speed_ms": wind_info.get("speed_ms"),
            "offset_m": wind_info.get("offset_m"),
        }

    # Always store events - they are internal state, used by serve_feed
    # on every request, and preserved across calendar_enabled toggles.
    store_windows_as_events(
        windows, mooring_id, source, m.get("boat_name", ""),
        calc_params=calc_params, wind_details=wind_details,
    )

    feed_generated = False
    if m.get("calendar_enabled"):
        feed_cal = cal if cal.get("confidence") != "none" else None
        generate_feed_for_mooring(mooring_id, m.get("boat_name", ""), feed_cal)
        feed_generated = True

    log_activity(
        event_type="feed_generation",
        message=(
            f"Calendar feed updated ({len(windows)} windows from {source.upper()})"
            if feed_generated
            else f"Events stored but feed file not regenerated - calendar disabled for mooring #{mooring_id}"
        ),
        severity="info" if feed_generated else "warning",
        scope="mooring",
        mooring_id=mooring_id,
        details={
            "window_count": len(windows),
            "source": source,
            "trigger": "user_update_feed",
            "feed_generated": feed_generated,
        },
    )

    return {
        "status": "ok",
        "window_count": len(windows),
        "source": source,
        "feed_generated": feed_generated,
    }


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
    # Invalidate the cached tidal curve parameters so the next calculation
    # picks up any changes to stand_duration_minutes, stand_height_fraction, etc.
    invalidate_model_config_cache()
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


@app.get("/api/tides")
async def get_tides(range: str = "forecast"):
    """
    Return tide events for the Tides tab.

    range='forecast' (default): UKHO events from now to now+7 days.
    range='extended':            UKHO events for days 0-7 plus harmonic
                                  predictions for days 7-180. Each event
                                  carries an extra `data_source` field set
                                  to 'ukho' or 'harmonic' so the UI can
                                  show a per-row badge and insert a
                                  visible break between sources.
    range='history':              UKHO events from now-365 days up to now.

    Each event is returned with its `station` field intact ('langstone' or
    'portsmouth') so the UI can show a per-row source badge. The Portsmouth
    secondary-port offset is NOT applied here for stored UKHO events - the
    UI shows raw stored values from each station, with a badge indicating
    which one. Harmonic events have already been Langstone-corrected at
    storage time (scheduler applies the offset before write), so they need
    no further correction here; their station field is set to 'langstone'
    for consistency.

    Note: this endpoint is intentionally ungated - tide data is public.
    """
    if range not in ("forecast", "extended", "history"):
        raise HTTPException(400, "range must be 'forecast', 'extended', or 'history'")

    now = datetime.now(timezone.utc)
    if range == "forecast":
        start = now
        end = now + timedelta(days=7)
        events = get_tide_events(
            to_utc_str(start), to_utc_str(end), source="ukho"
        )
        for ev in events:
            ev["data_source"] = "ukho"
        return {"range": range, "events": events, "count": len(events)}

    if range == "history":
        # Rolling 12-month window. The cleanup_old_tide_data scheduled job
        # prevents data older than this from accumulating, so the upper
        # limit of the query is effectively bounded by retention.
        start = now - timedelta(days=365)
        end = now
        events = get_tide_events(
            to_utc_str(start), to_utc_str(end), source="ukho"
        )
        for ev in events:
            ev["data_source"] = "ukho"
        return {"range": range, "events": events, "count": len(events)}

    # range == 'extended': UKHO days 0-7 + harmonic days 7-180.
    ukho_end = now + timedelta(days=7)
    harmonic_end = now + timedelta(days=180)

    ukho_events = get_tide_events(
        to_utc_str(now), to_utc_str(ukho_end), source="ukho"
    )
    for ev in ukho_events:
        ev["data_source"] = "ukho"

    # Harmonic predictions are already Langstone-corrected (scheduler applies
    # the offset before storing). They include neither station nor source
    # columns from get_tide_events shape; tag them so the UI sees a uniform
    # event dict.
    harmonic_start = now + timedelta(days=7)
    harmonic_rows = get_harmonic_predictions(
        to_utc_str(harmonic_start), to_utc_str(harmonic_end), latest_only=True
    )
    harmonic_events = []
    for h in harmonic_rows:
        harmonic_events.append({
            "timestamp": h["timestamp"],
            "height_m": h["height_m"],
            "event_type": h["event_type"],
            "station": "langstone",  # post-offset; for badge consistency
            "source": "harmonic",
            "data_source": "harmonic",
            "is_approximate_time": True,
            "is_approximate_height": True,
        })

    # Defensive deduplication: drop any harmonic event whose timestamp is
    # within 90 minutes of a UKHO event of the same type. UKHO wins. Same
    # rule as in the 180d feed generator.
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
        clash = any(
            ukho_et == he["event_type"]
            and abs((he_dt - ukho_dt).total_seconds()) < 5400
            for ukho_dt, ukho_et in ukho_index
        )
        if not clash:
            filtered_harmonic.append(he)

    combined = ukho_events + filtered_harmonic
    combined.sort(key=lambda e: e["timestamp"])
    return {
        "range": range,
        "events": combined,
        "count": len(combined),
        "ukho_count": len(ukho_events),
        "harmonic_count": len(filtered_harmonic),
    }


# --- Standalone Langstone tide feeds ---
#
# These two feeds are unaffiliated with any mooring and serve UKHO tide
# events (next 7 days) and a UKHO+harmonic merge (next 180 days). They are
# regenerated daily by the scheduler at 02:00. Both routes regenerate the
# file on demand if it is missing or stale - this covers first deployment
# (no scheduler run yet) and any in-day regeneration triggered by a manual
# UKHO refresh.

@app.get("/feeds/Langstone_UKHO_7d.ics")
async def serve_langstone_ukho_7d():
    """Serve the Langstone UKHO 7-day tide feed. Regenerates if missing."""
    feed_path = FEEDS_DIR / "Langstone_UKHO_7d.ics"
    if not feed_path.exists():
        feed_path = generate_langstone_ukho_7d_feed()
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


@app.get("/feeds/Langstone_Harmonic_180d.ics")
async def serve_langstone_harmonic_180d():
    """Serve the Langstone 180-day tide feed. Regenerates if missing."""
    feed_path = FEEDS_DIR / "Langstone_Harmonic_180d.ics"
    if not feed_path.exists():
        feed_path = generate_langstone_harmonic_180d_feed()
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
