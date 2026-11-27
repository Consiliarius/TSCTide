"""
Regression pins for wind_observation_job (v2.11.1, stage A-0).

These tests record the exact rows the wind grounding job wrote BEFORE the
v2.11.1 refactor moved its window computation into a shared helper
(compute_wind_window). They must keep passing unchanged afterwards: the
refactor is meant to move code, not change what the job writes.

Fixed dates are used (March 2027) so the pinned timestamps are deterministic.
The network is stubbed: OWM wind via app.wind.fetch_current_wind, and the
barometric pressure provider via app.barometric.make_pressure_provider.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import to_utc_str


T0 = datetime(2027, 3, 1, 0, 0, tzinfo=timezone.utc)


def _tides(start: datetime, count: int = 12) -> list[dict]:
    """Synthetic semidiurnal events: LW 0.5 m / HW 4.5 m alternating 6h13m apart."""
    out = []
    t = start
    for i in range(count):
        out.append({
            "timestamp": to_utc_str(t),
            "height_m": [0.5, 4.5][i % 2],
            "event_type": ["LowWater", "HighWater"][i % 2],
        })
        t += timedelta(hours=6, minutes=13)
    return out


EVENTS = _tides(T0)
NEXT_HW = [e["timestamp"] for e in EVENTS if e["event_type"] == "HighWater"][2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import app.config as config
    import app.database as database
    import app.ical_manager as ical

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FEEDS_DIR", tmp_path / "feeds")
    monkeypatch.setattr(ical, "FEEDS_DIR", tmp_path / "feeds")
    database.init_db()
    database.store_tide_events(EVENTS, source="ukho", station="langstone")
    return database


def _mooring(database, **overrides):
    cfg = {
        "mooring_id": 1, "boat_name": "Pin", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 1, "shallow_direction": "W",
        "shallow_extra_depth_m": 0.5, "calendar_enabled": 1,
    }
    cfg.update(overrides)
    database.save_mooring(cfg)


def _stub_wind(monkeypatch, compass):
    import app.wind as wind

    deg = {"E": 90.0, "W": 270.0}[compass]

    async def fake_wind():
        return {
            "timestamp": to_utc_str(datetime.now(timezone.utc)),
            "direction_deg": deg,
            "direction_compass": compass,
            "speed_ms": 10.0,
        }

    monkeypatch.setattr(wind, "fetch_current_wind", fake_wind)


def _run_job():
    import app.scheduler as sched
    asyncio.run(sched.wind_observation_job(1, NEXT_HW))


def _only_row(database):
    rows = database.get_calendar_events(1)
    assert len(rows) == 1
    return rows[0]


def test_offset_applied_moves_start_only(env, monkeypatch):
    _mooring(env)
    _stub_wind(monkeypatch, "E")
    _run_job()
    row = _only_row(env)
    assert row["hw_timestamp"] == NEXT_HW
    assert row["start_time"] == "2027-03-02T05:35:04Z"
    assert row["end_time"] == "2027-03-02T09:59:11Z"
    assert row["wind_adjusted"] == 1
    assert row["wind_direction"] == "E"
    assert row["wind_speed_ms"] == 10.0
    assert row["wind_offset_m"] == 0.5
    assert row["always_accessible"] == 0
    assert row["title"] == "⚓ Pin Afloat (4¼h)"


def test_favourable_wind_keeps_baseline_and_records_check(env, monkeypatch):
    _mooring(env)
    _stub_wind(monkeypatch, "W")
    _run_job()
    row = _only_row(env)
    assert row["start_time"] == "2027-03-02T05:03:53Z"
    assert row["end_time"] == "2027-03-02T09:59:11Z"
    assert row["wind_adjusted"] == 1
    assert row["wind_direction"] == "W"
    assert row["wind_offset_m"] == 0.0


def test_offset_above_hw_writes_no_access_marker(env, monkeypatch):
    _mooring(env, shallow_extra_depth_m=1.5)
    _stub_wind(monkeypatch, "E")
    _run_job()
    row = _only_row(env)
    assert row["start_time"] == row["end_time"] == NEXT_HW
    assert row["wind_offset_m"] == 1.5
    assert row["title"] == "⚓ Pin - No access (wind-blown to shallows)"


def test_offset_on_always_afloat_tide_surfaces_emergent_window(env, monkeypatch):
    _mooring(env, drying_height_m=-1.0)
    _stub_wind(monkeypatch, "E")
    _run_job()
    row = _only_row(env)
    assert row["always_accessible"] == 0
    assert row["start_time"] == "2027-03-02T01:48:15Z"
    assert row["end_time"] == "2027-03-02T12:23:27Z"
    assert row["wind_adjusted"] == 1


def test_tender_start_moves_with_offset(env, monkeypatch):
    _mooring(env, tender_access_enabled=1, tender_min_depth_m=0.3)
    _stub_wind(monkeypatch, "E")
    _run_job()
    row = _only_row(env)
    assert row["start_time"] == "2027-03-02T05:35:04Z"
    assert row["tender_start_time"] == "2027-03-02T04:36:05Z"
    assert row["tender_end_time"] == "2027-03-02T10:52:18Z"


def test_barometric_opt_in_composes_with_offset(env, monkeypatch):
    import app.barometric as barometric
    import app.config as config

    # Do not depend on the master switch shipped in model_config.json.
    monkeypatch.setattr(config, "get_barometric_enabled", lambda default=False: True)
    _mooring(env, barometric_enabled=1)
    _stub_wind(monkeypatch, "E")
    # 20 hPa below the reference raises water by about 0.18 m.
    monkeypatch.setattr(
        barometric, "make_pressure_provider",
        lambda *a, **k: (lambda t: (993.25, 1.0)),
    )
    _run_job()
    row = _only_row(env)
    assert row["start_time"] == "2027-03-02T05:21:55Z"
    assert row["end_time"] == "2027-03-02T10:10:06Z"
    assert row["wind_offset_m"] == 0.5


def test_failed_wind_fetch_writes_nothing(env, monkeypatch):
    import app.wind as wind

    _mooring(env)

    async def no_wind():
        return None

    monkeypatch.setattr(wind, "fetch_current_wind", no_wind)
    _run_job()
    assert env.get_calendar_events(1) == []
