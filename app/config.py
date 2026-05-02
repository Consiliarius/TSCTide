"""Application configuration from environment variables."""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def to_utc_str(dt: datetime) -> str:
    """
    Normalise a datetime to a UTC ISO string with Z suffix.
    All timestamps throughout the application use this format for consistency.
    Prevents mismatches from mixing 'Z' and '+00:00' representations.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
APP_DIR = Path(__file__).parent

# API keys
UKHO_API_KEY = os.environ.get("UKHO_API_KEY", "")
OWM_API_KEY = os.environ.get("OWM_API_KEY", "")

# UKHO station config
UKHO_STATION_ID = os.environ.get("UKHO_STATION_ID", "0066")
UKHO_FALLBACK_STATION_ID = os.environ.get("UKHO_FALLBACK_STATION_ID", "0065")

# Scheduling
UKHO_FETCH_HOUR = int(os.environ.get("UKHO_FETCH_HOUR", "2"))
UKHO_FETCH_MINUTE = int(os.environ.get("UKHO_FETCH_MINUTE", "0"))
WIND_SAMPLE_HW_OFFSET_HOURS = float(os.environ.get("WIND_SAMPLE_HW_OFFSET_HOURS", "4"))

# Location for OWM
LOCATION_LAT = float(os.environ.get("LOCATION_LAT", "50.8185"))
LOCATION_LON = float(os.environ.get("LOCATION_LON", "-0.9806"))

# PIN protection (v2)
# Site-wide salt used when hashing mooring PINs. Must be set in .env for
# PIN operations to succeed. Kept separate from the database so that
# someone with only the database file cannot independently reproduce
# hashes without also obtaining the salt. See README for admin-reset
# procedure.
PIN_HASH_SALT = os.environ.get("PIN_HASH_SALT", "")

# Defaults
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Europe/London")

# Paths
# Note: MOORINGS_DIR (legacy file-based storage) and WIND_LOG_PATH (unused)
# were removed. All mooring and wind data is stored in SQLite.
FEEDS_DIR = DATA_DIR / "feeds"
DB_PATH = DATA_DIR / "tides.db"

# Bundled model configuration shipped with the application. Read-only at
# runtime as of v2.5.5; see CALIBRATION_NOTES.md and config history for
# rationale.
#
# Previously the file was copied to /app/data/model_config.json on first
# run and that operative copy was used thereafter. That arrangement was
# designed to support a UI for live-editing model parameters; no such UI
# was ever built, the cache-invalidation path was never wired up, and the
# operative copy diverged from the bundled file silently across rebuilds.
# See git history for the v2.5.5 change that removed the persistence.
_BUNDLED_MODEL_CONFIG_PATH = APP_DIR / "model_config.json"


def ensure_dirs():
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FEEDS_DIR.mkdir(exist_ok=True)


def load_model_config() -> dict:
    """
    Load the bundled model configuration from app/model_config.json.

    The file is read-only at runtime. Edits must be made in the source
    repository and applied via container rebuild + restart. The
    `access_calc._get_curve_params` cache holds the parsed values for
    the lifetime of the process; restart is the deliberate refresh
    trigger.

    Returns an empty dict if the bundled file is absent (which would
    indicate a packaging fault rather than normal operation).
    """
    if not _BUNDLED_MODEL_CONFIG_PATH.exists():
        return {}
    with open(_BUNDLED_MODEL_CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Model parameter accessors with lenient JSON-overrides-defaults pattern (v2.5.6)
# ---------------------------------------------------------------------------
#
# The following helpers expose model parameters that have a hardcoded
# default in their respective .py modules but are also expressed in
# model_config.json. Behaviour:
#
#   * If the JSON entry is present and well-formed, the JSON value wins.
#   * If the JSON entry is missing or malformed, the supplied default
#     (passed by the caller) is used and a one-shot INFO message is
#     logged so an operator can see when fallback occurred.
#   * Each helper caches its result for the process lifetime to keep the
#     hot path (predict_height_at_time, ~43k calls/day) free of repeated
#     JSON parsing.
#
# Hot-path callers should bind the helper return to a local variable
# inside their function, not call the helper inside their loop.
#
# Adding a new accessor: define a parallel _get_X(default) function below,
# add a corresponding key to model_config.json, and document the JSON path
# in the docstring. Do not remove the .py default constant - per the
# v2.5.6 design, .py keeps the reference value as fallback.
#
# To force the cache to refresh, restart the process. There is no
# in-process invalidation by design.

# Cache of resolved model-config values keyed by JSON path string.
# Populated on first access and never cleared. Keys are dotted paths
# (e.g. "harmonic_reference.mean_level_m"); values are whatever shape
# the resolver returns (float, dict, etc.).
_resolved_cache: dict[str, Any] = {}

# Set of (json_path,) tuples for which a fallback log message has
# already been emitted, so subsequent fallbacks for the same key in
# the same process do not spam the log.
_logged_fallbacks: set[str] = set()


def _walk_path(cfg: dict, path: str) -> Optional[Any]:
    """
    Walk a dotted JSON path through a config dict, returning the value
    at the leaf or None if any segment is missing.

    Used for resolving keys like "harmonic_reference.mean_level_m"
    against the loaded model_config.json. Does not raise on missing
    intermediate dicts; returns None instead so the caller can fall
    back to a default.
    """
    node = cfg
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def _log_fallback_once(path: str, reason: str) -> None:
    """
    Emit a single INFO log line per (process, json_path) explaining
    that the JSON value was unavailable. Subsequent calls for the same
    path during the same process are silent.
    """
    if path in _logged_fallbacks:
        return
    _logged_fallbacks.add(path)
    logger.info(
        f"model_config.json: using module-level default for '{path}' ({reason}). "
        f"To make the JSON value authoritative, add the key with a valid value."
    )


def _resolve_scalar(path: str, default: Any, expected_type: type) -> Any:
    """
    Resolve a dotted JSON path to a scalar value of the expected type,
    falling back to the supplied default if the entry is missing or
    cannot be coerced. Result is cached for the process lifetime.

    Strings, ints, and floats are coerced via the type itself; other
    types are accepted only if isinstance matches exactly.
    """
    if path in _resolved_cache:
        return _resolved_cache[path]

    cfg = load_model_config()
    raw = _walk_path(cfg, path)

    if raw is None:
        _log_fallback_once(path, "key absent from JSON")
        _resolved_cache[path] = default
        return default

    if expected_type in (int, float):
        # Booleans are subclasses of int in Python; reject them explicitly
        # so a stray `true` in JSON does not silently coerce to 1.
        if isinstance(raw, bool):
            _log_fallback_once(path, "JSON value is boolean, not numeric")
            _resolved_cache[path] = default
            return default
        if isinstance(raw, (int, float)):
            try:
                value = expected_type(raw)
            except (TypeError, ValueError):
                _log_fallback_once(path, f"JSON value {raw!r} not coercible to {expected_type.__name__}")
                _resolved_cache[path] = default
                return default
            _resolved_cache[path] = value
            return value
        _log_fallback_once(path, f"JSON value {raw!r} is not numeric")
        _resolved_cache[path] = default
        return default

    if isinstance(raw, expected_type):
        _resolved_cache[path] = raw
        return raw

    _log_fallback_once(path, f"JSON value {raw!r} is not of expected type {expected_type.__name__}")
    _resolved_cache[path] = default
    return default


def get_z0(default: float) -> float:
    """
    Mean level above Chart Datum, metres.
    JSON path: harmonic_reference.mean_level_m
    """
    return _resolve_scalar("harmonic_reference.mean_level_m", default, float)


def get_harmonics(default: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """
    Harmonic constituents amplitude and phase lag.
    JSON path: harmonic_reference.constituents
    Each constituent in JSON is shaped {"amplitude": float, "phase_lag": float}.
    Returns a dict mapping constituent name to (amplitude, phase_lag) tuple,
    matching the in-code dict shape used by harmonic.py.

    Falls back to the supplied default if the JSON section is missing,
    malformed, or contains entries that cannot be coerced. Partial
    fallback is NOT supported - either the JSON section is fully
    valid or the entire default is used. This avoids the surprising
    case where one corrupted constituent silently reverts while
    others use JSON values; the harmonic synthesis uses all 19 in
    combination, so a partial mix would produce subtly wrong results.
    """
    path = "harmonic_reference.constituents"
    if path in _resolved_cache:
        return _resolved_cache[path]

    cfg = load_model_config()
    raw = _walk_path(cfg, path)

    if not isinstance(raw, dict):
        _log_fallback_once(path, "JSON section missing or not an object")
        _resolved_cache[path] = default
        return default

    parsed: dict[str, tuple[float, float]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            _log_fallback_once(path, f"constituent {name!r} is not an object")
            _resolved_cache[path] = default
            return default
        amp = entry.get("amplitude")
        phase = entry.get("phase_lag")
        if not isinstance(amp, (int, float)) or isinstance(amp, bool):
            _log_fallback_once(path, f"constituent {name!r} has non-numeric amplitude")
            _resolved_cache[path] = default
            return default
        if not isinstance(phase, (int, float)) or isinstance(phase, bool):
            _log_fallback_once(path, f"constituent {name!r} has non-numeric phase_lag")
            _resolved_cache[path] = default
            return default
        parsed[name] = (float(amp), float(phase))

    if not parsed:
        _log_fallback_once(path, "no valid constituents found in JSON section")
        _resolved_cache[path] = default
        return default

    _resolved_cache[path] = parsed
    return parsed


def get_secondary_port_offset(field: str, default: float) -> float:
    """
    Portsmouth -> Langstone secondary port offset components.
    field must be one of:
        "hw_time_offset_minutes"
        "hw_height_offset_m"
        "lw_time_offset_minutes"
        "lw_height_offset_m"
    JSON path: secondary_port_offset.<field>
    """
    return _resolve_scalar(f"secondary_port_offset.{field}", default, float)


# ---------------------------------------------------------------------------
# Cycle-number helper (shared across access_calc.py, ical_manager.py,
# database.py)
# ---------------------------------------------------------------------------
#
# Tide cycles are numbered relative to a fixed epoch, dividing
# hours-since-epoch by the average tidal cycle length and rounding.
# This produces a stable integer ID for each physical tide that:
#   - is the same regardless of which data source predicted the tide
#     (UKHO timing vs harmonic timing differ by tens of minutes at most),
#   - changes only when actual tide drifts more than half a cycle (~6h)
#     from where the rounding boundary lies, which never happens for
#     real tides.
#
# These constants ARE the dedup key for the harmonic_predictions table
# (column cycle_number) and the basis for calendar event UIDs. Changing
# them invalidates every existing UID and breaks the dedup index.
# Do not edit the values in JSON unless a database migration is also
# planned; the values here MUST remain bit-for-bit identical to those
# baked into existing rows.
#
# The defaults here are the canonical fallback referenced by the three
# consuming modules. The .py constants in those modules (where they
# still exist) are reference defaults only.
DEFAULT_CYCLE_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEFAULT_AVG_CYCLE_HOURS = 12.4167


def _get_cycle_epoch() -> datetime:
    """
    Return the cycle-numbering epoch as a timezone-aware UTC datetime.
    JSON path: cycle_number.epoch_iso (string, e.g. "2026-01-01T00:00:00Z")
    Falls back to DEFAULT_CYCLE_EPOCH if absent or malformed.
    """
    path = "cycle_number.epoch_iso"
    if path in _resolved_cache:
        return _resolved_cache[path]

    cfg = load_model_config()
    raw = _walk_path(cfg, path)

    if raw is None:
        _log_fallback_once(path, "key absent from JSON")
        _resolved_cache[path] = DEFAULT_CYCLE_EPOCH
        return DEFAULT_CYCLE_EPOCH

    if not isinstance(raw, str):
        _log_fallback_once(path, f"JSON value {raw!r} is not a string")
        _resolved_cache[path] = DEFAULT_CYCLE_EPOCH
        return DEFAULT_CYCLE_EPOCH

    # Accept "Z" suffix or +00:00 form for ISO timestamps.
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        _log_fallback_once(path, f"JSON value {raw!r} not a valid ISO datetime")
        _resolved_cache[path] = DEFAULT_CYCLE_EPOCH
        return DEFAULT_CYCLE_EPOCH

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    _resolved_cache[path] = dt
    return dt


def _get_avg_cycle_hours() -> float:
    """
    Return the average tidal cycle length in hours.
    JSON path: cycle_number.avg_cycle_hours
    """
    return _resolve_scalar(
        "cycle_number.avg_cycle_hours", DEFAULT_AVG_CYCLE_HOURS, float
    )


def compute_cycle_number(timestamp_iso: str) -> int:
    """
    Compute the tide-cycle number for an ISO UTC timestamp.

    Used as the deduplication key in harmonic_predictions and as part
    of calendar event UIDs. Stable under timestamp drift of much less
    than half a cycle (~6h), so seconds-to-tens-of-minutes drift between
    data sources or daily harmonic re-runs always maps to the same
    cycle number.

    Reads the epoch and cycle length from model_config.json on first
    call (each cached for the lifetime of the process), falling back
    to DEFAULT_CYCLE_EPOCH and DEFAULT_AVG_CYCLE_HOURS if the JSON
    entries are absent or malformed.

    Important: the resolved values become the dedup key for stored
    rows. Once data has been written under one set of constants, those
    constants must not change without a database migration. See the
    block comment above DEFAULT_CYCLE_EPOCH.
    """
    ts = timestamp_iso
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = _get_cycle_epoch()
    hours = (dt - epoch).total_seconds() / 3600.0
    return round(hours / _get_avg_cycle_hours())
