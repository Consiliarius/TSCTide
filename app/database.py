"""SQLite persistence layer for tide data, moorings, observations, and calendar events."""

import sqlite3
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from app.config import DB_PATH, ensure_dirs, to_utc_str

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tide_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    height_m REAL NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    station TEXT NOT NULL,
    is_approximate_time INTEGER DEFAULT 0,
    is_approximate_height INTEGER DEFAULT 0,
    fetched_at TEXT NOT NULL,
    UNIQUE(timestamp, station, source)
);

CREATE TABLE IF NOT EXISTS moorings (
    mooring_id INTEGER PRIMARY KEY,
    boat_name TEXT DEFAULT '',
    draught_m REAL NOT NULL DEFAULT 1.0,
    drying_height_m REAL NOT NULL DEFAULT 2.0,
    safety_margin_m REAL NOT NULL DEFAULT 0.3,
    timezone TEXT NOT NULL DEFAULT 'Europe/London',
    wind_offset_enabled INTEGER DEFAULT 0,
    shallow_direction TEXT DEFAULT '',
    shallow_extra_depth_m REAL DEFAULT 0.0,
    calendar_enabled INTEGER DEFAULT 0,
    use_observations INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mooring_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    state TEXT NOT NULL,
    wind_direction TEXT DEFAULT '',
    direction_of_lay TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (mooring_id) REFERENCES moorings(mooring_id)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_uid TEXT PRIMARY KEY,
    mooring_id INTEGER NOT NULL,
    hw_timestamp TEXT NOT NULL,
    hw_height_m REAL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    wind_adjusted INTEGER DEFAULT 0,
    draught_m REAL,
    drying_height_m REAL,
    safety_margin_m REAL,
    obs_calibrated INTEGER DEFAULT 0,
    wind_direction TEXT,
    wind_speed_ms REAL,
    wind_offset_m REAL DEFAULT 0,
    always_accessible INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (mooring_id) REFERENCES moorings(mooring_id)
);

CREATE TABLE IF NOT EXISTS wind_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    direction_deg REAL,
    direction_compass TEXT,
    speed_ms REAL,
    source TEXT NOT NULL DEFAULT 'owm',
    fetched_at TEXT NOT NULL,
    UNIQUE(timestamp, source)
);

CREATE INDEX IF NOT EXISTS idx_tide_timestamp ON tide_data(timestamp);
CREATE INDEX IF NOT EXISTS idx_tide_source ON tide_data(source);
CREATE INDEX IF NOT EXISTS idx_obs_mooring ON observations(mooring_id);
CREATE INDEX IF NOT EXISTS idx_cal_mooring ON calendar_events(mooring_id);
CREATE INDEX IF NOT EXISTS idx_cal_hw ON calendar_events(hw_timestamp);
CREATE INDEX IF NOT EXISTS idx_wind_ts ON wind_observations(timestamp);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    scope TEXT NOT NULL,
    mooring_id INTEGER,
    severity TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_scope ON activity_log(scope, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_mooring ON activity_log(mooring_id, timestamp DESC);
"""


def get_db() -> sqlite3.Connection:
    """Get a database connection."""
    ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialise the database schema."""
    conn = get_db()
    conn.executescript(SCHEMA)

    # Migrate existing databases: add new metadata columns if missing
    cursor = conn.execute("PRAGMA table_info(calendar_events)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    migrations = {
        "draught_m": "ALTER TABLE calendar_events ADD COLUMN draught_m REAL",
        "drying_height_m": "ALTER TABLE calendar_events ADD COLUMN drying_height_m REAL",
        "safety_margin_m": "ALTER TABLE calendar_events ADD COLUMN safety_margin_m REAL",
        "obs_calibrated": "ALTER TABLE calendar_events ADD COLUMN obs_calibrated INTEGER DEFAULT 0",
        "wind_direction": "ALTER TABLE calendar_events ADD COLUMN wind_direction TEXT",
        "wind_speed_ms": "ALTER TABLE calendar_events ADD COLUMN wind_speed_ms REAL",
        "wind_offset_m": "ALTER TABLE calendar_events ADD COLUMN wind_offset_m REAL DEFAULT 0",
        "always_accessible": "ALTER TABLE calendar_events ADD COLUMN always_accessible INTEGER DEFAULT 0",
    }
    for col, sql in migrations.items():
        if col not in existing_cols:
            conn.execute(sql)

    # Migrate moorings table
    cursor2 = conn.execute("PRAGMA table_info(moorings)")
    mooring_cols = {row[1] for row in cursor2.fetchall()}
    if "use_observations" not in mooring_cols:
        conn.execute("ALTER TABLE moorings ADD COLUMN use_observations INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


@contextmanager
def db_connection():
    """Context manager for database connections."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Tide Data ---

def store_tide_events(events: list[dict], source: str, station: str):
    """Store tide events, replacing KHM data with UKHO if source is ukho.

    The station parameter must accurately reflect which station provided the
    data. Pass "langstone" for native Langstone UKHO data, "portsmouth" for
    Portsmouth fallback data. Do NOT pre-apply the secondary port offset
    before storing Portsmouth data — get_ukho_tide_events() applies it at
    query time so that provenance is preserved in the database.
    """
    now = to_utc_str(datetime.now(timezone.utc))
    with db_connection() as conn:
        for ev in events:
            ts = ev["timestamp"]
            height = ev["height_m"]
            event_type = ev["event_type"]
            approx_time = ev.get("is_approximate_time", False)
            approx_height = ev.get("is_approximate_height", False)

            if source == "ukho":
                # Delete any KHM data for the same timestamp/station.
                # Note: Portsmouth UKHO data (station="portsmouth") will NOT
                # delete KHM Langstone data (station="langstone"), which is
                # correct — KHM Langstone should only be superseded by native
                # Langstone UKHO data.
                conn.execute(
                    "DELETE FROM tide_data WHERE timestamp = ? AND station = ? AND source = 'khm'",
                    (ts, station)
                )

            conn.execute("""
                INSERT INTO tide_data (timestamp, height_m, event_type, source, station,
                                       is_approximate_time, is_approximate_height, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp, station, source) DO UPDATE SET
                    height_m = excluded.height_m,
                    event_type = excluded.event_type,
                    fetched_at = excluded.fetched_at
            """, (ts, height, event_type, source, station, approx_time, approx_height, now))


def get_tide_events(start: str, end: str, station: Optional[str] = None,
                    source: Optional[str] = None) -> list[dict]:
    """Get tide events between start and end ISO timestamps, optionally filtered by source."""
    with db_connection() as conn:
        query = "SELECT * FROM tide_data WHERE timestamp >= ? AND timestamp <= ?"
        params = [start, end]
        if station:
            query += " AND station = ?"
            params.append(station)
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY timestamp"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_ukho_tide_events(start: str, end: str) -> list[dict]:
    """
    Get UKHO tide events for the requested time range, applying the
    Portsmouth→Langstone secondary port offset if only fallback data exists.

    Priority:
      1. Native Langstone UKHO data (station="langstone") — returned as-is.
      2. Portsmouth fallback data (station="portsmouth") — secondary port
         offset applied before returning, so callers always receive
         Langstone-equivalent values regardless of which station provided them.

    This is the correct function to use for all access window calculations.
    Use get_tide_events() directly only when you need raw stored values or
    need to filter by a specific station for provenance purposes.
    """
    from app.secondary_port import apply_offset

    # Prefer native Langstone data
    events = get_tide_events(start, end, source="ukho", station="langstone")
    if events:
        return events

    # Fall back to Portsmouth data and apply Langstone correction
    portsmouth_events = get_tide_events(start, end, source="ukho", station="portsmouth")
    if portsmouth_events:
        logger.debug(
            f"get_ukho_tide_events: applying Portsmouth->Langstone offset to "
            f"{len(portsmouth_events)} fallback events"
        )
        return apply_offset(portsmouth_events)

    return []


# --- Moorings ---

def save_mooring(data: dict) -> dict:
    """Create or update a mooring configuration."""
    now = to_utc_str(datetime.now(timezone.utc))
    with db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM moorings WHERE mooring_id = ?", (data["mooring_id"],)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE moorings SET boat_name=?, draught_m=?, drying_height_m=?,
                    safety_margin_m=?, timezone=?, wind_offset_enabled=?,
                    shallow_direction=?, shallow_extra_depth_m=?, calendar_enabled=?,
                    use_observations=?, updated_at=?
                WHERE mooring_id=?
            """, (
                data.get("boat_name", ""),
                data["draught_m"],
                data["drying_height_m"],
                data["safety_margin_m"],
                data.get("timezone", "Europe/London"),
                data.get("wind_offset_enabled", 0),
                data.get("shallow_direction", ""),
                data.get("shallow_extra_depth_m", 0.0),
                data.get("calendar_enabled", 0),
                data.get("use_observations", 0),
                now,
                data["mooring_id"]
            ))
        else:
            conn.execute("""
                INSERT INTO moorings (mooring_id, boat_name, draught_m, drying_height_m,
                    safety_margin_m, timezone, wind_offset_enabled, shallow_direction,
                    shallow_extra_depth_m, calendar_enabled, use_observations,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data["mooring_id"],
                data.get("boat_name", ""),
                data["draught_m"],
                data["drying_height_m"],
                data["safety_margin_m"],
                data.get("timezone", "Europe/London"),
                data.get("wind_offset_enabled", 0),
                data.get("shallow_direction", ""),
                data.get("shallow_extra_depth_m", 0.0),
                data.get("calendar_enabled", 0),
                data.get("use_observations", 0),
                now, now
            ))
    return get_mooring(data["mooring_id"])


def get_mooring(mooring_id: int) -> Optional[dict]:
    """Get mooring by ID."""
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM moorings WHERE mooring_id = ?", (mooring_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_moorings() -> list[dict]:
    """Get all configured moorings."""
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM moorings ORDER BY mooring_id").fetchall()
    return [dict(r) for r in rows]


def get_calendar_enabled_moorings() -> list[dict]:
    """Get moorings with calendar subscription enabled."""
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM moorings WHERE calendar_enabled = 1 ORDER BY mooring_id"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Observations ---

def add_observation(data: dict) -> dict:
    """Add an observation record."""
    now = to_utc_str(datetime.now(timezone.utc))
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO observations (mooring_id, timestamp, state, wind_direction,
                                      direction_of_lay, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data["mooring_id"],
            data["timestamp"],
            data["state"],
            data.get("wind_direction", ""),
            data.get("direction_of_lay", ""),
            data.get("notes", ""),
            now
        ))
        return {"id": cursor.lastrowid, **data}


def get_observations(mooring_id: int) -> list[dict]:
    """Get all observations for a mooring."""
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM observations WHERE mooring_id = ? ORDER BY timestamp",
            (mooring_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_observation(observation_id: int, mooring_id: int) -> bool:
    """Delete a single observation. Enforces mooring ownership."""
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM observations WHERE id = ? AND mooring_id = ?",
            (observation_id, mooring_id)
        )
        return result.rowcount > 0


def clear_observations(mooring_id: int) -> int:
    """Delete all observations for a mooring. Returns count deleted."""
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM observations WHERE mooring_id = ?",
            (mooring_id,)
        )
        return result.rowcount


def load_classification_inputs(mooring_id: int):
    """
    Load all data needed for calibration in a single pass. Returns a tuple:
      (mooring, observations, tide_events, wind_observations, classifications)
    where classifications is a list of dicts from
    observation_classifier.classify_observations, one per observation,
    in the same order as observations.

    Returns (None, [], [], [], []) if the mooring does not exist.

    The full historical range is queried intentionally: older observations
    are still valid calibration data, and tide events/wind observations
    beyond the current 7-day window are needed to classify them.

    Callers that need both calibrate_drying_height and calibrate_wind_offset
    should call this once and pass the result to both via the _preloaded
    parameter, avoiding redundant DB queries and classifier passes.
    """
    from app.observation_classifier import classify_observations

    mooring = get_mooring(mooring_id)
    if not mooring:
        return None, [], [], [], []

    observations = get_observations(mooring_id)
    tide_events = get_tide_events("2000-01-01", "2099-12-31")
    wind_observations = get_wind_observations_in_range(
        "2000-01-01", "2099-12-31"
    )
    classifications = classify_observations(
        observations, mooring, tide_events, wind_observations
    )
    return mooring, observations, tide_events, wind_observations, classifications


def calibrate_drying_height(mooring_id: int, _preloaded=None) -> dict:
    """
    Estimate base drying height from observations tied to this mooring.

    Pass _preloaded=(mooring, observations, tide_events, wind_obs, classifications)
    from load_classification_inputs() to avoid redundant DB queries when both
    calibrate_drying_height and calibrate_wind_offset are needed in the same
    request.

    Afloat observations always contribute an upper bound. Aground
    observations classified "wind_offset" by app.observation_classifier
    are excluded here *only when their implied offset is strictly
    positive* - in that case they go to calibrate_wind_offset. Aground
    observations whose implied offset is non-positive fall through and
    contribute a lower bound to base drying, since they still record a
    grounding height even if the shallow-side offset is not constrained.

    Returns a dict with:
        best_estimate: float or None
        lower_bound: float or None (from aground observations)
        upper_bound: float or None (from afloat observations)
        confidence: str ('high', 'medium', 'low', 'partial-low',
                         'partial-high', 'inconsistent', 'none')
        matched: int (observations matched to tidal data)
        unmatched: int (observations outside tidal data range)
        afloat_count: int (base-classified afloat observations used)
        aground_count: int (aground observations feeding base-drying's
            lower bound; includes wind-offset-classified observations
            whose implied offset was non-positive and therefore fell
            through to base drying)
        excluded_wind_offset_count: int (wind-offset-classified aground
            observations whose implied offset was strictly positive and
            therefore went to calibrate_wind_offset instead of feeding
            the base-drying lower bound)
    """
    result = {
        "best_estimate": None, "lower_bound": None, "upper_bound": None,
        "confidence": "none", "matched": 0, "unmatched": 0,
        "afloat_count": 0, "aground_count": 0,
        "excluded_wind_offset_count": 0,
    }

    if _preloaded is not None:
        mooring, observations, tide_events, _wind, classifications = _preloaded
    else:
        mooring, observations, tide_events, _wind, classifications = (
            load_classification_inputs(mooring_id)
        )
    if not mooring or not observations or not tide_events:
        return result

    from app.access_calc import interpolate_height_at_time
    draught = mooring["draught_m"]
    try:
        current_drying = float(mooring.get("drying_height_m") or 0.0)
    except (TypeError, ValueError):
        current_drying = 0.0

    upper_bounds = []
    lower_bounds = []

    for cls in classifications:
        obs = cls["observation"]
        if obs.get("state") not in ("afloat", "aground"):
            continue

        height = interpolate_height_at_time(obs["timestamp"], tide_events)
        if height is None:
            result["unmatched"] += 1
            continue

        result["matched"] += 1
        implied_drying = height - draught

        if obs["state"] == "afloat":
            upper_bounds.append(implied_drying)
            result["afloat_count"] += 1
            continue

        # Aground observation from here on.
        if cls["classification"] == "wind_offset":
            # Does this observation actually constrain the wind offset?
            # An aground obs implies offset >= h - draught - current_drying.
            # If that is non-positive, the current base drying alone
            # explains the grounding height and the obs provides no new
            # information about the offset - but it is still a valid
            # aground observation for base drying. Fall it through to the
            # base-drying lower-bound pool below.
            #
            # This gate must stay in sync with calibrate_wind_offset,
            # which uses the same `implied > 0` test to decide which
            # observations contribute to the offset calibration.
            implied_offset = implied_drying - current_drying
            if implied_offset > 0:
                result["excluded_wind_offset_count"] += 1
                continue
            # else: fall through

        lower_bounds.append(implied_drying)
        result["aground_count"] += 1

    if not upper_bounds and not lower_bounds:
        return result

    upper = min(upper_bounds) if upper_bounds else None
    lower = max(lower_bounds) if lower_bounds else None

    result["upper_bound"] = round(upper, 2) if upper is not None else None
    result["lower_bound"] = round(lower, 2) if lower is not None else None

    if lower is not None and upper is not None:
        if lower < upper:
            result["best_estimate"] = round((lower + upper) / 2.0, 2)
            bound_range = upper - lower
            if bound_range < 0.2:
                result["confidence"] = "high"
            elif bound_range < 0.5:
                result["confidence"] = "medium"
            else:
                result["confidence"] = "low"
        else:
            # Bounds are inconsistent
            result["best_estimate"] = round((lower + upper) / 2.0, 2)
            result["confidence"] = "inconsistent"
    elif lower is not None:
        result["best_estimate"] = round(lower + 0.15, 2)
        result["confidence"] = "partial-low"
    elif upper is not None:
        result["best_estimate"] = round(upper - 0.15, 2)
        result["confidence"] = "partial-high"

    return result


def calibrate_wind_offset(mooring_id: int, _preloaded=None) -> dict:
    """
    Estimate the required shallow-side wind offset from aground
    observations classified as "wind_offset" by
    app.observation_classifier.

    Pass _preloaded=(mooring, observations, tide_events, wind_obs, classifications)
    from load_classification_inputs() to avoid redundant DB queries when both
    calibrate_drying_height and calibrate_wind_offset are needed in the same
    request.

    Only aground observations are used in v1. Each such observation
    implies: h(obs_time) <= draught + base_drying + wind_offset, i.e.
    wind_offset >= h(obs_time) - draught - base_drying. The returned
    suggestion is the tightest (largest) of these lower bounds.

    The mooring's currently-stored drying_height_m is used as the
    baseline. If the base drying height is updated, this calibration
    should be re-run.

    Returns a dict with:
        suggested_offset_m: float or None
        lower_bound: float or None (same value; kept for symmetry with
                     calibrate_drying_height)
        confidence: str ('partial-low' if any data, else 'none')
        observation_count: int (qualifying aground obs used)
        current_drying_height_m: float
        current_shallow_extra_depth_m: float
    """
    result = {
        "suggested_offset_m": None,
        "lower_bound": None,
        "confidence": "none",
        "observation_count": 0,
        "current_drying_height_m": None,
        "current_shallow_extra_depth_m": None,
    }

    if _preloaded is not None:
        mooring, observations, tide_events, _wind, classifications = _preloaded
    else:
        mooring, observations, tide_events, _wind, classifications = (
            load_classification_inputs(mooring_id)
        )
    if not mooring:
        return result

    try:
        current_drying = float(mooring.get("drying_height_m") or 0.0)
    except (TypeError, ValueError):
        current_drying = 0.0
    try:
        current_offset = float(mooring.get("shallow_extra_depth_m") or 0.0)
    except (TypeError, ValueError):
        current_offset = 0.0
    draught = mooring["draught_m"]

    result["current_drying_height_m"] = round(current_drying, 2)
    result["current_shallow_extra_depth_m"] = round(current_offset, 2)

    if not observations or not tide_events:
        return result

    from app.access_calc import interpolate_height_at_time

    implied_lower_bounds = []
    for cls in classifications:
        if cls["classification"] != "wind_offset":
            continue
        obs = cls["observation"]
        height = interpolate_height_at_time(obs["timestamp"], tide_events)
        if height is None:
            continue
        # Aground at height h implies: offset >= h - draught - base_drying.
        # Negative implied offset values mean the observation is already
        # consistent with the base drying alone (no offset needed); those
        # do not constrain the offset and are skipped.
        implied = height - draught - current_drying
        if implied > 0:
            implied_lower_bounds.append(implied)

    result["observation_count"] = len(implied_lower_bounds)

    if not implied_lower_bounds:
        return result

    suggested = max(implied_lower_bounds)  # tightest lower bound
    result["suggested_offset_m"] = round(suggested, 2)
    result["lower_bound"] = round(suggested, 2)
    # v1 uses aground-only data, which can only ever produce a lower bound.
    # Confidence stays at partial-low regardless of count.
    result["confidence"] = "partial-low"
    return result


# --- Calendar Events ---

def upsert_calendar_event(event: dict):
    """Insert or update a calendar event, respecting data source priority."""
    # Microsecond precision for updated_at to ensure ordering in rapid sequences.
    # Note: this intentionally uses .isoformat() (microseconds, +00:00 suffix)
    # rather than to_utc_str() (Z suffix, no microseconds) so that two events
    # written in the same second can still be ordered correctly. All updated_at
    # comparisons in cleanup_superseded_events use string comparison, which
    # works correctly provided the format is consistent (which it is, since all
    # updated_at values come through this single function).
    now = datetime.now(timezone.utc).isoformat()
    SOURCE_PRIORITY = {"ukho": 3, "khm": 2, "harmonic": 1}

    with db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM calendar_events WHERE event_uid = ?", (event["event_uid"],)
        ).fetchone()

        if existing:
            existing_priority = SOURCE_PRIORITY.get(existing["source"], 0)
            new_priority = SOURCE_PRIORITY.get(event["source"], 0)
            if new_priority < existing_priority:
                return  # Don't downgrade
            conn.execute("""
                UPDATE calendar_events SET hw_timestamp=?, hw_height_m=?, start_time=?,
                    end_time=?, source=?, title=?, wind_adjusted=?,
                    draught_m=?, drying_height_m=?, safety_margin_m=?, obs_calibrated=?,
                    wind_direction=?, wind_speed_ms=?, wind_offset_m=?,
                    always_accessible=?,
                    updated_at=?
                WHERE event_uid=?
            """, (
                event["hw_timestamp"], event.get("hw_height_m"),
                event["start_time"], event["end_time"],
                event["source"], event["title"],
                event.get("wind_adjusted", 0),
                event.get("draught_m"), event.get("drying_height_m"),
                event.get("safety_margin_m"), event.get("obs_calibrated", 0),
                event.get("wind_direction"), event.get("wind_speed_ms"),
                event.get("wind_offset_m", 0),
                event.get("always_accessible", 0),
                now, event["event_uid"]
            ))
        else:
            conn.execute("""
                INSERT INTO calendar_events (event_uid, mooring_id, hw_timestamp,
                    hw_height_m, start_time, end_time, source, title, wind_adjusted,
                    draught_m, drying_height_m, safety_margin_m, obs_calibrated,
                    wind_direction, wind_speed_ms, wind_offset_m, always_accessible,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event["event_uid"], event["mooring_id"],
                event["hw_timestamp"], event.get("hw_height_m"),
                event["start_time"], event["end_time"],
                event["source"], event["title"],
                event.get("wind_adjusted", 0),
                event.get("draught_m"), event.get("drying_height_m"),
                event.get("safety_margin_m"), event.get("obs_calibrated", 0),
                event.get("wind_direction"), event.get("wind_speed_ms"),
                event.get("wind_offset_m", 0),
                event.get("always_accessible", 0),
                now, now
            ))


def get_calendar_events(mooring_id: int, start: str = None, end: str = None) -> list[dict]:
    """Get calendar events for a mooring, optionally filtered by date range."""
    with db_connection() as conn:
        query = "SELECT * FROM calendar_events WHERE mooring_id = ?"
        params = [mooring_id]
        if start:
            query += " AND hw_timestamp >= ?"
            params.append(start)
        if end:
            query += " AND hw_timestamp <= ?"
            params.append(end)
        query += " ORDER BY hw_timestamp"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_future_events(mooring_id: int, after: str):
    """Delete future events for recalculation after observation update."""
    with db_connection() as conn:
        conn.execute(
            "DELETE FROM calendar_events WHERE mooring_id = ? AND hw_timestamp > ?",
            (mooring_id, after)
        )


def cleanup_old_events(days: int = 14):
    """Remove calendar events with HW times older than the given number of days.
    Only cleans calendar_events (computed windows); tide_data is preserved
    for observation calibration."""
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=days))
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM calendar_events WHERE hw_timestamp < ?", (cutoff,)
        )
        if result.rowcount:
            logger.info(f"Cleaned up {result.rowcount} calendar events older than {days} days")


def cleanup_superseded_events(mooring_id: int):
    """
    Remove events that have been superseded by other events for the same
    tidal cycle. Handles both cross-source upgrades (harmonic to UKHO) and
    same-source duplicates from successive calculations.

    For events with HW times within 90 minutes of each other:
      - Different priority: lower-priority event is removed
      - Same priority: older event is removed (newer is preferred)
    """
    SOURCE_PRIORITY = {"ukho": 3, "khm": 2, "harmonic": 1}

    events = get_calendar_events(mooring_id)
    if len(events) < 2:
        return

    from dateutil import parser as dtparse

    to_delete = set()
    for i, e1 in enumerate(events):
        if e1["event_uid"] in to_delete:
            continue
        for e2 in events[i + 1:]:
            if e2["event_uid"] in to_delete:
                continue
            if e1["event_uid"] == e2["event_uid"]:
                continue

            # Check if HW times are within 90 minutes (same tidal cycle)
            hw1 = dtparse.parse(e1["hw_timestamp"])
            hw2 = dtparse.parse(e2["hw_timestamp"])
            gap_minutes = abs((hw1 - hw2).total_seconds()) / 60

            if gap_minutes < 90:
                p1 = SOURCE_PRIORITY.get(e1["source"], 0)
                p2 = SOURCE_PRIORITY.get(e2["source"], 0)
                if p1 < p2:
                    to_delete.add(e1["event_uid"])
                elif p2 < p1:
                    to_delete.add(e2["event_uid"])
                else:
                    # Same priority: keep the most recently updated.
                    # If updated_at is identical (sub-second execution),
                    # keep the event with the later HW time (from newer data).
                    if e1["updated_at"] > e2["updated_at"]:
                        to_delete.add(e2["event_uid"])
                    elif e2["updated_at"] > e1["updated_at"]:
                        to_delete.add(e1["event_uid"])
                    else:
                        # Tiebreaker: later HW timestamp is from newer data
                        if e1["hw_timestamp"] >= e2["hw_timestamp"]:
                            to_delete.add(e2["event_uid"])
                        else:
                            to_delete.add(e1["event_uid"])

    if to_delete:
        with db_connection() as conn:
            for uid in to_delete:
                conn.execute(
                    "DELETE FROM calendar_events WHERE event_uid = ? AND mooring_id = ?",
                    (uid, mooring_id)
                )


# --- Wind Observations ---

def store_wind_observation(timestamp: str, direction_deg: float,
                           direction_compass: str, speed_ms: float, source: str = "owm"):
    """Store an observed wind reading."""
    now = to_utc_str(datetime.now(timezone.utc))
    with db_connection() as conn:
        conn.execute("""
            INSERT INTO wind_observations (timestamp, direction_deg, direction_compass,
                                           speed_ms, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(timestamp, source) DO UPDATE SET
                direction_deg = excluded.direction_deg,
                direction_compass = excluded.direction_compass,
                speed_ms = excluded.speed_ms,
                fetched_at = excluded.fetched_at
        """, (timestamp, direction_deg, direction_compass, speed_ms, source, now))


def get_latest_wind(before: str = None) -> Optional[dict]:
    """Get the most recent wind observation, optionally before a given time."""
    with db_connection() as conn:
        if before:
            row = conn.execute(
                "SELECT * FROM wind_observations WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                (before,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM wind_observations ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    return dict(row) if row else None


def get_wind_observations_in_range(start: str, end: str) -> list[dict]:
    """
    Return all wind observations with timestamps between start and end
    (inclusive, ISO strings). Used by the observation classifier to match
    aground observations against historical HW+4h wind samples.
    """
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM wind_observations "
            "WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp",
            (start, end)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Activity Log ---

def log_activity(event_type: str, message: str, severity: str = "info",
                 scope: str = "system", mooring_id: Optional[int] = None,
                 details: Optional[dict] = None):
    """
    Record an activity log entry.

    Args:
        event_type: Machine-readable event identifier (e.g., 'ukho_fetch')
        message: Human-readable summary
        severity: 'info' | 'success' | 'warning' | 'error'
        scope: 'system' or 'mooring'
        mooring_id: Required if scope='mooring'
        details: Optional dict of structured metadata (stored as JSON)
    """
    now = to_utc_str(datetime.now(timezone.utc))
    details_json = json.dumps(details) if details else None
    try:
        with db_connection() as conn:
            conn.execute("""
                INSERT INTO activity_log (timestamp, scope, mooring_id, severity,
                                           event_type, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now, scope, mooring_id, severity, event_type, message, details_json))
    except Exception as e:
        # Never let logging failures break the caller
        logger.warning(f"Failed to write activity log: {e}")


def get_activity_log(scope: Optional[str] = None, mooring_id: Optional[int] = None,
                     event_type: Optional[str] = None, severity: Optional[str] = None,
                     limit: int = 500) -> list[dict]:
    """Query the activity log with optional filters, newest first."""
    query = "SELECT * FROM activity_log WHERE 1=1"
    params = []
    if scope:
        query += " AND scope = ?"
        params.append(scope)
    if mooring_id is not None:
        query += " AND mooring_id = ?"
        params.append(mooring_id)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with db_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


def prune_activity_log(system_days: int = 30, mooring_days: int = 7):
    """
    Remove activity log entries older than retention policies.
    System-scope entries kept for 30 days by default; mooring-scope for 7.
    """
    system_cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=system_days))
    mooring_cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=mooring_days))
    with db_connection() as conn:
        result_sys = conn.execute(
            "DELETE FROM activity_log WHERE scope = 'system' AND timestamp < ?",
            (system_cutoff,)
        )
        result_moor = conn.execute(
            "DELETE FROM activity_log WHERE scope = 'mooring' AND timestamp < ?",
            (mooring_cutoff,)
        )
        total = result_sys.rowcount + result_moor.rowcount
        if total:
            logger.info(
                f"Pruned activity log: {result_sys.rowcount} system, "
                f"{result_moor.rowcount} mooring entries"
            )
