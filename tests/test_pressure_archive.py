"""
Unit tests for the durable measured-pressure archive lookup,
app.database.get_pressure_at (v2.11).

get_pressure_at reads the pressure_history table and returns the measured
pressure at an arbitrary past time, interpolating between bracketing readings
and degrading to None (rather than extrapolating) outside coverage. It backs
the pressure correction applied to observation calibration, so its edge
behaviour — gaps, coverage edges, empty archive — is the important surface.

DB_PATH is a module global in app.database, so we monkeypatch it to a temp file
(the same pattern as test_wind_scheduling_integration).
"""

import pytest

from app.config import to_utc_str
from datetime import datetime, timezone


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.database as database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database


def _seed(db, readings):
    """readings: list of (iso_z_timestamp, pressure_hpa)."""
    for ts, p in readings:
        db.store_pressure_reading(ts, p)


def test_empty_archive_returns_none(db):
    assert db.get_pressure_at("2026-06-02T10:00:00Z") is None


def test_exact_hit_returns_that_reading(db):
    _seed(db, [("2026-06-02T10:00:00Z", 1005.0)])
    assert db.get_pressure_at("2026-06-02T10:00:00Z") == 1005.0


def test_linear_interpolation_between_two_readings(db):
    # 10:00 -> 1000, 10:30 -> 1006; midpoint 10:15 -> 1003.
    _seed(db, [
        ("2026-06-02T10:00:00Z", 1000.0),
        ("2026-06-02T10:30:00Z", 1006.0),
    ])
    got = db.get_pressure_at("2026-06-02T10:15:00Z")
    assert got == pytest.approx(1003.0, abs=1e-6)


def test_quarter_point_interpolation(db):
    _seed(db, [
        ("2026-06-02T10:00:00Z", 1000.0),
        ("2026-06-02T11:00:00Z", 1004.0),
    ])
    # 10:15 is 1/4 of the way -> 1001.0.
    assert db.get_pressure_at("2026-06-02T10:15:00Z") == pytest.approx(1001.0, abs=1e-6)


def test_nearest_neighbour_at_coverage_edge(db):
    # Target before the earliest reading: only the 'hi' side exists. Within the
    # 90-min default tolerance, so the nearest (earliest) reading is used.
    _seed(db, [("2026-06-02T10:00:00Z", 1010.0)])
    assert db.get_pressure_at("2026-06-02T09:30:00Z") == 1010.0


def test_gap_wider_than_tolerance_returns_none(db):
    # Nearest reading either side is >90 min away -> uncovered gap -> None.
    _seed(db, [
        ("2026-06-02T06:00:00Z", 1000.0),
        ("2026-06-02T14:00:00Z", 1000.0),
    ])
    assert db.get_pressure_at("2026-06-02T10:00:00Z") is None


def test_one_sided_gap_uses_in_tolerance_side(db):
    # lo is 20 min before (in tolerance); hi is 5 h after (out of tolerance).
    # Must NOT interpolate across the far gap -> returns the near lo reading.
    _seed(db, [
        ("2026-06-02T09:40:00Z", 1002.0),
        ("2026-06-02T15:00:00Z", 990.0),
    ])
    assert db.get_pressure_at("2026-06-02T10:00:00Z") == 1002.0


def test_accepts_datetime_input(db):
    _seed(db, [("2026-06-02T10:00:00Z", 1007.0)])
    dt = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
    assert db.get_pressure_at(dt) == 1007.0


def test_custom_tolerance_narrows_coverage(db):
    _seed(db, [("2026-06-02T10:00:00Z", 1010.0)])
    # 40 min away: inside default 90 min, outside a tight 15 min.
    assert db.get_pressure_at("2026-06-02T10:40:00Z", max_gap_minutes=90.0) == 1010.0
    assert db.get_pressure_at("2026-06-02T10:40:00Z", max_gap_minutes=15.0) is None
