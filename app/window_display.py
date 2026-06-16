"""
Display-only rounding of access-window edges (v2.9).

Every access window shown to a user -- in the /api/calculate response and in
generated .ics feeds/exports -- has its edges rounded to a coarse grid so the
UI never implies sub-grid precision the model does not have (its own timing
uncertainty is ~15-20 min; see model_config.json admiralty_convention_offset).
Rounding is conservative inward: the start moves later, the end moves earlier,
so a displayed window always sits inside the computed one and never overstates
access.

This is RENDER-ONLY. Computation, the stored calendar_events rows, the
feed-rewrite deadband comparison, and the calibration scripts all keep full
precision; only the values emitted to a user pass through here, via this one
shared helper so the API and the iCal paths cannot drift.

Because the /api/calculate response doubles as the round-trip payload posted
back to /feed/update (which STORES it) and /export-ics, the API path must not
overwrite the raw edges. ``display_fields`` therefore returns *additional*
display_* keys and leaves start_time/end_time alone; the iCal paths, which
read already-stored full-precision edges, round at the point of emit.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dtparse

from app.config import (
    to_utc_str, get_window_rounding_minutes, get_window_rounding_mode,
)

logger = logging.getLogger(__name__)

# Reference defaults; the runtime values come from model_config.json
# (window_display.*) via the app.config accessors. See
# docs/V2.9_BAROMETRIC_DESIGN.md section 4.6.
ROUNDING_MINUTES = 5
ROUNDING_MODE = "conservative_inward"

# Modes we have already warned about, so an unrecognised config value logs once.
_warned_modes: set[str] = set()


def round_window_conservative(start, end) -> Optional[tuple[datetime, datetime]]:
    """
    Conservative-inward round a bounded window's edges to the display grid.

    ``start``/``end`` may be datetimes or ISO-Z strings. Returns the rounded
    ``(start, end)`` as tz-aware UTC datetimes, or None when inward rounding
    collapses the window to under one grid step (rounded start >= rounded
    end) -- a sub-grid "negligible access" window that must not be shown or
    written to a feed.

    Call this only for genuine bounded windows. Special states
    (below_threshold, always_accessible, incomplete_data, and the
    wind_no_access start==end marker) are NOT window edges and must not be
    rounded; the callers gate on that before calling.

    Grid alignment is computed in UTC. The display timezone (Europe/London)
    is a whole-hour offset from UTC, so a 5-minute grid is identical in both
    -- no timezone subtlety at this grid size.
    """
    s = _coerce_dt(start)
    e = _coerce_dt(end)
    if s is None or e is None:
        return None

    minutes = get_window_rounding_minutes(ROUNDING_MINUTES)
    if minutes <= 0:
        # Rounding disabled by config: pass through, but still refuse an
        # inverted or zero-length window.
        return (s, e) if s < e else None

    mode = get_window_rounding_mode(ROUNDING_MODE)
    if mode != "conservative_inward" and mode not in _warned_modes:
        _warned_modes.add(mode)
        logger.warning(
            "window_display.rounding_mode=%r not recognised; "
            "using conservative_inward.", mode,
        )

    s_r = _snap(s, minutes, ceil=True)   # start -> later
    e_r = _snap(e, minutes, ceil=False)  # end   -> earlier
    if s_r >= e_r:
        return None
    return (s_r, e_r)


def display_fields(w: dict) -> dict:
    """
    Return the display-rounded fields to merge onto a calculated window dict
    for the /api/calculate response, WITHOUT touching its raw edges (which
    round-trip to /feed/update for full-precision storage).

    Keys returned:
      display_start_time, display_end_time, display_duration_minutes
      negligible_access      -- True when the vessel window collapsed
      display_tender_start_time, display_tender_end_time

    Only genuine bounded windows get display edges. Special states
    (below_threshold, always_accessible, incomplete_data, the wind_no_access
    start==end marker) and HW time/height are passed over -- the UI renders
    those without window times.
    """
    out: dict = {}

    if _is_roundable(w.get("start_time"), w.get("end_time"), w.get("always_accessible"),
                     w.get("below_threshold") or w.get("incomplete_data")
                     or w.get("wind_no_access")):
        rounded = round_window_conservative(w["start_time"], w["end_time"])
        if rounded is None:
            out["negligible_access"] = True
            out["display_start_time"] = None
            out["display_end_time"] = None
            out["display_duration_minutes"] = 0
        else:
            s, e = rounded
            out["display_start_time"] = to_utc_str(s)
            out["display_end_time"] = to_utc_str(e)
            out["display_duration_minutes"] = int((e - s).total_seconds() / 60)

    if _is_roundable(w.get("tender_start_time"), w.get("tender_end_time"),
                     w.get("tender_always_accessible"), False):
        rt = round_window_conservative(w["tender_start_time"], w["tender_end_time"])
        if rt is None:
            out["display_tender_start_time"] = None
            out["display_tender_end_time"] = None
        else:
            ts, te = rt
            out["display_tender_start_time"] = to_utc_str(ts)
            out["display_tender_end_time"] = to_utc_str(te)

    return out


def _is_roundable(start, end, always_accessible, other_special) -> bool:
    """A pair of edges is roundable only if both are present, distinct (not a
    start==end marker), and the window is not a special state."""
    return bool(
        start and end and start != end
        and not always_accessible and not other_special
    )


def _coerce_dt(value) -> Optional[datetime]:
    """Parse an ISO-Z string (or pass through a datetime) to tz-aware UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = dtparse.parse(value)
        except (ValueError, OverflowError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _snap(dt: datetime, minutes: int, ceil: bool) -> datetime:
    """Snap a tz-aware datetime to the N-minute grid, up (ceil) or down."""
    grid = minutes * 60
    total = round(dt.replace(microsecond=0).timestamp())
    if ceil:
        snapped = ((total + grid - 1) // grid) * grid
    else:
        snapped = (total // grid) * grid
    return datetime.fromtimestamp(snapped, tz=timezone.utc)
