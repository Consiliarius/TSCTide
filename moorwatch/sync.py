"""Refresh the local vessel config from TSCTide. Run ashore, on wifi.

Reads ``GET /api/moorings/{id}`` (app/main.py:430), which is a PIN-free
endpoint -- the server returns the mooring through ``_public_mooring``, which
strips pin_hash. No PIN is needed and none is sent, so this holds no credential
on the boat.

stdlib urllib rather than httpx: the netbook should need nothing installed
beyond python-dateutil, and this runs once in a while over a hotspot.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from moorwatch.config import VesselConfig, save

TIMEOUT_SECONDS = 20


class SyncError(Exception):
    """Raised when the config could not be fetched or understood."""


def fetch_config(base_url: str, mooring_id: int,
                 timeout: float = TIMEOUT_SECONDS) -> dict:
    """Fetch one mooring's public config. Raises SyncError on any failure."""
    url = f"{base_url.rstrip('/')}/api/moorings/{mooring_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SyncError(f"Mooring {mooring_id} not found at {base_url}.") from e
        raise SyncError(f"{url} returned HTTP {e.code}.") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SyncError(f"Could not reach {base_url}: {e.reason if hasattr(e, 'reason') else e}") from e
    except json.JSONDecodeError as e:
        raise SyncError(f"{url} did not return JSON: {e}") from e


def sync(base_url: str, mooring_id: int,
         previous: Optional[VesselConfig] = None) -> tuple[VesselConfig, list[str]]:
    """Fetch and persist the mooring config, returning it and what changed.

    Takes no config of its own: this is the command that CREATES a usable
    config, so requiring one would deadlock a fresh install. ``previous`` is
    only for the change report, and is None on a first sync.

    The changed list is not decoration: a moved ``drying_height_m`` means the
    calibration corpus has re-estimated the seabed, which changes every depth
    this tool has been reporting. The operator should see that happen.
    """
    raw = fetch_config(base_url, mooring_id)

    for field in ("draught_m", "drying_height_m", "safety_margin_m"):
        if raw.get(field) is None:
            raise SyncError(
                f"Mooring {mooring_id} has no {field} set in TSCTide. "
                f"Configure the mooring there first."
            )

    try:
        fresh = VesselConfig(
            mooring_id=mooring_id,
            boat_name=str(raw.get("boat_name", "") or ""),
            draught_m=float(raw["draught_m"]),
            drying_height_m=float(raw["drying_height_m"]),
            safety_margin_m=float(raw["safety_margin_m"]),
            timezone=str(raw.get("timezone") or "Europe/London"),
            shallow_direction=str(raw.get("shallow_direction", "") or ""),
            shallow_extra_depth_m=float(raw.get("shallow_extra_depth_m") or 0.0),
            source_url=base_url,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            tsctide_commit=previous.tsctide_commit if previous else None,
        )
    except (TypeError, ValueError) as e:
        raise SyncError(f"Mooring {mooring_id} returned a malformed value: {e}") from e

    changes = []
    if previous is not None:
        for field in ("draught_m", "drying_height_m", "safety_margin_m",
                      "boat_name", "timezone"):
            old, new = getattr(previous, field), getattr(fresh, field)
            if old != new:
                changes.append(f"{field}: {old} -> {new}")

    save(fresh)
    return fresh, changes
