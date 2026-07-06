"""
Tests that observation calibration corrects the interpolated tide height to the
measured barometric pressure frozen on each observation (v2.11).

The mooring's drying height is a static seabed level, so an observation made
under anomalous pressure must be reconciled against the ACTUAL water level, not
the average-pressure prediction. These tests assert the *delta* between the
pressure-corrected and pressure-blind results equals the inverse-barometer
correction, which is robust to the tidal-curve internals.

The functions are driven via `_preloaded` (mooring, observations, tide_events,
wind_obs, classifications) so no DB or classifier round-trip is needed; the
barometric master is monkeypatched on/off. With the model config unloaded, the
correction uses barometric.py's fallbacks (k=0.0100 m/hPa, ref=1013.25 hPa,
scale=1.0, clamp 0.30 m), so 993.25 hPa -> +0.20 m and 1033.25 hPa -> -0.20 m.
"""

import pytest

from datetime import datetime, timedelta, timezone

from app.config import to_utc_str
from app.database import calibrate_drying_height, calibrate_wind_offset


def _seed_tides(start, count=8):
    """Synthetic semidiurnal LW(0.5)/HW(4.5) events ~6h13m apart."""
    types = ["LowWater", "HighWater"]
    heights = [0.5, 4.5]
    out = []
    t = start
    for i in range(count):
        out.append({
            "timestamp": to_utc_str(t),
            "height_m": heights[i % 2],
            "event_type": types[i % 2],
        })
        t += timedelta(hours=6, minutes=13)
    return out


BASE = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
TIDES = _seed_tides(BASE - timedelta(hours=6), 10)
# First LW at BASE-6h, first HW at ~BASE+13m, next LW at ~BASE+6h26m.
FLOOD_TS = to_utc_str(BASE + timedelta(hours=2))          # off the HW, mid-tide
NEAR_HW_TS = to_utc_str(BASE + timedelta(minutes=30))     # ~17 min after the HW

MOORING = {"draught_m": 1.0, "drying_height_m": 2.0, "sounder_datum": "keel"}


def _preloaded(obs, classification):
    cls = [{"observation": obs, "classification": classification,
            "reason": "", "hw_timestamp": None, "wind_compass": None}]
    return (MOORING, [obs], TIDES, [], cls)


def _set_master(monkeypatch, enabled):
    monkeypatch.setattr("app.config.get_barometric_enabled", lambda default: enabled)


def test_low_pressure_raises_afloat_upper_bound(monkeypatch):
    obs = {"timestamp": FLOOD_TS, "state": "afloat", "obs_type": "binary",
           "pressure_hpa": 993.25}  # 20 hPa low -> +0.20 m

    _set_master(monkeypatch, False)
    off = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))

    _set_master(monkeypatch, True)
    on = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))

    assert off["pressure_corrected_count"] == 0
    assert on["pressure_corrected_count"] == 1
    assert on["pressure_correction_min_m"] == 0.20
    assert on["pressure_correction_max_m"] == 0.20
    # Upper bound = height - draught; +0.20 m of water -> +0.20 m higher bound.
    assert on["upper_bound"] - off["upper_bound"] == pytest.approx(0.20, abs=1e-9)


def test_high_pressure_lowers_afloat_upper_bound(monkeypatch):
    obs = {"timestamp": FLOOD_TS, "state": "afloat", "obs_type": "binary",
           "pressure_hpa": 1033.25}  # 20 hPa high -> -0.20 m

    _set_master(monkeypatch, False)
    off = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))
    _set_master(monkeypatch, True)
    on = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))

    assert on["pressure_correction_min_m"] == -0.20
    assert on["upper_bound"] - off["upper_bound"] == pytest.approx(-0.20, abs=1e-9)


def test_null_pressure_is_uncorrected_even_with_master_on(monkeypatch):
    obs = {"timestamp": FLOOD_TS, "state": "afloat", "obs_type": "binary",
           "pressure_hpa": None}

    _set_master(monkeypatch, True)
    on = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))
    _set_master(monkeypatch, False)
    off = calibrate_drying_height(0, _preloaded=_preloaded(obs, "base"))

    assert on["pressure_corrected_count"] == 0
    assert on["upper_bound"] == off["upper_bound"]


def test_wind_offset_suggestion_shifts_with_pressure(monkeypatch):
    # Aground near HW so implied offset (height - draught - base_drying) > 0.
    obs = {"timestamp": NEAR_HW_TS, "state": "aground", "obs_type": "binary",
           "pressure_hpa": 993.25, "direction_of_lay": "E"}

    _set_master(monkeypatch, False)
    off = calibrate_wind_offset(0, _preloaded=_preloaded(obs, "wind_offset"))
    _set_master(monkeypatch, True)
    on = calibrate_wind_offset(0, _preloaded=_preloaded(obs, "wind_offset"))

    assert off["suggested_offset_m"] is not None
    assert on["pressure_corrected_count"] == 1
    # +0.20 m of water at the grounding -> +0.20 m required offset.
    assert on["suggested_offset_m"] - off["suggested_offset_m"] == pytest.approx(0.20, abs=1e-9)


def test_base_and_offset_use_the_same_corrected_height(monkeypatch):
    # A wind-offset-classified aground obs whose implied offset is non-positive
    # falls through to base drying; whichever pool it lands in, the corrected
    # height must be identical. Here we just confirm both functions apply the
    # same +0.20 m so the two calibrations stay consistent.
    obs = {"timestamp": NEAR_HW_TS, "state": "aground", "obs_type": "binary",
           "pressure_hpa": 993.25, "direction_of_lay": "E"}
    _set_master(monkeypatch, True)

    dry = calibrate_drying_height(0, _preloaded=_preloaded(obs, "wind_offset"))
    off = calibrate_wind_offset(0, _preloaded=_preloaded(obs, "wind_offset"))
    assert dry["pressure_correction_max_m"] == off["pressure_correction_max_m"] == 0.20
