"""
Secondary port offset: converts Portsmouth values to Langstone Harbour.

Applied at three sites in the production data flow:
  - Harmonic model output (scheduler.daily_ukho_fetch + main.calculate when
    source='harmonic') before storage and consumption.
  - UKHO Portsmouth fallback rows on read (database.get_ukho_tide_events),
    so callers never see uncorrected fallback heights/times.
  - The manual /api/fetch-ukho path schedules wind jobs against corrected
    times when the API fell back to Portsmouth.

KHM data has its HW timing correction applied inside parse_khm_paste at
parse time; it is stored already-corrected with station="langstone" and is
NOT passed through this module on read. UKHO native Langstone data needs
no offset and bypasses this module entirely.

Correction values (validated April 2026 against UKHO half-hourly data,
refined April 2026 against the 16-day calibration corpus):
  HW: +9 minutes timing offset, no height offset
  LW: no time or height correction

The HW height offset was previously 0.05m. The 16-day calibration analysis
(scripts/calibrate_from_ukho_week.py) showed that propagating this offset
through curve interpolation gave a +0.05m mean bias in the production path
versus raw harmonic output, with no improvement in RMS - and a meaningful
worsening in the mid-tide band where the curve interpolator was most
affected. The 0.0-0.1m delta observed at six HW events in the original
validation appears to have been within the noise of half-hourly resolution
rather than a systematic offset. Removing it brings the production-path
harmonic mean bias close to zero.
"""

from datetime import timedelta
from dateutil import parser as dtparse

from app.config import to_utc_str, get_secondary_port_offset


# Reference defaults. The values actually used at runtime come from
# model_config.json (loaded via app.config.get_secondary_port_offset).
# These constants are kept here as readable documentation and as a
# fallback if the JSON is missing or malformed. To change the model
# behaviour, edit the JSON; do not edit these.
#
# Langstone corrections relative to Portsmouth.
#
# Timing offset (HW: +9 min) is supported by both the original April 2026
# half-hourly comparison and the 16-day calibration corpus.
#
# Height offset (HW: 0.0m) was previously 0.05m. The 16-day calibration
# analysis showed that propagating a +0.05m HW height bump through the
# curve interpolation yielded a +0.05m mean bias in the production-path
# harmonic test variant (scripts/calibrate_from_ukho_week.py), with no
# offsetting RMS improvement. The 0.0-0.1m HW height delta observed in
# the original 6-event validation appears to have been within the noise
# of half-hourly height resolution rather than a systematic offset.
#
# LW: no offset on either timing or height. Original validation showed
# both ports' LW heights effectively identical (0.7-1.0m range).
HW_TIME_OFFSET_MINUTES = 9
HW_HEIGHT_OFFSET_M = 0.0
LW_TIME_OFFSET_MINUTES = 0
LW_HEIGHT_OFFSET_M = 0.0


def apply_offset(events: list[dict]) -> list[dict]:
    """
    Apply Portsmouth→Langstone secondary port offset to tide events.

    Resolves the four offset values via the config accessors once per
    call. Each accessor caches after first use, so the JSON is parsed
    at most once per process; subsequent calls are O(1) dict lookups.
    """
    hw_time = get_secondary_port_offset("hw_time_offset_minutes", HW_TIME_OFFSET_MINUTES)
    hw_height = get_secondary_port_offset("hw_height_offset_m", HW_HEIGHT_OFFSET_M)
    lw_time = get_secondary_port_offset("lw_time_offset_minutes", LW_TIME_OFFSET_MINUTES)
    lw_height = get_secondary_port_offset("lw_height_offset_m", LW_HEIGHT_OFFSET_M)

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
            dt += timedelta(minutes=hw_time)
            new_ev["height_m"] = round(ev["height_m"] + hw_height, 2)
        elif event_type == "LowWater":
            dt += timedelta(minutes=lw_time)
            new_ev["height_m"] = round(ev["height_m"] + lw_height, 2)

        new_ev["timestamp"] = to_utc_str(dt)
        result.append(new_ev)

    return result
