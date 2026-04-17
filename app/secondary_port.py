"""
Secondary port offset: converts Portsmouth predictions to Langstone Harbour.

Applied to harmonic model predictions only. KHM data has corrections applied
within the KHM parser itself. UKHO Langstone data is native and needs no offset.

Correction values (from Admiralty data comparison):
  HW: +9 minutes, +0.24m
  LW: no time or height correction
"""

from datetime import timedelta
from dateutil import parser as dtparse

from app.config import to_utc_str


# Langstone corrections relative to Portsmouth
HW_TIME_OFFSET_MINUTES = 9
HW_HEIGHT_OFFSET_M = 0.24
LW_TIME_OFFSET_MINUTES = 0
LW_HEIGHT_OFFSET_M = 0.0


def apply_offset(events: list[dict]) -> list[dict]:
    """
    Apply Portsmouth→Langstone secondary port offset to tide events.
    """
    result = []

    for ev in events:
        new_ev = dict(ev)
        ts = ev["timestamp"]
        if isinstance(ts, str):
            dt = dtparse.parse(ts)
        else:
            dt = ts

        event_type = ev.get("event_type", "")

        if event_type == "HighWater":
            dt += timedelta(minutes=HW_TIME_OFFSET_MINUTES)
            new_ev["height_m"] = round(ev["height_m"] + HW_HEIGHT_OFFSET_M, 2)
        elif event_type == "LowWater":
            dt += timedelta(minutes=LW_TIME_OFFSET_MINUTES)
            new_ev["height_m"] = round(ev["height_m"] + LW_HEIGHT_OFFSET_M, 2)

        new_ev["timestamp"] = to_utc_str(dt)
        new_ev["offset_applied"] = True
        result.append(new_ev)

    return result
