"""OpenWeatherMap weather observation client and swing mooring offset logic.

Fetches current weather conditions (wind, pressure, precipitation,
visibility) from the OWM Current Weather API. The full response is
extracted by fetch_current_weather(); the original fetch_current_wind()
is a thin wrapper preserved for backward compatibility with callers that
only need wind data.

The same API call serves both the wind-offset check (existing) and the
Current Conditions panel (v2.6). No additional OWM requests are needed.
"""

import httpx
import logging
from datetime import datetime, timezone

from app.config import OWM_API_KEY, LOCATION_LAT, LOCATION_LON, to_utc_str

logger = logging.getLogger(__name__)

# Compass points in clockwise order
COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# Beaufort scale thresholds (m/s upper boundaries for forces 0-12)
_BEAUFORT = [
    (0.5, 0, "Calm"),
    (1.6, 1, "Light air"),
    (3.4, 2, "Light breeze"),
    (5.5, 3, "Gentle breeze"),
    (8.0, 4, "Moderate breeze"),
    (10.8, 5, "Fresh breeze"),
    (13.9, 6, "Strong breeze"),
    (17.2, 7, "Near gale"),
    (20.8, 8, "Gale"),
    (24.5, 9, "Strong gale"),
    (28.5, 10, "Storm"),
    (32.7, 11, "Violent storm"),
    (999, 12, "Hurricane force"),
]


def _beaufort(speed_ms: float) -> tuple[int, str]:
    """Return (force_number, description) for a wind speed in m/s."""
    for threshold, force, desc in _BEAUFORT:
        if speed_ms < threshold:
            return force, desc
    return 12, "Hurricane force"


def degrees_to_compass(deg: float) -> str:
    """Convert wind direction in degrees to 8-point compass."""
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
    opposite_idx = (idx + 4) % 8

    return [
        COMPASS_POINTS[(opposite_idx - 1) % 8],
        COMPASS_POINTS[opposite_idx],
        COMPASS_POINTS[(opposite_idx + 1) % 8],
    ]


def should_apply_offset(wind_compass: str, shallow_direction: str) -> bool:
    """
    Determine whether the wind offset should be applied.

    The offset is applied when the wind is blowing FROM one of the three
    sectors opposite to the shallow water direction -- i.e. wind pushing
    the boat toward the shallow side.
    """
    trigger_sectors = get_opposite_sector(shallow_direction)
    return wind_compass in trigger_sectors


async def fetch_current_weather() -> dict | None:
    """
    Fetch current weather from OpenWeatherMap, extracting all fields
    needed by the Current Conditions panel.

    Returns a dict with wind, pressure, precipitation, visibility, and
    conditions summary. Returns None on failure.

    The response shape is stable regardless of which OWM fields are
    present -- missing optional fields (rain, gust) get sensible
    defaults so callers do not need to check for None on every field.
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

        # -- Wind --
        wind_block = data.get("wind", {})
        deg = wind_block.get("deg", 0)
        speed = wind_block.get("speed", 0)
        gust = wind_block.get("gust")
        compass = degrees_to_compass(deg)
        force, force_desc = _beaufort(speed)

        # -- Pressure --
        main_block = data.get("main", {})
        pressure = main_block.get("pressure")

        # -- Precipitation --
        rain_block = data.get("rain", {})
        rain_1h = rain_block.get("1h", 0.0)
        if rain_1h > 4.0:
            rain_desc = "Heavy"
        elif rain_1h > 1.0:
            rain_desc = "Moderate"
        elif rain_1h > 0.0:
            rain_desc = "Light"
        else:
            rain_desc = "None"

        # -- Visibility --
        vis = data.get("visibility")
        if vis is None:
            vis_desc = "Unknown"
        elif vis >= 10000:
            vis_desc = "Good (10 km+)"
        elif vis >= 4000:
            vis_desc = "Moderate"
        elif vis >= 1000:
            vis_desc = "Poor"
        else:
            vis_desc = "Very poor (<1 km)"

        # -- Conditions summary --
        weather_list = data.get("weather", [])
        summary = weather_list[0].get("description", "") if weather_list else ""
        icon = weather_list[0].get("icon", "") if weather_list else ""

        now_str = to_utc_str(datetime.now(timezone.utc))

        result = {
            "wind": {
                "direction_deg": deg,
                "direction_compass": compass,
                "speed_ms": speed,
                "gust_ms": gust,
                "beaufort_force": force,
                "beaufort_description": force_desc,
            },
            "pressure": {
                "hpa": pressure,
                # trend is computed by conditions.py from pressure_history,
                # not by this function. Included as None placeholder so
                # callers have a consistent shape.
                "trend": None,
                "trend_description": None,
            },
            "precipitation": {
                "rain_mm_h": rain_1h,
                "description": rain_desc,
            },
            "visibility": {
                "metres": vis,
                "description": vis_desc,
            },
            "conditions": {
                "summary": summary,
                "icon": icon,
            },
            "fetched_at": now_str,
        }
        logger.info(
            f"Weather: {compass} ({deg}) {speed} m/s, "
            f"{pressure} hPa, rain {rain_1h} mm/h, vis {vis}m"
        )
        return result

    except Exception as e:
        logger.error(f"OWM API request failed: {e}")
        return None


async def fetch_pressure_forecast() -> list[dict] | None:
    """
    Fetch the OWM 5-day / 3-hourly pressure forecast for the barometric
    correction (v2.9).

    Returns a list of ``{"timestamp": ISO-Z, "pressure_hpa": float}`` steps
    (~40 entries at 3-hour spacing, ~5-day horizon), oldest first, or None on
    failure / no key. This is store-only data: it is fetched by the daily job
    and consumed later by the barometric pressure provider; it is never
    written into the tide tables.

    Prefers ``main.sea_level`` (sea-level pressure, what the inverse-barometer
    correction wants) and falls back to ``main.pressure``. At the Langstone
    lat/lon both are populated and equal (verified 15 June 2026), so the
    fallback is belt-and-braces for a station where they might diverge.
    """
    if not OWM_API_KEY:
        logger.warning("No OWM API key configured")
        return None

    url = "https://api.openweathermap.org/data/2.5/forecast"
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

        steps = []
        for entry in data.get("list", []):
            main_block = entry.get("main", {})
            pressure = main_block.get("sea_level")
            if pressure is None:
                pressure = main_block.get("pressure")
            dt_unix = entry.get("dt")
            if pressure is None or dt_unix is None:
                continue
            ts = to_utc_str(datetime.fromtimestamp(dt_unix, tz=timezone.utc))
            steps.append({"timestamp": ts, "pressure_hpa": float(pressure)})

        if not steps:
            logger.warning("OWM forecast returned no usable pressure steps")
            return None

        steps.sort(key=lambda s: s["timestamp"])
        logger.info(
            f"Pressure forecast: {len(steps)} steps, "
            f"{steps[0]['timestamp']} .. {steps[-1]['timestamp']}"
        )
        return steps

    except Exception as e:
        logger.error(f"OWM forecast request failed: {e}")
        return None


async def fetch_current_wind() -> dict | None:
    """
    Fetch current wind observation from OpenWeatherMap.

    Returns dict with direction_deg, direction_compass, speed_ms, timestamp.
    Returns None on failure.

    Thin wrapper around fetch_current_weather() for backward compatibility
    with callers that only need wind data (e.g. the wind-offset sampler
    in scheduler.py).
    """
    weather = await fetch_current_weather()
    if weather is None:
        return None
    w = weather["wind"]
    return {
        "direction_deg": w["direction_deg"],
        "direction_compass": w["direction_compass"],
        "speed_ms": w["speed_ms"],
        "timestamp": weather["fetched_at"],
    }
