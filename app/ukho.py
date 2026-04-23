"""UKHO Admiralty Tidal API client."""

import httpx
import logging
from datetime import datetime, timezone

from app.config import UKHO_API_KEY, UKHO_STATION_ID, UKHO_FALLBACK_STATION_ID

logger = logging.getLogger(__name__)

BASE_URL = "https://admiraltyapi.azure-api.net/uktidalapi/api/V1"


async def fetch_tidal_events(
    station_id: str = None, duration: int = 7
) -> tuple[list[dict], str]:
    """
    Fetch tidal events from UKHO API.

    Returns a 2-tuple: (events, station_id_used).
      - events: list of dicts with timestamp, height_m, event_type.
        Raw values for the station that provided the data — no secondary
        port correction is applied here.
      - station_id_used: the UKHO station ID that actually supplied the
        data. Callers must check this: if it equals UKHO_FALLBACK_STATION_ID
        (Portsmouth) rather than UKHO_STATION_ID (Langstone), the events
        are raw Portsmouth values and the secondary port offset must be
        applied before use. See database.get_ukho_tide_events() which
        handles this transparently for DB-backed callers.

    All timestamps from UKHO are in UTC/GMT.
    Returns ([], "") on failure.
    """
    station = station_id or UKHO_STATION_ID
    url = f"{BASE_URL}/Stations/{station}/TidalEvents"
    params = {"duration": min(duration, 7)}  # API max is 7
    headers = {"Ocp-Apim-Subscription-Key": UKHO_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        events = []
        for item in data:
            event_type = item.get("EventType", "")
            ts = item.get("DateTime", "")
            height = item.get("Height")
            if not ts or height is None:
                continue

            # Normalise timestamp to ISO format with Z suffix
            if not ts.endswith("Z") and "+" not in ts and "-" not in ts[10:]:
                ts = ts + "Z"

            events.append({
                "timestamp": ts,
                "height_m": float(height),
                "event_type": event_type,
                "is_approximate_time": item.get("IsApproximateTime", False),
                "is_approximate_height": item.get("IsApproximateHeight", False),
            })

        logger.info(f"Fetched {len(events)} events from UKHO station {station}")
        return events, station

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404 and station == UKHO_STATION_ID and UKHO_FALLBACK_STATION_ID:
            logger.warning(
                f"Station {station} not found or insufficient data. "
                f"Falling back to {UKHO_FALLBACK_STATION_ID} (Portsmouth)."
            )
            return await fetch_tidal_events(
                station_id=UKHO_FALLBACK_STATION_ID, duration=duration
            )
        logger.error(f"UKHO API error: {e.response.status_code} - {e.response.text}")
        return [], ""
    except Exception as e:
        logger.error(f"UKHO API request failed: {e}")
        return [], ""
