"""
Core access window calculation.

Given tide event data (HW/LW times and heights) and mooring configuration,
computes the time windows during which there is sufficient water depth
for the boat to be afloat.

Uses the Langstone asymmetric tidal curve to interpolate heights between
HW and LW events, then finds the threshold crossings.
"""

import math
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse
from typing import Optional

from app.config import load_model_config, to_utc_str, compute_cycle_number

logger = logging.getLogger(__name__)

# Module-level cache for model config tidal curve parameters.
# load_model_config() parses a JSON file on every call; at 3-minute
# interpolation intervals over a 7-hour window this is called thousands
# of times per calculation. The bundled config is read-only at runtime
# and effectively static for the lifetime of the process, so cache it
# on first access. The deliberate refresh trigger is a container
# restart, which reinitialises the process and discards the cache.
_cached_curve_params: Optional[dict] = None


def _get_curve_params() -> dict:
    """Return tidal curve parameters, loading from config on first call."""
    global _cached_curve_params
    if _cached_curve_params is None:
        cfg = load_model_config()
        _cached_curve_params = cfg.get("tidal_curve", {})
    return _cached_curve_params


def _interpolate_from_parsed(target: datetime, parsed_events: list[tuple]) -> Optional[float]:
    """
    Fast interpolation using pre-parsed, pre-sorted events.
    parsed_events: list of (datetime, height_m, event_type) tuples, sorted by time.
    """
    before = None
    after = None
    for dt, h, et in parsed_events:
        if dt <= target:
            before = (dt, h, et)
        if dt > target and after is None:
            after = (dt, h, et)
            break  # No need to continue once we have the bracket

    if before is None or after is None:
        return None

    return _curve_interpolate(target, before, after)


def interpolate_height_at_time(target_iso: str, events: list[dict]) -> Optional[float]:
    """
    Interpolate tide height at a specific time using surrounding HW/LW events
    and the Langstone asymmetric tidal curve.

    This is the public API used by calibration. For repeated calls with the
    same event set, use _interpolate_from_parsed with pre-parsed data.
    """
    target = dtparse.parse(target_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    parsed = []
    for ev in events:
        ts = ev["timestamp"]
        dt = dtparse.parse(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append((dt, ev["height_m"], ev["event_type"]))

    parsed.sort(key=lambda x: x[0])

    return _interpolate_from_parsed(target, parsed)


# --- Echo-sounder (depth sounding) calibration support (v2.10) ---
#
# A sounding is a two-sided point estimate of drying height, in contrast to
# the one-sided afloat/aground inequality. Only the raw measured depth is
# stored; the drying height is derived here, at query time, through the same
# height model (interpolate_height_at_time) the bound logic uses. Soundings
# therefore re-derive automatically if the harmonic model is recalibrated,
# with no stored value going stale.
#
# Pressure (v2.10.0): derivation is deliberately pressure-blind, matching the
# afloat/aground calibration path — app/barometric.py keeps the calibration
# corpus pressure-blind, and interpolate_height_at_time applies no barometric
# correction. A sounding taken under an extreme inverse-barometer anomaly
# therefore carries that anomaly into its derived drying height. Correcting
# the sounding instant for pressure is a documented follow-up; see
# docs/CALIBRATION_NOTES.md. The offset and soft-mud uncertainties below
# dominate at typical anomaly magnitudes, so this is acceptable for the
# initial release.

SOUNDER_DATUMS = ("waterline", "transducer", "keel")

# Coarse 1-sigma components (metres) for a sounding-derived drying height.
# These are engineering estimates, not measured constants; their job is to
# weight soundings relative to one another and to set interval width, not to
# assert an absolute accuracy. They are combined in quadrature.
_SOUNDING_SIGMA_INSTRUMENT = 0.10    # echo sounder + readout resolution
_SOUNDING_SIGMA_OFFSET = 0.10        # transducer offset / trim uncertainty
_SOUNDING_SIGMA_PREDICTION = 0.15    # inherited interpolate_height_at_time error
# Multiplier over soft / unknown bed, where the sounder may return off the
# fluid-mud surface and over-read depth (biasing derived drying LOW — the
# unsafe direction). Down-weights such soundings; the aground floor in
# calibrate_drying_height remains the hard guard, not this factor.
_SOUNDING_SIGMA_SOFT_BED_FACTOR = 2.0


def sounder_water_depth(measured_depth_m, sounder_datum,
                        transducer_offset_m, draught_m) -> Optional[float]:
    """
    Total water depth over the seabed at the sounding point, derived from the
    raw sounder reading and the datum it is referenced to:

      waterline  -> reading is already to the waterline; depth = measured
      transducer -> reading is below the transducer;     depth = measured + offset
      keel       -> reading is below the keel;           depth = measured + draught
                    (the keel sits ``draught`` below the waterline)

    The "keel" form uses the mooring's draught rather than a separate
    keel-to-transducer geometry, which the tool does not store. Returns None
    if the inputs are unusable.
    """
    if measured_depth_m is None:
        return None
    try:
        m = float(measured_depth_m)
    except (TypeError, ValueError):
        return None

    datum = (sounder_datum or "keel").strip().lower()
    if datum == "waterline":
        return m
    if datum == "keel":
        try:
            return m + float(draught_m)
        except (TypeError, ValueError):
            return None
    # "transducer": raw reading from the hull-mounted sensor.
    try:
        return m + float(transducer_offset_m or 0.0)
    except (TypeError, ValueError):
        return m


def sounding_sigma(bed_type: Optional[str] = None) -> float:
    """
    Combined 1-sigma uncertainty (metres) of a single sounding-derived drying
    height. Inflated over soft / unknown bed to down-weight likely over-reads.
    """
    base = math.sqrt(
        _SOUNDING_SIGMA_INSTRUMENT ** 2
        + _SOUNDING_SIGMA_OFFSET ** 2
        + _SOUNDING_SIGMA_PREDICTION ** 2
    )
    if (bed_type or "unknown").strip().lower() != "hard":
        base *= _SOUNDING_SIGMA_SOFT_BED_FACTOR
    return base


def _curve_interpolate(target: datetime, before: tuple, after: tuple) -> float:
    """
    Interpolate height using the Langstone asymmetric tidal curve.

    before/after: (datetime, height_m, event_type) tuples

    Note: flood_duration_fraction is present in model_config.json for
    documentation purposes but is not applied here. The actual event
    timestamps already encode the asymmetric flood/ebb duration, so
    applying an additional fraction correction would double-count it.
    """
    curve = _get_curve_params()

    t_before, h_before, et_before = before
    t_after, h_after, et_after = after

    total_seconds = (t_after - t_before).total_seconds()
    if total_seconds <= 0:
        return h_before

    elapsed = (target - t_before).total_seconds()
    fraction = elapsed / total_seconds

    # Determine if this is a flooding or ebbing phase
    if et_before == "LowWater" and et_after == "HighWater":
        # Flooding tide. Two regimes, selected by configuration:
        #
        #   1. LW stand then cosine (v2.5.3+, current production):
        #      Linear rise for the first flood_lw_stand_minutes after LW,
        #      lifting the water by flood_lw_stand_rise_fraction of the
        #      flood range, then a half-cosine from that level up to HW.
        #      Models the documented Solent young-flood-stand effect:
        #      early-flood inflows arriving around the Isle of Wight are
        #      out of phase with the main flood and briefly pause the
        #      rise just after LW.
        #
        #   2. Pure half-cosine (pre-v2.5.3 fallback): if either
        #      flood_lw_stand_minutes or flood_lw_stand_rise_fraction is
        #      zero or absent, the rise from LW to HW follows a standard
        #      half-cosine smoothstep. This was production behaviour up
        #      to v2.5.2 and had a residual +0.13m mid-flood mean bias
        #      against the 16-day UKHO corpus.
        #
        # Parameter semantics: rise_fraction is the fraction of the
        # flood RANGE that the water lifts during the stand, NOT the
        # fraction of LW absolute height. The ebb stand below uses a
        # fraction of HW absolute height (historical accident); the two
        # forms differ because LW heights in Langstone are small (0.5-
        # 1.5m typical) and a fraction of LW would be physically
        # negligible, whereas HW heights are large enough that fraction-
        # of-HW is meaningful.
        #
        # An earlier attempt added a *pre-HW* stand symmetric to the
        # ebb stand. It was rolled back: the corpus showed it more than
        # doubled flood RMS by double-counting the cosine's own natural
        # slowdown near its peak. The LW-stand form here acts at the
        # opposite end of the flood, where the cosine's natural rise is
        # fastest and the empirical data shows a real depression.
        #
        # Tuned 30 April 2026 against the 16-day UKHO corpus via
        # scripts/sweep_flood_curve.py: 60min / rise_fraction=0.08 gives
        # flood mean +0.016m (was +0.134m), RMS 0.219m (was 0.290m).
        lw_stand_mins = curve.get("flood_lw_stand_minutes", 0)
        lw_stand_frac = curve.get("flood_lw_stand_rise_fraction", 0)

        if lw_stand_mins > 0 and lw_stand_frac > 0 and total_seconds > 0:
            # Cap stand at 50% of flood span. Combinations beyond this
            # are unphysical (no room for the post-stand cosine to reach
            # HW); the cap is a defensive bound for misconfiguration.
            stand_proportion = min((lw_stand_mins * 60) / total_seconds, 0.5)
            range_m = h_after - h_before
            stand_top = h_before + range_m * lw_stand_frac

            if fraction < stand_proportion:
                # Linear rise during the young-flood stand.
                return h_before + (stand_top - h_before) * (fraction / stand_proportion)

            # Cosine from the top of the stand up to HW.
            adjusted = (fraction - stand_proportion) / (1 - stand_proportion)
            return _cosine_interp(adjusted, stand_top, h_after)

        # Pure cosine fallback (legacy, pre-v2.5.3 behaviour).
        return _cosine_interp(fraction, h_before, h_after)
    elif et_before == "HighWater" and et_after == "LowWater":
        # Ebbing tide - apply asymmetry via stand effect
        stand_mins = curve.get("stand_duration_minutes", 30)
        stand_frac = curve.get("stand_height_fraction", 0.95)

        if total_seconds > 0:
            stand_proportion = (stand_mins * 60) / total_seconds
        else:
            stand_proportion = 0

        if fraction < stand_proportion:
            # During the stand - height stays near HW
            stand_drop = h_before * (1 - stand_frac)
            return h_before - stand_drop * (fraction / stand_proportion)
        else:
            # After stand - cosine ebb from stand level to LW
            adjusted_fraction = (fraction - stand_proportion) / (1 - stand_proportion)
            stand_height = h_before * stand_frac
            return _cosine_interp(1 - adjusted_fraction, h_after, stand_height)
    else:
        # Same event types (shouldn't happen with clean data) - linear fallback
        return h_before + (h_after - h_before) * fraction


def _cosine_interp(fraction: float, h_low: float, h_high: float) -> float:
    """Cosine interpolation between low and high values. fraction 0 to 1 maps low to high."""
    fraction = max(0.0, min(1.0, fraction))
    # Cosine gives smooth curve: 0 at fraction=0, 1 at fraction=1
    t = (1 - math.cos(math.pi * fraction)) / 2.0
    return h_low + (h_high - h_low) * t


def compute_access_windows(
    events: list[dict],
    draught_m: float,
    drying_height_m: float,
    safety_margin_m: float,
    source: str = "ukho",
    interval_minutes: int = 3,
) -> list[dict]:
    """
    Compute baseline access windows from tide events.

    An access window is the continuous period around each HW during which:
        tide_height > drying_height + draught + safety_margin

    This computes the baseline only. The wind/shallow-water offset is applied
    separately and start-only by compute_next_window_with_wind (driven by the
    scheduler at each worst-case grounding); it is deliberately not a parameter
    here, so ``wind_adjusted`` is always False on these windows.

    Returns list of window dicts:
        hw_timestamp, hw_height_m, start_time, end_time, duration_minutes,
        source, wind_adjusted, below_threshold, incomplete_data,
        always_accessible

    Window states (mutually exclusive):
      - below_threshold=True: HW itself doesn't clear the threshold, no window
      - always_accessible=True: tide never drops below the threshold across
        the tidal cycle (both bracketing LWs are above threshold). Happens
        with low or negative drying heights or high-neap LWs.
      - incomplete_data=True: couldn't find one or both threshold crossings
        AND couldn't confirm "always accessible" (bracketing LWs missing from
        the event list, typically at edges of the data window).
      - otherwise a normal window with start_time / end_time populated.
    """
    base_threshold = drying_height_m + draught_m + safety_margin_m

    # Parse and sort events
    parsed = []
    for ev in events:
        ts = ev["timestamp"]
        dt = dtparse.parse(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append({
            "dt": dt,
            "height_m": ev["height_m"],
            "event_type": ev["event_type"],
        })
    parsed.sort(key=lambda x: x["dt"])

    if len(parsed) < 2:
        return []

    # Identify high waters
    high_waters = [p for p in parsed if p["event_type"] == "HighWater"]

    windows = []
    for hw in high_waters:
        hw_dt = hw["dt"]
        hw_height = hw["height_m"]
        hw_ts_str = to_utc_str(hw_dt)
        threshold = base_threshold

        # If HW height is below threshold, no access window
        if hw_height < threshold:
            windows.append({
                "hw_timestamp": hw_ts_str,
                "hw_height_m": hw_height,
                "start_time": None,
                "end_time": None,
                "duration_minutes": 0,
                "source": source,
                "below_threshold": True,
                "wind_adjusted": False,
            })
            continue

        # Search outward from HW to find threshold crossings
        # Search backward (before HW)
        start_time = _find_crossing(
            hw_dt, parsed, threshold, direction="backward",
            max_hours=7, interval_minutes=interval_minutes
        )
        # Search forward (after HW)
        end_time = _find_crossing(
            hw_dt, parsed, threshold, direction="forward",
            max_hours=7, interval_minutes=interval_minutes
        )

        # Distinguish "always accessible" from "incomplete data":
        #   - Always accessible: the bracketing LWs on either side of HW are
        #     themselves above the threshold, so the tide never drops low
        #     enough to ground the boat. Happens with low or negative drying
        #     heights, or on shallow neaps where LW is unusually high.
        #   - Incomplete data: one or both bracketing LWs are missing from
        #     the event list (edge of data window, typical for first/last HW).
        if not (start_time and end_time):
            # Find bracketing LWs within 8 hours either side of HW
            lw_before = next(
                (p for p in reversed(parsed)
                 if p["event_type"] == "LowWater"
                 and p["dt"] < hw_dt
                 and (hw_dt - p["dt"]).total_seconds() <= 8 * 3600),
                None,
            )
            lw_after = next(
                (p for p in parsed
                 if p["event_type"] == "LowWater"
                 and p["dt"] > hw_dt
                 and (p["dt"] - hw_dt).total_seconds() <= 8 * 3600),
                None,
            )
            both_lws_above = (
                lw_before is not None
                and lw_after is not None
                and lw_before["height_m"] >= threshold
                and lw_after["height_m"] >= threshold
            )

            if both_lws_above:
                # Always accessible across this tidal cycle. Report the span
                # from the trough of one side to the trough of the other so
                # downstream code has something sensible to work with if it
                # needs a duration; the UI should show this as a distinct
                # state rather than a numeric window.
                windows.append({
                    "hw_timestamp": hw_ts_str,
                    "hw_height_m": hw_height,
                    "start_time": to_utc_str(lw_before["dt"]),
                    "end_time": to_utc_str(lw_after["dt"]),
                    "duration_minutes": round(
                        (lw_after["dt"] - lw_before["dt"]).total_seconds() / 60
                    ),
                    "source": source,
                    "below_threshold": False,
                    "incomplete_data": False,
                    "always_accessible": True,
                    "wind_adjusted": False,
                })
                continue
            # else: fall through to the incomplete-data window below

        if start_time and end_time:
            duration = (end_time - start_time).total_seconds() / 60.0
        else:
            duration = 0

        windows.append({
            "hw_timestamp": hw_ts_str,
            "hw_height_m": hw_height,
            "start_time": to_utc_str(start_time) if start_time else None,
            "end_time": to_utc_str(end_time) if end_time else None,
            "duration_minutes": round(duration),
            "source": source,
            "below_threshold": False,
            "incomplete_data": not (start_time and end_time),
            "always_accessible": False,
            "wind_adjusted": False,
        })

    return windows


def _window_for_hw(windows: list[dict], hw_timestamp: str) -> Optional[dict]:
    """Pick the window whose HW matches hw_timestamp from a compute result."""
    for w in windows:
        if w["hw_timestamp"] == hw_timestamp:
            return w
    return None


def _is_bounded(w: Optional[dict]) -> bool:
    """True if w is a normal bounded window with a real start and end."""
    return bool(
        w is not None
        and not w.get("below_threshold")
        and not w.get("always_accessible")
        and not w.get("incomplete_data")
        and w.get("start_time")
        and w.get("end_time")
    )


def compute_next_window_with_wind(
    events: list[dict],
    draught_m: float,
    drying_height_m: float,
    safety_margin_m: float,
    next_hw_timestamp: str,
    wind_offset_m: float,
    source: str = "ukho",
    interval_minutes: int = 3,
) -> Optional[dict]:
    """
    Compute the single access window for the HW at ``next_hw_timestamp``,
    applying the wind/shallow-water offset to the window's START only.

    ``wind_offset_m`` is the extra drying height to require while the wind is
    pushing the boat toward its shallow side (0 when the wind is favourable).
    It is applied only to the flood-side (start) crossing of the next window;
    the ebb-side (end / grounding) keeps the baseline threshold, because the
    grounding is a deterministic sampling trigger that gets its own fresh wind
    reading next cycle.

    Why this lives outside ``compute_access_windows``: the offset is asymmetric
    (start only) and the always-accessible/no-access transitions need both a
    baseline and an offset evaluation. Rather than thread that through the core
    threshold logic, this helper calls ``compute_access_windows`` twice -- at
    ``safety_margin_m`` and at ``safety_margin_m + wind_offset_m`` -- and merges
    the two results. The core function is left untouched.

    Returns one window dict (same shape as ``compute_access_windows`` entries,
    plus a ``wind_no_access`` flag for the wind-induced no-access marker), or
    ``None`` if ``next_hw_timestamp`` is not among ``events``.

    The result always carries ``wind_adjusted=True`` (this HW *was* wind-
    evaluated) regardless of whether the offset ended up being applied, so the
    feed can distinguish "checked, favourable" from "not checked".

    Merge logic (baseline state vs state at base+offset):
      baseline below_threshold          -> below_threshold (offset irrelevant)
      baseline bounded, Delta-start ok  -> start = Delta-start, end = baseline end
      baseline bounded, Delta-start gone-> wind_no_access marker (start=end=HW)
      baseline always_accessible:
          still always at base+Delta    -> always_accessible
          grounds at base+Delta         -> full base+Delta window (start + end)
    """
    base_windows = compute_access_windows(
        events, draught_m, drying_height_m, safety_margin_m,
        source=source, interval_minutes=interval_minutes,
    )
    w_base = _window_for_hw(base_windows, next_hw_timestamp)
    if w_base is None:
        return None

    result = dict(w_base)
    result["wind_adjusted"] = True

    # Favourable wind (or no offset configured): baseline window stands.
    if wind_offset_m <= 0:
        return result

    # No-access even at baseline: the tide is unsafe regardless of wind, and
    # the offset can only make it worse. Report the baseline state unchanged.
    if w_base.get("below_threshold"):
        return result

    delta_windows = compute_access_windows(
        events, draught_m, drying_height_m, safety_margin_m + wind_offset_m,
        source=source, interval_minutes=interval_minutes,
    )
    w_delta = _window_for_hw(delta_windows, next_hw_timestamp)

    if w_base.get("always_accessible"):
        # Baseline never grounds this cycle, but the offset may push the trough
        # below the keel and create a grounding -- surface the emergent window
        # (start AND end). If it still never grounds, stay always-accessible.
        if _is_bounded(w_delta):
            result["always_accessible"] = False
            result["start_time"] = w_delta["start_time"]
            result["end_time"] = w_delta["end_time"]
            result["duration_minutes"] = w_delta["duration_minutes"]
        return result

    # Baseline is a normal bounded window: apply the offset to the START only.
    if _is_bounded(w_delta):
        start_delta = dtparse.parse(w_delta["start_time"])
        end_base = dtparse.parse(result["end_time"])
        result["start_time"] = w_delta["start_time"]
        result["duration_minutes"] = round(
            (end_base - start_delta).total_seconds() / 60
        )
        return result

    # The offset lifts the safe threshold above even HW: the boat still floats
    # off (it grounds at the lower baseline keel line, which is why the chain
    # still triggers), but there is no safe-access window this tide. Emit a
    # zero-duration marker so the feed shows *why* the window vanished.
    result["start_time"] = next_hw_timestamp
    result["end_time"] = next_hw_timestamp
    result["duration_minutes"] = 0
    result["wind_no_access"] = True
    return result


def _find_crossing(
    hw_dt: datetime,
    events: list[dict],
    threshold: float,
    direction: str,
    max_hours: float = 7,
    interval_minutes: int = 3,
) -> Optional[datetime]:
    """
    Find the time at which tide height crosses the threshold,
    searching outward from HW in the given direction.
    Uses pre-parsed event tuples for efficient repeated interpolation.
    """
    step = timedelta(minutes=interval_minutes)
    if direction == "backward":
        step = -step

    max_delta = timedelta(hours=max_hours)
    current = hw_dt

    # Pre-parse events once for fast interpolation
    parsed_tuples = [
        (e["dt"], e["height_m"], e["event_type"]) for e in events
    ]

    prev_height = None
    while abs(current - hw_dt) < max_delta:
        height = _interpolate_from_parsed(current, parsed_tuples)
        if height is None:
            break

        if prev_height is not None:
            if (direction == "backward" and height < threshold <= prev_height) or \
               (direction == "forward" and prev_height >= threshold > height):
                # Crossing found between current and previous step
                # Linear interpolation for precise crossing time
                if abs(prev_height - height) > 0.001:
                    frac = (threshold - height) / (prev_height - height)
                    crossing = current - step * frac
                    return crossing
                return current

        prev_height = height
        current += step

    # No crossing found - insufficient data or HW below threshold for full range
    return None


def generate_event_uid(mooring_id: int, hw_timestamp: str) -> str:
    """
    Generate a deterministic event UID for a given mooring and HW time.

    Uses a tidal cycle number rather than exact HW minutes, so the same
    physical tide gets the same UID regardless of which data source
    predicted it (harmonic +/- 30min vs UKHO). The average tidal cycle is
    12.42 hours; dividing hours-since-epoch by this and rounding gives
    a cycle ID that's stable for shifts of up to +/- 3 hours.

    Cycle epoch and length come from the shared helper in app.config
    (compute_cycle_number) so this UID matches the cycle_number column
    in harmonic_predictions and the UID generated by
    ical_manager._tide_event_uid for the same tide.
    """
    cycle_number = compute_cycle_number(hw_timestamp)
    return f"tidal-access-m{mooring_id:03d}-c{cycle_number:05d}@langstone"
