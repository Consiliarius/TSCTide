"""
Secondary port offset: converts Portsmouth predictions to Langstone Harbour.

Applied to harmonic model predictions only. KHM data has corrections applied
within the KHM parser itself. UKHO Langstone data is native and needs no offset.

Correction values (validated April 2026 against UKHO half-hourly data):
  HW: +9 minutes, +0.05m
  LW: no time or height correction
"""

from datetime import timedelta
from dateutil import parser as dtparse

from app.config import to_utc_str


# Langstone corrections relative to Portsmouth
# Validated against April 2026 half-hourly UKHO data for both ports:
# Height delta observed at six HW events ranged 0.0-0.1m (mean +0.05m), not +0.24m
# as previously assumed. LW heights effectively identical (both ports 0.7-1.0m range).
# Timing: Langstone HW lags Portsmouth HW by roughly 0-30min at half-hour resolution;
# the +9min figure is consistent with this and retained.
HW_TIME_OFFSET_MINUTES = 9
HW_HEIGHT_OFFSET_M = 0.05
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
