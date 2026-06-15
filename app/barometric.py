"""
Barometric (inverse-barometer) correction of predicted tide height.

UKHO predictions (and the harmonic model, which is calibrated to UKHO) assume
*average* barometric pressure. Real sea level departs from that prediction with
the barometer: low pressure raises observed water above the prediction, high
pressure depresses it. This module adjusts predicted event heights for the
deviation of forecast pressure from a reference, before access-window
computation.

    correction_m = clamp( (P_ref - P_event) * k * scale , +/- max_correction_m )

  - P below reference (low barometer)  -> positive correction -> MORE water.
  - P above reference (high barometer)  -> negative correction -> LESS water.

This is the height-correction analogue of the secondary-port offset
(app/secondary_port.py): a pure events-list transform that returns a freshly
copied list with adjusted ``height_m`` and is applied *before*
``compute_access_windows``. Like that module it touches no stored rows, so the
harmonic-residual monitor and calibration scripts stay pressure-blind. The two
compose on orthogonal axes — this shifts event *heights*; the wind offset
shifts the *threshold*.

The correction is orthogonal to, and does not model, storm surge (wind-driven)
or the lag between a pressure change and the water response. See
docs/V2.9_BAROMETRIC_DESIGN.md for the full design, the coefficient provenance,
and the gating/staleness rationale.

Gating (system master ``barometric.enabled`` AND per-mooring opt-in AND a fresh
forecast) lives at the call sites; this function is unconditional and simply
passes an event through uncorrected when its pressure is missing or stale.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from dateutil import parser as dtparse

from app.config import (
    get_barometric_reference_hpa,
    get_barometric_coefficient_m_per_hpa,
    get_barometric_scale_factor,
    get_barometric_max_correction_m,
    get_barometric_forecast_staleness_hours,
)

logger = logging.getLogger(__name__)


# Reference defaults. The values actually used at runtime come from
# model_config.json (loaded via the app.config accessors). These constants are
# kept here as readable documentation and as a fallback if the JSON is missing
# or malformed. To change the model behaviour, edit the JSON; do not edit these.
#
# k = 0.0100 m/hPa: empirically fitted against the BODC Portsmouth gauge
# (wind-filtered 12-month regression ~= 0.0100), coinciding with the textbook
# inverse-barometer 1 cm/hPa. scale_factor stays 1.0 (no empirical tuning
# source; the effect is regional, so Portsmouth k == Langstone k). The 0.30 m
# clamp follows UKHO guidance that pressure-driven change seldom exceeds 0.3 m;
# exceeding it implies bad data or a storm surge this correction does not model.
# 36 h staleness tolerates one missed daily forecast fetch.
REFERENCE_HPA = 1013.25
COEFFICIENT_M_PER_HPA = 0.0100
SCALE_FACTOR = 1.0
MAX_CORRECTION_M = 0.30
FORECAST_STALENESS_HOURS = 36.0


# A pressure provider maps a target time to the forecast pressure at that time
# plus the age (hours) of the underlying forecast, or None when no usable
# forecast covers the time (beyond horizon / no data). The provider is pure
# data access; the staleness *policy* (comparison against
# forecast_staleness_hours) lives here, in apply_barometric_correction.
PressureProvider = Callable[[datetime], Optional[tuple[float, float]]]


def _clamp(value: float, limit: float) -> float:
    """Clamp ``value`` to the symmetric interval [-limit, +limit]."""
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


def apply_barometric_correction(
    events: list[dict],
    pressure_provider: PressureProvider,
) -> list[dict]:
    """
    Return a fresh copied list of ``events`` with ``height_m`` shifted by the
    barometric correction at each event's time.

    For each event the provider is sampled at the event's timestamp. When it
    returns a fresh pressure (forecast age within ``forecast_staleness_hours``)
    the height is shifted by ``clamp((P_ref - P) * k * scale, +/- max)``;
    otherwise — pressure missing, or the forecast too stale — the event passes
    through uncorrected. Event timestamps and types are never altered; only
    heights move.

    The input list and its event dicts are not mutated (each event is shallow
    copied), so this never touches stored rows. The correction value is uniform
    across moorings (one pressure series, one k); only the decision to apply it
    is gated, at the call sites.

    Resolves its constants via the cached config accessors once per call
    (each caches after first use, so the JSON is parsed at most once per
    process; subsequent calls are O(1) dict lookups).
    """
    p_ref = get_barometric_reference_hpa(REFERENCE_HPA)
    k = get_barometric_coefficient_m_per_hpa(COEFFICIENT_M_PER_HPA)
    scale = get_barometric_scale_factor(SCALE_FACTOR)
    max_corr = get_barometric_max_correction_m(MAX_CORRECTION_M)
    staleness_h = get_barometric_forecast_staleness_hours(FORECAST_STALENESS_HOURS)

    result = []

    for ev in events:
        new_ev = dict(ev)

        target_time = _event_time(ev)
        sample = pressure_provider(target_time) if target_time is not None else None

        if not sample:
            # No usable forecast covers this event time -> pass through.
            result.append(new_ev)
            continue

        pressure, age_hours = sample
        if age_hours is None or age_hours > staleness_h:
            # Forecast too stale (or age unknown) -> revert this event to baseline.
            result.append(new_ev)
            continue

        height = new_ev.get("height_m")
        if height is None:
            result.append(new_ev)
            continue

        raw = (p_ref - pressure) * k * scale
        corr = _clamp(raw, max_corr)
        if corr != raw:
            logger.warning(
                "Barometric correction clamped at +/-%.2f m: raw %.3f m "
                "(pressure %.1f hPa) implies bad data or storm surge.",
                max_corr, raw, pressure,
            )

        new_ev["height_m"] = round(height + corr, 2)
        result.append(new_ev)

    return result


def _event_time(ev: dict) -> Optional[datetime]:
    """
    Parse an event's timestamp to a datetime for pressure sampling, accepting
    either the stored UTC ISO-Z string or an already-parsed datetime. Returns
    None if the event carries no usable timestamp (then it passes through
    uncorrected).
    """
    return _parse_iso(ev.get("timestamp"))


def _parse_iso(value) -> Optional[datetime]:
    """
    Parse an ISO-Z string (or pass through a datetime) to a timezone-aware
    UTC datetime. Naive values are assumed UTC. Returns None on anything
    unparseable, so callers can degrade gracefully.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = dtparse.parse(value)
        except (ValueError, OverflowError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def make_pressure_provider(
    forecast_rows: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
) -> PressureProvider:
    """
    Build a pressure provider closing over the stored forecast steps.

    The returned callable ``provider(target_time)`` linearly interpolates
    forecast pressure between the two bracketing steps and reports the age
    (hours) of the forecast used — the ``(pressure_hpa, age_hours)`` pair that
    ``apply_barometric_correction`` expects. It returns None when target_time
    lies outside the stored forecast span (beyond the ~5-day horizon, or no
    data), so an event with no usable forecast reverts to baseline. The age
    is taken from the older of the two bracketing rows, the conservative
    choice for the staleness gate.

    The forecast is loaded and parsed once here, not per event, so a single
    correction pass over a feed's events does one DB read. Pass ``forecast_rows``
    explicitly (each ``{"target_time", "pressure_hpa", "fetched_at"}``) to
    build a provider without touching the database — used by tests. ``now``
    defaults to the current UTC time and is used only to compute forecast age.
    """
    if forecast_rows is None:
        from app.database import get_pressure_forecast
        forecast_rows = get_pressure_forecast()
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Parse to sorted (target_dt, pressure, fetched_dt) tuples once.
    parsed: list[tuple[datetime, float, Optional[datetime]]] = []
    for row in forecast_rows:
        target = _parse_iso(row.get("target_time"))
        pressure = row.get("pressure_hpa")
        if target is None or pressure is None:
            continue
        parsed.append((target, float(pressure), _parse_iso(row.get("fetched_at"))))
    parsed.sort(key=lambda r: r[0])

    def provider(target_time: datetime) -> Optional[tuple[float, float]]:
        if not parsed:
            return None
        tt = target_time if target_time.tzinfo else target_time.replace(tzinfo=timezone.utc)

        # Outside the stored forecast span -> no usable forecast.
        if tt < parsed[0][0] or tt > parsed[-1][0]:
            return None

        # Bracketing steps: lo = last step <= tt, hi = first step >= tt.
        lo = None
        hi = None
        for entry in parsed:
            if entry[0] <= tt:
                lo = entry
            if entry[0] >= tt:
                hi = entry
                break
        if lo is None or hi is None:
            return None

        lo_t, lo_p, lo_f = lo
        hi_t, hi_p, hi_f = hi
        if hi_t == lo_t:
            pressure = lo_p
        else:
            frac = (tt - lo_t).total_seconds() / (hi_t - lo_t).total_seconds()
            pressure = lo_p + (hi_p - lo_p) * frac

        # Age from the older bracketing fetch (largest age -> conservative
        # staleness). If neither row carries a fetch time, staleness can't be
        # judged, so report no usable forecast.
        fetches = [f for f in (lo_f, hi_f) if f is not None]
        if not fetches:
            return None
        age_hours = (now - min(fetches)).total_seconds() / 3600.0
        return (pressure, age_hours)

    return provider
