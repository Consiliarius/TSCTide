"""Spring / Neap / Mid tide classification (v2.8).

Classifies a given local date as 'spring', 'mid', or 'neap' by comparing
its highest predicted High Water against percentile thresholds derived
from a rolling 90-day window of UKHO HW heights ending at that date.

Adaptive per-location: thresholds are computed from local data rather
than hard-coded so the classifier auto-tunes as the dataset grows and
across seasonal drift in the tidal range.

The classifier intentionally degrades gracefully when there is too little
stored data to be honest: under MIN_SAMPLE_SIZE HW samples, it returns
None and the UI suppresses the indicator rather than guessing.
"""

import logging
from datetime import datetime, timedelta, timezone, date as date_cls
from typing import Optional

import pytz

from app.config import DEFAULT_TIMEZONE, to_utc_str
from app.database import get_ukho_tide_events

logger = logging.getLogger(__name__)

# Rolling-window size in days. 90 days spans roughly six spring/neap
# cycles, giving enough samples for stable percentiles without including
# data so old that seasonal range drift skews the percentile boundaries.
ROLLING_WINDOW_DAYS = 90

# Below this many HW samples in the window, we don't classify - the
# percentile estimate is too noisy to be meaningful. ~14 HW events
# corresponds to about a week of UKHO data; less than that and we don't
# yet span a full spring/neap cycle.
MIN_SAMPLE_SIZE = 14

# Percentile thresholds. Today's max-HW above p_upper = "spring"; below
# p_lower = "neap"; in between = "mid". 30/70 gives a comfortable middle
# band rather than over-classifying every day as one or the other.
PERCENTILE_LOWER = 30
PERCENTILE_UPPER = 70


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy.percentile default)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def classify_spring_neap(local_date: Optional[date_cls] = None) -> Optional[str]:
    """
    Classify a local date as 'spring', 'mid', or 'neap', or None when
    there isn't enough UKHO history to classify honestly.

    local_date: the date to classify in DEFAULT_TIMEZONE. Defaults to today.

    Approach:
      1. Find all HW events in [local_date - 90 days, local_date] UTC range.
      2. Find the HW events on local_date itself; pick the highest height.
      3. Compute p30 and p70 of all HW heights in the window.
      4. Classify: today_max >= p70 -> spring; <= p30 -> neap; else mid.

    If today has no stored HW events, the function still classifies based
    on the window (the caller decides whether to show the indicator at all).
    """
    try:
        tz = pytz.timezone(DEFAULT_TIMEZONE)
        if local_date is None:
            local_date = datetime.now(tz).date()

        local_midnight = tz.localize(datetime.combine(local_date, datetime.min.time()))
        local_end = local_midnight + timedelta(days=1)
        day_start_utc = local_midnight.astimezone(timezone.utc)
        day_end_utc = local_end.astimezone(timezone.utc)

        window_start_utc = day_start_utc - timedelta(days=ROLLING_WINDOW_DAYS)
        window_end_utc = day_end_utc

        events = get_ukho_tide_events(
            to_utc_str(window_start_utc), to_utc_str(window_end_utc)
        )

        hw_heights = [e["height_m"] for e in events if e.get("event_type") == "HighWater"]
        if len(hw_heights) < MIN_SAMPLE_SIZE:
            return None

        # Pick today's highest HW. The two daily HWs differ in height
        # (diurnal inequality); classifying on the larger value gives the
        # cleanest spring/neap signal.
        today_hw_heights = []
        from dateutil import parser as dtparse
        for e in events:
            if e.get("event_type") != "HighWater":
                continue
            dt = dtparse.parse(e["timestamp"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if day_start_utc <= dt < day_end_utc:
                today_hw_heights.append(e["height_m"])
        if not today_hw_heights:
            # No HW on the requested date itself - either pre-dawn LW only
            # or data gap. Without a "today" HW we cannot classify.
            return None
        today_max = max(today_hw_heights)

        sorted_heights = sorted(hw_heights)
        p_lower = _percentile(sorted_heights, PERCENTILE_LOWER)
        p_upper = _percentile(sorted_heights, PERCENTILE_UPPER)

        if today_max >= p_upper:
            return "spring"
        if today_max <= p_lower:
            return "neap"
        return "mid"
    except Exception as e:
        # Never fail the conditions or tide-curve endpoint just because
        # classification could not complete - log and return None so the
        # UI silently omits the indicator.
        logger.warning(f"Spring/neap classification failed: {e}")
        return None
