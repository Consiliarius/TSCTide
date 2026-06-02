"""
Unit tests for the cycle-based wind matching in app.observation_classifier.

After the wind-sampling rework, samples land at per-mooring worst-case
groundings rather than a fixed HW+4h, so the classifier matches an aground
observation to a wind sample within the *same tidal cycle*. These tests cover
that matching plus the unchanged guard clauses.
"""

from app.observation_classifier import classify_observation

HW_EVENTS = [
    {"timestamp": "2026-06-02T06:00:00Z", "event_type": "HighWater", "height_m": 4.5},
    {"timestamp": "2026-06-02T18:24:00Z", "event_type": "HighWater", "height_m": 4.5},
]

# Shallow side to the W -> wind from the E (and NE/SE) pushes the boat onto it.
MOORING = {"wind_offset_enabled": 1, "shallow_direction": "W"}


def _obs(timestamp, state="aground", lay="E"):
    return {"timestamp": timestamp, "state": state, "direction_of_lay": lay}


def test_aground_wind_toward_shallows_classifies_wind_offset():
    wind = [{"timestamp": "2026-06-02T09:00:00Z", "direction_compass": "E"}]
    res = classify_observation(_obs("2026-06-02T10:00:00Z", lay="E"), MOORING, HW_EVENTS, wind)
    assert res["classification"] == "wind_offset"
    assert res["wind_compass"] == "E"


def test_no_wind_sample_in_cycle_falls_back_to_base():
    # Sample sits in the *next* cycle (after the 18:24 HW), not this one.
    wind = [{"timestamp": "2026-06-02T19:00:00Z", "direction_compass": "E"}]
    res = classify_observation(_obs("2026-06-02T10:00:00Z", lay="E"), MOORING, HW_EVENTS, wind)
    assert res["classification"] == "base"
    assert "no wind sample" in res["reason"]


def test_favourable_wind_classifies_base():
    # Wind from the W blows the boat away from the shallow (W) side.
    wind = [{"timestamp": "2026-06-02T09:00:00Z", "direction_compass": "W"}]
    res = classify_observation(_obs("2026-06-02T10:00:00Z", lay="W"), MOORING, HW_EVENTS, wind)
    assert res["classification"] == "base"


def test_afloat_always_base():
    wind = [{"timestamp": "2026-06-02T09:00:00Z", "direction_compass": "E"}]
    res = classify_observation(
        _obs("2026-06-02T10:00:00Z", state="afloat", lay="E"), MOORING, HW_EVENTS, wind
    )
    assert res["classification"] == "base"


def test_lay_not_matching_wind_classifies_base():
    # Wind toward the shallows, but the bow is not aligned with it, so the
    # grounding is not attributable to wind-driven swing.
    wind = [{"timestamp": "2026-06-02T09:00:00Z", "direction_compass": "E"}]
    res = classify_observation(_obs("2026-06-02T10:00:00Z", lay="N"), MOORING, HW_EVENTS, wind)
    assert res["classification"] == "base"
