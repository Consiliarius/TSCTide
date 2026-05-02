"""SQLite persistence layer for tide data, moorings, observations, and calendar events."""

import sqlite3
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from app.config import DB_PATH, ensure_dirs, to_utc_str, compute_cycle_number

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
    pin_hash TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS pin_failed_attempts (
    mooring_id INTEGER PRIMARY KEY,
    failed_count INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT NOT NULL,
    locked_until TEXT,
    FOREIGN KEY (mooring_id) REFERENCES moorings(mooring_id)
);

CREATE TABLE IF NOT EXISTS harmonic_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,           -- target tide event time, ISO UTC, Langstone-corrected
    cycle_number INTEGER,              -- tide cycle since 2026-01-01 epoch; primary dedup key (nullable for legacy rows pre-migration)
    height_m REAL NOT NULL,            -- predicted height in metres above CD
    event_type TEXT NOT NULL,          -- 'HighWater' or 'LowWater'
    generated_at TEXT NOT NULL,        -- ISO UTC time when this row was created
    UNIQUE (timestamp, event_type, generated_at)
);
CREATE INDEX IF NOT EXISTS idx_harm_timestamp ON harmonic_predictions(timestamp);
CREATE INDEX IF NOT EXISTS idx_harm_generated ON harmonic_predictions(generated_at);
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
    if "pin_hash" not in mooring_cols:
        conn.execute("ALTER TABLE moorings ADD COLUMN pin_hash TEXT DEFAULT ''")

    # Migrate harmonic_predictions: add cycle_number column for cycle-based
    # deduplication of latest_only queries. Existing rows are backfilled in
    # the same pass; the unique index is then created (idempotent via
    # IF NOT EXISTS) and lives outside the column-presence check so an
    # interrupted prior migration self-heals on next startup.
    cursor3 = conn.execute("PRAGMA table_info(harmonic_predictions)")
    harm_cols = {row[1] for row in cursor3.fetchall()}
    if "cycle_number" not in harm_cols:
        conn.execute(
            "ALTER TABLE harmonic_predictions ADD COLUMN cycle_number INTEGER"
        )
        # Backfill: compute cycle_number from each row's stored timestamp
        # using the shared helper. As of v2.5.6 the cycle constants live
        # in model_config.json (with .py defaults as fallback); routing
        # the migration through the same helper ensures backfilled values
        # match those produced by store_harmonic_predictions on subsequent
        # writes. The previous self-contained copy of the constants was
        # replaced because app.config is already imported at the top of
        # this module, so there is no circular-import risk to defend
        # against.
        rows = conn.execute(
            "SELECT id, timestamp FROM harmonic_predictions"
        ).fetchall()
        for r in rows:
            cyc = compute_cycle_number(r["timestamp"])
            conn.execute(
                "UPDATE harmonic_predictions SET cycle_number = ? WHERE id = ?",
                (cyc, r["id"])
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_harm_cycle_dedup "
        "ON harmonic_predictions(cycle_number, event_type, generated_at)"
    )

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
    """
    Add an observation record.

    The supplied timestamp is normalised to a UTC ISO-Z second-precision
    string before insert so that all stored observations share one format,
    regardless of which caller (XLSX upload, JSON API) wrote them. Without
    this, timestamps from the JSON `POST /observations` path could carry
    "+00:00" suffixes, fractional seconds, or naive local times, breaking
    string-based ORDER BY and equality comparisons elsewhere.

    Naive timestamps are interpreted as DEFAULT_TIMEZONE (matching the
    XLSX upload flow's documented "Times are local time" convention).
    """
    now = to_utc_str(datetime.now(timezone.utc))
    raw_ts = data["timestamp"]
    try:
        if isinstance(raw_ts, datetime):
            dt = raw_ts
        else:
            from dateutil import parser as _dtparse
            dt = _dtparse.parse(str(raw_ts))
        if dt.tzinfo is None:
            import pytz
            from app.config import DEFAULT_TIMEZONE
            dt = pytz.timezone(DEFAULT_TIMEZONE).localize(dt)
        normalized_ts = to_utc_str(dt)
    except (ValueError, TypeError) as e:
        # Surface unparseable input rather than silently corrupting the row.
        raise ValueError(f"Could not parse observation timestamp {raw_ts!r}: {e}")

    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO observations (mooring_id, timestamp, state, wind_direction,
                                      direction_of_lay, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data["mooring_id"],
            normalized_ts,
            data["state"],
            data.get("wind_direction", ""),
            data.get("direction_of_lay", ""),
            data.get("notes", ""),
            now
        ))
        # Return the stored (normalised) timestamp so callers see what the
        # DB actually holds, not what they sent.
        return {"id": cursor.lastrowid, **data, "timestamp": normalized_ts}


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


def delete_mooring(mooring_id: int) -> dict:
    """
    Permanently remove a mooring and its associated per-mooring data.

    Cascades into:
      - observations (per-mooring)
      - calendar_events (per-mooring)
      - pin_failed_attempts (per-mooring)

    Intentionally NOT touched:
      - activity_log: kept as audit trail per design decision. Old entries
        age out via prune_activity_log within 7 days for mooring scope.
      - wind_observations: not per-mooring, shared across all moorings.
      - tide_data: not per-mooring.

    The on-disk feed file (.ics under FEEDS_DIR) is the responsibility of
    the caller (see app.main.delete_mooring_config) - this function only
    handles database state.

    Returns a dict of deletion counts:
        {"mooring": 0|1, "observations": int, "calendar_events": int,
         "pin_failed_attempts": 0|1}
    """
    counts = {
        "mooring": 0,
        "observations": 0,
        "calendar_events": 0,
        "pin_failed_attempts": 0,
    }
    with db_connection() as conn:
        r = conn.execute(
            "DELETE FROM observations WHERE mooring_id = ?", (mooring_id,)
        )
        counts["observations"] = r.rowcount

        r = conn.execute(
            "DELETE FROM calendar_events WHERE mooring_id = ?", (mooring_id,)
        )
        counts["calendar_events"] = r.rowcount

        r = conn.execute(
            "DELETE FROM pin_failed_attempts WHERE mooring_id = ?", (mooring_id,)
        )
        counts["pin_failed_attempts"] = r.rowcount

        # Delete the mooring row last so that if any of the above fails,
        # the orphan rows can still be cleaned up by re-running.
        r = conn.execute(
            "DELETE FROM moorings WHERE mooring_id = ?", (mooring_id,)
        )
        counts["mooring"] = r.rowcount

    return counts


# --- PIN Protection (v2) ---
#
# PIN hashes are stored on the moorings table in column pin_hash. An
# empty string means "unclaimed" - the next write to a PIN-gated endpoint
# triggers the claim flow (user sets a new PIN).
#
# Rate limiting is tracked in pin_failed_attempts, keyed by mooring_id.
# Policy: up to MAX_PIN_ATTEMPTS failures within PIN_ATTEMPT_WINDOW_MINUTES
# before the mooring is locked for PIN_LOCKOUT_MINUTES. These constants
# live in app.pin so they can be imported by other modules without
# creating a database dependency.

def get_mooring_pin_hash(mooring_id: int) -> Optional[str]:
    """
    Return the stored PIN hash for a mooring, or None if the mooring
    does not exist, or an empty string if the mooring exists but is
    unclaimed. Callers must distinguish those two cases.
    """
    with db_connection() as conn:
        row = conn.execute(
            "SELECT pin_hash FROM moorings WHERE mooring_id = ?",
            (mooring_id,)
        ).fetchone()
    if row is None:
        return None
    return row["pin_hash"] or ""


def set_mooring_pin_hash(mooring_id: int, pin_hash: str) -> bool:
    """
    Store a PIN hash on an existing mooring row. Returns True on success,
    False if no such mooring exists. Does NOT create a mooring row;
    callers should ensure the row exists first (save_mooring).
    """
    now = to_utc_str(datetime.now(timezone.utc))
    with db_connection() as conn:
        result = conn.execute(
            "UPDATE moorings SET pin_hash = ?, updated_at = ? WHERE mooring_id = ?",
            (pin_hash, now, mooring_id)
        )
        return result.rowcount > 0


def clear_mooring_pin_hash(mooring_id: int) -> bool:
    """
    Remove the stored PIN hash from a mooring (returning it to the
    unclaimed state). Used by the admin-reset procedure if invoked
    through Python; the documented reset flow is to overwrite with a
    known hash via direct SQL.
    """
    return set_mooring_pin_hash(mooring_id, "")


def check_pin_lockout(mooring_id: int) -> Optional[dict]:
    """
    Return None if the mooring is not currently locked. If locked,
    returns {"locked_until": iso_str, "seconds_remaining": int}. The
    caller is responsible for returning a 429 with this information.

    A lockout expires naturally once locked_until is in the past; this
    function does not delete the row (cleared on next successful PIN
    verify or next failed attempt that starts a new window).
    """
    with db_connection() as conn:
        row = conn.execute(
            "SELECT locked_until FROM pin_failed_attempts WHERE mooring_id = ?",
            (mooring_id,)
        ).fetchone()
    if row is None or not row["locked_until"]:
        return None
    locked_until = dtparse_iso(row["locked_until"])
    now = datetime.now(timezone.utc)
    if locked_until <= now:
        return None
    return {
        "locked_until": row["locked_until"],
        "seconds_remaining": int((locked_until - now).total_seconds()),
    }


def record_failed_pin_attempt(mooring_id: int,
                              max_attempts: int,
                              window_minutes: int,
                              lockout_minutes: int) -> dict:
    """
    Record a failed PIN attempt and return the post-attempt state.

    Policy:
      - First failure (no row, or first_failed_at older than
        window_minutes ago): failed_count := 1, first_failed_at := now.
      - Subsequent failure within the window: failed_count increments.
      - When failed_count reaches max_attempts: locked_until := now +
        lockout_minutes. Any further attempts in the lockout window
        continue to be rejected by check_pin_lockout before reaching
        this function.

    The increment is performed by a single UPSERT statement with CASE
    expressions, so two concurrent failed attempts cannot interleave a
    SELECT/UPDATE pair and lose an increment. The previous read-then-write
    implementation had a small race window under WAL: both connections
    could observe failed_count=N before either committed, then each
    write N+1.

    Returns: {
        "failed_count":        current count,
        "attempts_remaining": max(0, max_attempts - failed_count),
        "locked_until":       iso_str or None,
    }
    """
    now = datetime.now(timezone.utc)
    now_str = to_utc_str(now)
    window_cutoff = to_utc_str(now - timedelta(minutes=window_minutes))
    lockout_until = to_utc_str(now + timedelta(minutes=lockout_minutes))

    with db_connection() as conn:
        # Single atomic UPSERT. Branches inside the CASE expressions
        # implement: "if the existing first_failed_at is older than the
        # window cutoff, start a fresh window with count=1; otherwise
        # increment the existing count". locked_until is set iff the
        # resulting count reaches max_attempts.
        #
        # All SET expressions in SQLite UPSERT reference the *old*
        # column values, so the inner CASE that derives the new
        # failed_count has to be repeated inside the locked_until SET
        # rather than referencing a freshly-set sibling column.
        conn.execute(
            "INSERT INTO pin_failed_attempts "
            "(mooring_id, failed_count, first_failed_at, locked_until) "
            "VALUES (?, 1, ?, CASE WHEN 1 >= ? THEN ? ELSE NULL END) "
            "ON CONFLICT(mooring_id) DO UPDATE SET "
            "  failed_count = CASE "
            "    WHEN first_failed_at < ? THEN 1 "
            "    ELSE failed_count + 1 "
            "  END, "
            "  first_failed_at = CASE "
            "    WHEN first_failed_at < ? THEN ? "
            "    ELSE first_failed_at "
            "  END, "
            "  locked_until = CASE "
            "    WHEN (CASE WHEN first_failed_at < ? THEN 1 "
            "                ELSE failed_count + 1 END) >= ? "
            "    THEN ? "
            "    ELSE NULL "
            "  END",
            (
                mooring_id, now_str, max_attempts, lockout_until,
                window_cutoff,
                window_cutoff, now_str,
                window_cutoff, max_attempts, lockout_until,
            ),
        )
        row = conn.execute(
            "SELECT failed_count, locked_until "
            "FROM pin_failed_attempts WHERE mooring_id = ?",
            (mooring_id,),
        ).fetchone()

    failed_count = row["failed_count"]
    locked_until_str = row["locked_until"]
    return {
        "failed_count": failed_count,
        "attempts_remaining": max(0, max_attempts - failed_count),
        "locked_until": locked_until_str,
    }


def clear_failed_pin_attempts(mooring_id: int) -> None:
    """
    Delete the failed-attempts row for a mooring. Called after a
    successful PIN verification.
    """
    with db_connection() as conn:
        conn.execute(
            "DELETE FROM pin_failed_attempts WHERE mooring_id = ?",
            (mooring_id,)
        )


def dtparse_iso(ts: str) -> datetime:
    """
    Parse an ISO timestamp stored by to_utc_str (format
    'YYYY-MM-DDTHH:MM:SSZ') back to a timezone-aware UTC datetime.
    Local helper to avoid a top-of-file dateutil import when the rest of
    the module uses raw datetime.
    """
    # The 'Z' suffix is equivalent to +00:00 in ISO 8601; fromisoformat
    # before Python 3.11 does not accept 'Z', so normalise first.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_classification_inputs(mooring_id: int):
    """
    Load all data needed for calibration in a single pass. Returns a tuple:
      (mooring, observations, tide_events, wind_observations, classifications)
    where classifications is a list of dicts from
    observation_classifier.classify_observations, one per observation,
    in the same order as observations.

    Returns (None, [], [], [], []) if the mooring does not exist.
    Returns (mooring, [], [], [], []) if the mooring exists but has no
    observations - the supporting-data queries are skipped entirely in
    that case.

    Tide events and wind observations are fetched bounded to a one-day
    buffer around the observation timestamps. The classifier needs each
    observation's preceding HW (≤12h earlier) and a wind sample within
    ±60 min of HW+4h, so one day is comfortably wider than required and
    keeps the queries from scanning the whole table. With years of
    retention this matters: the previous "2000-01-01" → "2099-12-31"
    bounds returned every row in the table on every call.

    Callers that need both calibrate_drying_height and calibrate_wind_offset
    should call this once and pass the result to both via the _preloaded
    parameter, avoiding redundant DB queries and classifier passes.
    """
    from app.observation_classifier import classify_observations

    mooring = get_mooring(mooring_id)
    if not mooring:
        return None, [], [], [], []

    observations = get_observations(mooring_id)
    if not observations:
        return mooring, [], [], [], []

    # Compute a bounded query window around the observation timestamps.
    # Observations stored via add_observation are normalised to ISO-Z, so
    # dtparse_iso handles them; older rows with "+00:00" suffixes are also
    # parsed correctly by dtparse_iso.
    obs_min = min(o["timestamp"] for o in observations)
    obs_max = max(o["timestamp"] for o in observations)
    try:
        window_start_dt = dtparse_iso(obs_min) - timedelta(days=1)
        window_end_dt = dtparse_iso(obs_max) + timedelta(days=1)
        window_start = to_utc_str(window_start_dt)
        window_end = to_utc_str(window_end_dt)
    except (ValueError, TypeError):
        # Defensive: if a malformed observation timestamp slipped past
        # add_observation's validation, fall back to the unbounded query
        # rather than skipping calibration entirely.
        window_start = "2000-01-01"
        window_end = "2099-12-31"

    tide_events = get_tide_events(window_start, window_end)
    wind_observations = get_wind_observations_in_range(window_start, window_end)
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
        # Aground-only observations give a lower bound but no upper bound:
        # the drying height could be anywhere from `lower` upwards. There is
        # no principled point estimate from a one-sided constraint, so we
        # do NOT suggest a value. The UI shows the lower bound as a fact
        # and presents no Apply button. Adding a fudge factor (e.g.
        # `lower + 0.15`) was wrong here: it made the system suggest
        # reductions in the stored drying height that the data did not
        # actually support, sometimes lowering it below known-grounded
        # observations.
        result["best_estimate"] = None
        result["confidence"] = "partial-low"
    elif upper is not None:
        # Symmetric reasoning to the partial-low branch: afloat-only
        # observations give an upper bound but no lower bound. No point
        # estimate is offered.
        result["best_estimate"] = None
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
    for observation calibration and the historical Tides tab view."""
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=days))
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM calendar_events WHERE hw_timestamp < ?", (cutoff,)
        )
        if result.rowcount:
            logger.info(f"Cleaned up {result.rowcount} calendar events older than {days} days")


def cleanup_old_tide_data(days: int = 365):
    """
    Remove tide_data rows older than the given number of days.

    Default 365 days matches the rolling 12-month retention policy for the
    historical Tides tab view. tide_data is otherwise written-once-and-kept,
    so without this cleanup the table would grow unbounded.

    Note: observation calibration relies on having tide events that bracket
    each observation. If observations older than `days` exist, they will
    become un-matchable to tide data and will count as `unmatched` in
    calibration responses. With the default 12-month window this is a
    non-issue in practice - the calibrate functions only consider
    observations the user has recorded, and observations more than a year
    old are unlikely to be representative of current mooring conditions.
    """
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=days))
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM tide_data WHERE timestamp < ?", (cutoff,)
        )
        if result.rowcount:
            logger.info(
                f"Cleaned up {result.rowcount} tide_data rows older than {days} days"
            )


# --- Harmonic Predictions ---
#
# Stores the harmonic model's predicted tide events for the next N days,
# regenerated daily by the scheduler. Two purposes:
#   1. Powers the 180-day Forecast view and the Langstone_Harmonic_180d.ics
#      feed without recomputing the harmonic model on every page load.
#   2. Preserves a record of past predictions so that, periodically, the
#      delta between predicted-on-day-X and actual-UKHO-on-day-X can be
#      analysed to refine the harmonic constants. Hence the per-row
#      generated_at column rather than overwriting on each daily run.
#
# Deduplication: the same physical tide computed by predict_events on
# different days produces timestamps that drift by seconds (the sampling
# grid is anchored at "now", which advances 24h+drift between daily
# runs). To collapse those drifting timestamps to one row per tide cycle
# in latest_only queries, each row is tagged with a cycle_number
# (= round(hours_since_2026-01-01 / 12.4167)). cycle_number is stable
# under drift of much less than half a cycle (several hours), so seconds
# of drift always map to the same cycle. The matching UNIQUE INDEX on
# (cycle_number, event_type, generated_at) enforces 'one row per
# (cycle, type) per write batch' at storage time as well as query time.
#
# This table is intentionally separate from tide_data:
#   - tide_data holds authoritative UKHO/KHM observations used by
#     observation calibration; harmonic predictions must NOT contaminate
#     the calibration inputs.
#   - get_ukho_tide_events / get_tide_events / load_classification_inputs
#     all query tide_data only and never see harmonic_predictions.

# Reference defaults. The values actually used at runtime come from
# model_config.json (loaded via app.config.compute_cycle_number). These
# constants are kept here as readable documentation and as a fallback
# if the JSON is missing or malformed. To change the model behaviour,
# edit the JSON; do not edit these.
#
# Constants for cycle-based deduplication. Match the values used by
# access_calc.generate_event_uid and ical_manager._tide_event_uid.
# Critically, these values are the dedup key for stored rows in
# harmonic_predictions.cycle_number; changing them invalidates every
# existing row's cycle assignment and must not be done without a
# database migration.
_HARM_CYCLE_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_HARM_CYCLE_HOURS = 12.4167


def _compute_cycle_number(timestamp_iso: str) -> int:
    """
    Compute the tide-cycle number for an ISO UTC timestamp, used as the
    deduplication key in harmonic_predictions. The same physical tide
    re-computed from a slightly different sample grid produces timestamps
    differing by seconds; the rounded cycle number is stable under such
    drift, making it a robust dedup key.

    Thin wrapper around app.config.compute_cycle_number so the dedup
    key matches what access_calc.generate_event_uid and
    ical_manager._tide_event_uid produce. Local function kept rather
    than calling the helper directly at every call site to preserve
    the documented module-level API.
    """
    return compute_cycle_number(timestamp_iso)


def store_harmonic_predictions(events: list[dict]) -> int:
    """
    Insert a batch of harmonic predictions, all tagged with the current
    UTC time as generated_at and with a derived cycle_number. Each input
    event is a dict with at least timestamp (ISO UTC string), height_m
    (float), event_type (string).

    Returns the count of rows the call attempted to insert. Conflicts on
    either UNIQUE constraint - the original (timestamp, event_type,
    generated_at) or the cycle-based (cycle_number, event_type,
    generated_at) index - are silently ignored. Under normal once-per-day
    operation no conflicts occur because each batch has a fresh generated_at.
    """
    if not events:
        return 0
    now = to_utc_str(datetime.now(timezone.utc))
    inserted = 0
    with db_connection() as conn:
        for ev in events:
            try:
                cycle = _compute_cycle_number(ev["timestamp"])
                conn.execute(
                    "INSERT OR IGNORE INTO harmonic_predictions "
                    "(timestamp, height_m, event_type, generated_at, cycle_number) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ev["timestamp"], ev["height_m"], ev["event_type"], now, cycle),
                )
                inserted += 1
            except (sqlite3.IntegrityError, KeyError, ValueError) as e:
                logger.warning(f"Skipped malformed harmonic event: {e}")
    return inserted


def get_harmonic_predictions(start: str, end: str,
                              latest_only: bool = True) -> list[dict]:
    """
    Return harmonic predictions whose target timestamp falls in [start, end].

    With latest_only=True (default), returns only the most recently generated
    row for each (timestamp, event_type) pair. This is what the Forecast view
    and the 180d feed need - the freshest prediction available.

    With latest_only=False, returns every stored prediction including older
    versions for the same target time. Used for historical delta analysis
    (compare predictions made at various lead times against actual UKHO).
    """
    with db_connection() as conn:
        if latest_only:
            # Group by (cycle_number, event_type) so that the same physical
            # tide cycle, whose timestamps drift seconds-to-tens-of-seconds
            # between daily runs, collapses to a single row per group with
            # the freshest generated_at winning. SQLite's IS operator is
            # used in place of = so any legacy NULL cycle_number rows
            # (should not occur post-migration) self-group correctly.
            rows = conn.execute(
                "SELECT * FROM harmonic_predictions h1 "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "AND generated_at = ("
                "  SELECT MAX(generated_at) FROM harmonic_predictions h2 "
                "  WHERE h2.cycle_number IS h1.cycle_number "
                "    AND h2.event_type = h1.event_type"
                ") "
                "ORDER BY timestamp",
                (start, end),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM harmonic_predictions "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "ORDER BY timestamp, generated_at",
                (start, end),
            ).fetchall()
    return [dict(r) for r in rows]


def cleanup_old_harmonic_predictions(days: int = 365):
    """
    Remove harmonic_predictions rows whose target timestamp is older than
    the given number of days. Matches the tide_data retention policy.

    A prediction made today for a tide 200 days in the future will not be
    pruned until that target tide is itself >365 days in the past. This is
    intentional: while the prediction sits in the future, it remains useful
    as a forecast; once the tide has happened and a year has passed, the
    delta-vs-actual analysis is no longer relevant.
    """
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=days))
    with db_connection() as conn:
        result = conn.execute(
            "DELETE FROM harmonic_predictions WHERE timestamp < ?", (cutoff,)
        )
        if result.rowcount:
            logger.info(
                f"Cleaned up {result.rowcount} harmonic prediction rows "
                f"older than {days} days"
            )


def compute_harmonic_residuals(days: int = 30) -> dict:
    """
    Compare stored harmonic predictions against actual UKHO HW/LW events for
    the trailing `days` days, ending at "now". Returns aggregated residual
    statistics suitable for activity-log monitoring.

    Matching:
      - For each UKHO HW/LW event with timestamp in [now - days, now], find
        the freshest harmonic prediction sharing the same (cycle_number,
        event_type). cycle_number is the same epoch-anchored quantity used
        elsewhere in this module (round((hours_since_2026_01_01) / 12.4167)),
        so harmonic timestamps drifting by minutes from UKHO still match
        the same physical tide.
      - Both sides are Langstone-corrected at storage time:
          * harmonic_predictions stores rows already passed through
            secondary_port.apply_offset() in scheduler.daily_ukho_fetch.
          * tide_data stations are "langstone" (native, returned as-is by
            get_ukho_tide_events) or "portsmouth" (fallback, returned
            after offset). This function uses get_ukho_tide_events() so
            the Portsmouth fallback offset is applied transparently.
        Direct predicted-minus-actual differencing is therefore valid for
        both height and timing; no further correction is applied here.

    Sign convention:
      residual = predicted - actual
      Positive = harmonic over-predicts; negative = harmonic under-predicts.
      Matches the convention used by scripts/calibrate_from_ukho_week.py
      so that numbers reported here are directly comparable.

    Returns a dict shaped for the activity-log details JSON:
        {
            "window_days": int,
            "window_start": ISO UTC string,
            "window_end":   ISO UTC string,
            "matched":      total UKHO events with a harmonic match,
            "unmatched":    total UKHO events without a match,
            "hw": {"count": int, "height_mean": float, "height_rms": float,
                    "height_max_abs": float, "timing_mean_min": float,
                    "timing_rms_min": float, "timing_max_abs_min": float},
            "lw": { ... same shape ... },
        }

    Returns the dict with all-None numeric fields and zero counts if the
    window contains no matched pairs (e.g. first run, fresh database).
    Callers must handle the empty-window case rather than this function
    raising.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    start_str = to_utc_str(window_start)
    end_str = to_utc_str(now)

    # UKHO events in the window. get_ukho_tide_events() applies the
    # Portsmouth->Langstone offset for fallback rows, so the events are
    # always Langstone-equivalent regardless of which station provided them.
    ukho_events = get_ukho_tide_events(start_str, end_str)

    # Harmonic predictions covering the same window. Slightly widen the
    # query at both ends so a UKHO event near the edge whose harmonic
    # twin lies just outside [start, end] still matches. Half a tidal
    # cycle (~6.2h) on either side is generous and cheap.
    harm_query_start = to_utc_str(window_start - timedelta(hours=7))
    harm_query_end = to_utc_str(now + timedelta(hours=7))
    harm_events = get_harmonic_predictions(
        harm_query_start, harm_query_end, latest_only=True
    )

    # Index harmonic events by (cycle_number, event_type) for O(1) lookup.
    # cycle_number is populated for all post-migration rows; legacy NULL
    # rows are excluded (they cannot be matched and would skew counts).
    harm_index: dict[tuple[int, str], dict] = {}
    for h in harm_events:
        cyc = h.get("cycle_number")
        if cyc is None:
            continue
        harm_index[(cyc, h["event_type"])] = h

    hw_height_resid: list[float] = []
    lw_height_resid: list[float] = []
    hw_timing_resid_min: list[float] = []
    lw_timing_resid_min: list[float] = []
    matched = 0
    unmatched = 0

    for u in ukho_events:
        et = u["event_type"]
        if et not in ("HighWater", "LowWater"):
            continue
        u_cyc = _compute_cycle_number(u["timestamp"])
        h = harm_index.get((u_cyc, et))
        if h is None:
            unmatched += 1
            continue

        height_resid = h["height_m"] - u["height_m"]
        try:
            u_dt = datetime.fromisoformat(
                u["timestamp"].replace("Z", "+00:00")
            )
            h_dt = datetime.fromisoformat(
                h["timestamp"].replace("Z", "+00:00")
            )
            timing_resid_min = (h_dt - u_dt).total_seconds() / 60.0
        except (ValueError, KeyError):
            # Defensive: a malformed timestamp on either side should not
            # poison the whole window. Skip this pair, log nothing here
            # (caller logs aggregate stats), continue.
            unmatched += 1
            continue

        if et == "HighWater":
            hw_height_resid.append(height_resid)
            hw_timing_resid_min.append(timing_resid_min)
        else:
            lw_height_resid.append(height_resid)
            lw_timing_resid_min.append(timing_resid_min)
        matched += 1

    def _stats(height_vals: list[float], timing_vals: list[float]) -> dict:
        n = len(height_vals)
        if n == 0:
            return {
                "count": 0,
                "height_mean": None, "height_rms": None,
                "height_max_abs": None,
                "timing_mean_min": None, "timing_rms_min": None,
                "timing_max_abs_min": None,
            }
        h_mean = sum(height_vals) / n
        h_rms = (sum(x * x for x in height_vals) / n) ** 0.5
        h_max = max(abs(x) for x in height_vals)
        t_mean = sum(timing_vals) / n
        t_rms = (sum(x * x for x in timing_vals) / n) ** 0.5
        t_max = max(abs(x) for x in timing_vals)
        return {
            "count": n,
            "height_mean": round(h_mean, 3),
            "height_rms": round(h_rms, 3),
            "height_max_abs": round(h_max, 2),
            "timing_mean_min": round(t_mean, 1),
            "timing_rms_min": round(t_rms, 1),
            "timing_max_abs_min": round(t_max, 1),
        }

    return {
        "window_days": days,
        "window_start": start_str,
        "window_end": end_str,
        "matched": matched,
        "unmatched": unmatched,
        "hw": _stats(hw_height_resid, hw_timing_resid_min),
        "lw": _stats(lw_height_resid, lw_timing_resid_min),
    }


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
