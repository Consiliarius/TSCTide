"""Application configuration from environment variables."""

import os
import json
from datetime import datetime, timezone
from pathlib import Path


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
