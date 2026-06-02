"""
Integration test for ensure_wind_jobs_scheduled() against a temporary SQLite
database. Exercises the highest-risk new scheduler code end-to-end: enumerating
each mooring's future worst-case groundings from real stored tide data and
scheduling one job per grounding pointed at the *next* HW.

DB_PATH is a module global in app.database, so we monkeypatch it to a temp file
(no env vars, no real data/ directory needed).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import to_utc_str


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.database as database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database


def _seed_tides(start: datetime, count: int = 16):
    """Synthetic semidiurnal events: LW(0.5)/HW(4.5) alternating ~6h13m apart."""
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


def test_ensure_schedules_groundings_and_skips_always_accessible(db, monkeypatch):
    import app.scheduler as sched

    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.store_tide_events(_seed_tides(now - timedelta(hours=12), 16),
                         source="ukho", station="langstone")

    # Drying mooring: worst-case threshold = 2.0 + 1.0 + 0.5 = 3.5 < HW 4.5,
    # so the boat grounds every tide -> a job per future grounding.
    db.save_mooring({
        "mooring_id": 1, "boat_name": "Test", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 1, "shallow_direction": "W",
        "shallow_extra_depth_m": 0.5, "calendar_enabled": 1,
    })
    # Deep mooring: worst-case threshold = -2.0 + 0.3 + 0.5 = -1.2 < LW 0.5,
    # so it never grounds even in the worst case -> no jobs.
    db.save_mooring({
        "mooring_id": 2, "boat_name": "Deep", "draught_m": 0.3,
        "drying_height_m": -2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 1, "shallow_direction": "W",
        "shallow_extra_depth_m": 0.5, "calendar_enabled": 1,
    })
    # Wind disabled: must be ignored entirely.
    db.save_mooring({
        "mooring_id": 3, "boat_name": "NoWind", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 0, "shallow_direction": "",
        "shallow_extra_depth_m": 0.0, "calendar_enabled": 1,
    })

    try:
        scheduled = sched.ensure_wind_jobs_scheduled()

        by_mooring = {1: [], 2: [], 3: []}
        for s in scheduled:
            by_mooring[s["mooring_id"]].append(s)

        assert len(by_mooring[1]) >= 2, "drying mooring should have grounding jobs"
        assert by_mooring[2] == [], "always-accessible mooring must be skipped"
        assert by_mooring[3] == [], "wind-disabled mooring must be skipped"

        now_str = to_utc_str(now)
        for s in by_mooring[1]:
            assert s["grounding"] > now_str          # only future groundings
            assert s["next_hw"] > s["grounding"]      # adjusts the NEXT HW

        # The jobs actually landed in the scheduler under the per-mooring id.
        landed = [j.id for j in sched.scheduler.get_jobs()
                  if (j.id or "").startswith("wind_sample_m1_")]
        assert len(landed) == len(by_mooring[1])
    finally:
        sched._purge_wind_jobs()


def test_wind_no_access_marker_is_stored_with_title(db):
    """The wind-induced no-access marker persists as a zero-duration event with
    the 'wind-blown to shallows' title (it passes store_windows_as_events'
    existing guards because start_time == end_time are both truthy)."""
    from app.ical_manager import store_windows_as_events

    db.save_mooring({
        "mooring_id": 5, "boat_name": "Marker", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 1, "shallow_direction": "W",
        "shallow_extra_depth_m": 0.5, "calendar_enabled": 1,
    })

    hw = "2026-06-02T18:36:00Z"
    marker = {
        "hw_timestamp": hw, "hw_height_m": 3.9,
        "start_time": hw, "end_time": hw, "duration_minutes": 0,
        "source": "ukho", "below_threshold": False,
        "always_accessible": False, "wind_no_access": True,
        "wind_adjusted": True,
    }
    store_windows_as_events(
        [marker], 5, "ukho", "Marker",
        calc_params={"draught_m": 1.0, "drying_height_m": 2.0,
                     "safety_margin_m": 0.3, "obs_calibrated": 0},
        wind_details={"direction": "E", "speed_ms": 10.0, "offset_m": 0.5},
    )

    events = db.get_calendar_events(5)
    assert len(events) == 1
    ev = events[0]
    assert ev["start_time"] == ev["end_time"] == hw
    assert "No access (wind-blown to shallows)" in ev["title"]
    assert ev["wind_adjusted"] == 1
