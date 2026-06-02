"""
Unit tests for the wind/shallow-water offset window logic in app.access_calc.

Focus: the compute_next_window_with_wind() merge tree (start-only offset,
no-access marker, and the always-accessible -> grounding transition) and the
worst-case-grounding trigger relationship that the scheduler relies on.

These functions are near-pure -- the only runtime dependency is the bundled
model_config.json, which app.config locates via APP_DIR regardless of the
current working directory -- so the tests run without the web stack:

    python -m pytest tests/
"""

from app.access_calc import compute_access_windows, compute_next_window_with_wind


def _events(hw_height: float = 4.5, lw_height: float = 0.5):
    """Two HWs and three bracketing LWs over ~24h at semidiurnal spacing."""
    return [
        {"timestamp": "2026-06-02T00:00:00Z", "height_m": lw_height, "event_type": "LowWater"},
        {"timestamp": "2026-06-02T06:12:00Z", "height_m": hw_height, "event_type": "HighWater"},
        {"timestamp": "2026-06-02T12:24:00Z", "height_m": lw_height, "event_type": "LowWater"},
        {"timestamp": "2026-06-02T18:36:00Z", "height_m": hw_height, "event_type": "HighWater"},
        {"timestamp": "2026-06-03T00:48:00Z", "height_m": lw_height, "event_type": "LowWater"},
    ]


def _first_hw(events, draught, drying, margin):
    """Return (hw_timestamp, baseline_window) for the first HW in the set.

    Taking the timestamp from the compute result (rather than the raw event)
    guarantees the string format matches what compute_next_window_with_wind
    compares against internally.
    """
    base = compute_access_windows(events, draught, drying, margin)
    return base[0]["hw_timestamp"], base[0]


def test_bounded_favourable_wind_leaves_window_unchanged():
    events = _events()  # base threshold 3.5, HW 4.5
    hw_ts, w_base = _first_hw(events, 1.0, 2.0, 0.5)
    res = compute_next_window_with_wind(events, 1.0, 2.0, 0.5, hw_ts, wind_offset_m=0.0)
    assert res["wind_adjusted"] is True
    assert not res.get("wind_no_access")
    assert res["start_time"] == w_base["start_time"]
    assert res["end_time"] == w_base["end_time"]


def test_bounded_adverse_wind_moves_start_only():
    events = _events()
    hw_ts, w_base = _first_hw(events, 1.0, 2.0, 0.5)
    res = compute_next_window_with_wind(events, 1.0, 2.0, 0.5, hw_ts, wind_offset_m=0.5)
    assert res["wind_adjusted"] is True
    assert not res.get("wind_no_access")
    # Higher start threshold -> refloat later; the ebb-side grounding (end) is
    # left at the baseline, because it is a separate sampling trigger.
    assert res["start_time"] > w_base["start_time"]
    assert res["end_time"] == w_base["end_time"]


def test_adverse_wind_can_remove_access_entirely():
    # HW 4.0 clears the base threshold (3.5) but not base+offset (4.5),
    # so under adverse wind there is no safe window -> no-access marker.
    events = _events(hw_height=4.0)
    hw_ts, w_base = _first_hw(events, 1.0, 2.0, 0.5)
    assert not w_base.get("below_threshold")  # a baseline window does exist
    res = compute_next_window_with_wind(events, 1.0, 2.0, 0.5, hw_ts, wind_offset_m=1.0)
    assert res["wind_no_access"] is True
    assert res["wind_adjusted"] is True
    assert res["start_time"] == res["end_time"] == hw_ts


def test_always_accessible_grounds_under_adverse_wind():
    # Deep mooring: base threshold -0.5, LW 0.5 -> always accessible at baseline.
    events = _events()
    hw_ts, w_base = _first_hw(events, 0.5, -1.0, 0.0)
    assert w_base.get("always_accessible") is True
    # offset 1.5 -> threshold 1.0 > LW 0.5 -> a grounding emerges this cycle.
    res = compute_next_window_with_wind(events, 0.5, -1.0, 0.0, hw_ts, wind_offset_m=1.5)
    assert res["wind_adjusted"] is True
    assert res.get("always_accessible") is False
    assert res["start_time"] != res["end_time"]
    assert not res.get("wind_no_access")


def test_always_accessible_stays_when_offset_small():
    events = _events()
    hw_ts, w_base = _first_hw(events, 0.5, -1.0, 0.0)
    assert w_base.get("always_accessible") is True
    # offset 0.3 -> threshold -0.2 still below LW 0.5 -> no grounding emerges.
    res = compute_next_window_with_wind(events, 0.5, -1.0, 0.0, hw_ts, wind_offset_m=0.3)
    assert res.get("always_accessible") is True
    assert res["wind_adjusted"] is True


def test_below_threshold_baseline_is_reported_unchanged():
    # HW 2.8 below the base threshold (3.5): no window regardless of wind.
    events = _events(hw_height=2.8)
    hw_ts, w_base = _first_hw(events, 1.0, 2.0, 0.5)
    assert w_base.get("below_threshold") is True
    res = compute_next_window_with_wind(events, 1.0, 2.0, 0.5, hw_ts, wind_offset_m=1.0)
    assert res.get("below_threshold") is True
    assert not res.get("wind_no_access")
    assert res["wind_adjusted"] is True


def test_worst_case_grounding_is_earlier_than_real_grounding():
    # The scheduler triggers at the worst-case grounding (margin = offset),
    # which on the ebb is a higher threshold and therefore crossed earlier than
    # the real keel-line grounding (margin = 0). Refloat is correspondingly later.
    events = _events()
    real = compute_access_windows(events, 1.0, 2.0, 0.0)[0]   # threshold 3.0
    worst = compute_access_windows(events, 1.0, 2.0, 0.5)[0]  # threshold 3.5
    assert worst["end_time"] < real["end_time"]
    assert worst["start_time"] > real["start_time"]
