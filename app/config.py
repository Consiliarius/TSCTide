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

# Defaults
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Europe/London")

# Paths
# Note: MOORINGS_DIR (legacy file-based storage) and WIND_LOG_PATH (unused)
# were removed. All mooring and wind data is stored in SQLite.
FEEDS_DIR = DATA_DIR / "feeds"
DB_PATH = DATA_DIR / "tides.db"
MODEL_CONFIG_PATH = DATA_DIR / "model_config.json"


def ensure_dirs():
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FEEDS_DIR.mkdir(exist_ok=True)


def load_model_config() -> dict:
    """Load model config from data dir, falling back to bundled default."""
    if MODEL_CONFIG_PATH.exists():
        with open(MODEL_CONFIG_PATH) as f:
            return json.load(f)
    # Copy bundled default to data dir on first run
    bundled = APP_DIR / "model_config.json"
    if bundled.exists():
        with open(bundled) as f:
            cfg = json.load(f)
        save_model_config(cfg)
        return cfg
    return {}


def save_model_config(cfg: dict):
    """Persist model config to data dir."""
    ensure_dirs()
    with open(MODEL_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)
