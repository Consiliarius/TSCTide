"""
Parser for KHM Portsmouth tide table data pasted from the Royal Navy website.

Expected format: 13 tab-separated columns per line.
Column layout:
  0: Date (DD/MM or DD/MM/YYYY)
  1: (ignored — typically a day code or sunrise time)
  2: HW1 time (HH:MM)
  3: HW1 height (m)
  4: LW1 time (HH:MM)
  5: LW1 height (m)
  6: HW2 time (HH:MM)
  7: HW2 height (m)
  8: LW2 time (HH:MM)
  9: LW2 height (m)
  10-12: (ignored — typically additional data like moon phase percentage)

Times are local (GMT or BST as published by KHM).
All data is for Portsmouth. Secondary port correction to Langstone is applied
within this parser (HW: +9 min timing only; no height offset; LW unchanged),
validated April 2026 against UKHO half-hourly data, refined April 2026 against
the 16-day calibration corpus. See app/secondary_port.py for the full rationale.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.config import get_secondary_port_offset
from app.secondary_port import HW_TIME_OFFSET_MINUTES

logger = logging.getLogger(__name__)


def parse_khm_paste(text: str, year: int = None, is_bst: bool = True) -> list[dict]:
    """
    Parse pasted KHM tide table text into structured events with
    Langstone secondary port corrections applied.

    Args:
        text: Raw pasted text from KHM tide table page.
        year: Year to assume if not in the date column.
        is_bst: Whether the times are in BST (True) or GMT (False).

    Returns:
        List of tide event dicts ready for storage, with timestamps in UTC
        and Langstone corrections applied.
    """
    if not year:
        year = datetime.now().year

    local_tz = timezone(timedelta(seconds=3600)) if is_bst else timezone.utc
    lines = [l for l in text.strip().split("\n") if l.strip()]
    events = []
    parse_errors = 0

    for line in lines:
        # Split by tab (primary) or multiple spaces (fallback for copy-paste issues)
        if "\t" in line:
            parts = [s.strip() for s in line.split("\t")]
        else:
            parts = [s.strip() for s in line.split("  ") if s.strip()]

        if len(parts) < 10:
            parse_errors += 1
            continue

        # Column 0: Date (DD/MM or DD/MM/YYYY)
        date_parts = parts[0].split("/")
        if len(date_parts) < 2:
            parse_errors += 1
            continue

        try:
            day = int(date_parts[0])
            month = int(date_parts[1])
            yr = int(date_parts[2]) if len(date_parts) > 2 else year
        except (ValueError, IndexError):
            parse_errors += 1
            continue

        if day < 1 or day > 31 or month < 1 or month > 12:
            parse_errors += 1
            continue

        # Columns 2-9: four time/height pairs
        pairs = [
            {"time_str": parts[2], "ht_str": parts[3], "type": "HighWater"},
            {"time_str": parts[4], "ht_str": parts[5], "type": "LowWater"},
            {"time_str": parts[6], "ht_str": parts[7], "type": "HighWater"},
            {"time_str": parts[8], "ht_str": parts[9], "type": "LowWater"},
        ]

        for p in pairs:
            time_str = p["time_str"]
            ht_str = p["ht_str"]

            # Skip empty or placeholder entries
            if not time_str or not ht_str or time_str == "-" or ht_str == "-":
                continue

            time_parts = time_str.split(":")
            if len(time_parts) < 2:
                continue

            try:
                hr = int(time_parts[0])
                mn = int(time_parts[1])
                ht = float(ht_str)
            except ValueError:
                continue

            if hr > 23 or mn > 59:
                continue

            # Build UTC datetime from local time
            try:
                local_dt = datetime(yr, month, day, hr, mn, 0, tzinfo=local_tz)
            except ValueError:
                continue

            utc_dt = local_dt.astimezone(timezone.utc)
            adjusted_ht = ht

            # Secondary port correction: KHM data is for Portsmouth.
            # Langstone HW is ~9 min later. The previous 0.05m height
            # offset was removed after the 16-day calibration corpus
            # showed it introduced a +0.05m mean bias when propagated
            # through curve interpolation; see app/secondary_port.py for
            # the analysis. LW times/heights are effectively identical
            # between ports.
            #
            # The offset value comes from model_config.json via the
            # config accessor (with the secondary_port module-level
            # constant as fallback), keeping this file aligned with
            # secondary_port.apply_offset() without duplicating the
            # number locally.
            if p["type"] == "HighWater":
                hw_offset_minutes = get_secondary_port_offset(
                    "hw_time_offset_minutes", HW_TIME_OFFSET_MINUTES
                )
                utc_dt = utc_dt + timedelta(minutes=hw_offset_minutes)

            events.append({
                "timestamp": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "height_m": adjusted_ht,
                "event_type": p["type"],
                "is_approximate_time": False,
                "is_approximate_height": False,
            })

    if not events:
        logger.warning(f"Could not parse any events. {parse_errors} lines skipped.")
    else:
        days = len(set(e["timestamp"][:10] for e in events))
        logger.info(
            f"KHM: Parsed {len(events)} events over {days} days "
            f"(Langstone corrections applied). {parse_errors} lines skipped."
        )

    return events
