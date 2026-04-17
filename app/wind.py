"""
OpenWeatherMap wind observation client and swing mooring offset logic.

Fetches current wind direction and determines whether the wind-based
drying height offset should be applied for a given mooring configuration.
"""

import httpx
import logging
from datetime import datetime, timezone

from app.config import OWM_API_KEY, LOCATION_LAT, LOCATION_LON, to_utc_str

logger = logging.getLogger(__name__)

# Compass points in clockwise order
COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# Mapping from degrees to compass point
def degrees_to_compass(deg: float) -> str:
    """Convert wind direction in degrees to 8-point compass."""
    # Wind direction in meteorology: direction wind is coming FROM
    idx = round(deg / 45.0) % 8
    return COMPASS_POINTS[idx]


def get_opposite_sector(direction: str) -> list[str]:
    """
    Get the three compass sectors opposite to a given direction.
    Returns the opposite point and its two neighbours.

    E.g. if shallow water is to the W, opposite is E, and the
    trigger sectors are [NE, E, SE].
    """
    if direction not in COMPASS_POINTS:
        return []

    idx = COMPASS_POINTS.index(direction)
    opposite_idx = (idx + 4) % 8  # 180 degrees opposite

    return [
        COMPASS_POINTS[(opposite_idx - 1) % 8],
        COMPASS_POINTS[opposite_idx],
        COMPASS_POINTS[(opposite_idx + 1) % 8],
    ]


def should_apply_offset(wind_compass: str, shallow_direction: str) -> bool:
    """
    Determine whether the wind offset should be applied.

    The offset is applied when the wind is blowing FROM one of the three
    sectors opposite to the shallow water direction — i.e. wind pushing
    the boat toward the shallow side.
    """
    trigger_sectors = get_opposite_sector(shallow_direction)
    return wind_compass in trigger_sectors


async def fetch_current_wind() -> dict | None:
    """
    Fetch current wind observation from OpenWeatherMap.

    Returns dict with direction_deg, direction_compass, speed_ms, timestamp.
    Returns None on failure.
    """
    if not OWM_API_KEY:
        logger.warning("No OWM API key configured")
        return None

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": LOCATION_LAT,
        "lon": LOCATION_LON,
        "appid": OWM_API_KEY,
        "units": "metric",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        wind = data.get("wind", {})
        deg = wind.get("deg", 0)
        speed = wind.get("speed", 0)
        compass = degrees_to_compass(deg)

        result = {
            "direction_deg": deg,
            "direction_compass": compass,
            "speed_ms": speed,
            "timestamp": to_utc_str(datetime.now(timezone.utc)),
        }
        logger.info(f"Wind observation: {compass} ({deg}°) at {speed} m/s")
        return result

    except Exception as e:
        logger.error(f"OWM API request failed: {e}")
        return None
