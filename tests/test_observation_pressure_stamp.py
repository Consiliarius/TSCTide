"""
Tests for freezing measured pressure onto observations at entry (v2.11) and the
backfill of pre-existing rows.

add_observation stamps observations.pressure_hpa from the durable
pressure_history archive at the observation time, so calibration can later
correct the tide height to the actual barometric conditions. When the archive
does not cover the time the stamp is NULL and the row stays pressure-blind.

DB_PATH is monkeypatched to a temp file (same pattern as the other DB tests).
"""

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.database as database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database


def _mooring(db, mooring_id=1):
    db.save_mooring({
        "mooring_id": mooring_id, "boat_name": "Test", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 0, "shallow_direction": "",
        "shallow_extra_depth_m": 0.0, "calendar_enabled": 1,
    })


def test_entry_stamps_pressure_from_archive(db):
    _mooring(db)
    db.store_pressure_reading("2026-06-02T09:50:00Z", 1000.0)
    db.store_pressure_reading("2026-06-02T10:10:00Z", 1010.0)

    obs = db.add_observation({
        "mooring_id": 1, "timestamp": "2026-06-02T10:00:00Z", "state": "aground",
    })
    # Interpolated midpoint of 1000 / 1010 at 10:00 -> 1005.
    assert obs["pressure_hpa"] == pytest.approx(1005.0, abs=1e-6)

    stored = db.get_observations(1)[0]
    assert stored["pressure_hpa"] == pytest.approx(1005.0, abs=1e-6)


def test_entry_leaves_pressure_null_when_uncovered(db):
    _mooring(db)
    # Archive reading is a full day away -> no coverage -> NULL.
    db.store_pressure_reading("2026-06-01T10:00:00Z", 1000.0)

    obs = db.add_observation({
        "mooring_id": 1, "timestamp": "2026-06-02T10:00:00Z", "state": "afloat",
    })
    assert obs["pressure_hpa"] is None
    assert db.get_observations(1)[0]["pressure_hpa"] is None


def test_explicit_pressure_overrides_lookup(db):
    _mooring(db)
    db.store_pressure_reading("2026-06-02T10:00:00Z", 1000.0)

    obs = db.add_observation({
        "mooring_id": 1, "timestamp": "2026-06-02T10:00:00Z", "state": "aground",
        "pressure_hpa": 987.6,
    })
    assert obs["pressure_hpa"] == pytest.approx(987.6)
    assert db.get_observations(1)[0]["pressure_hpa"] == pytest.approx(987.6)


def test_backfill_fills_covered_rows_only_and_is_idempotent(db):
    _mooring(db)
    # Two observations entered with NO archive coverage yet -> both NULL.
    db.add_observation({"mooring_id": 1, "timestamp": "2026-06-02T10:00:00Z", "state": "aground"})
    db.add_observation({"mooring_id": 1, "timestamp": "2026-06-05T10:00:00Z", "state": "afloat"})
    assert all(o["pressure_hpa"] is None for o in db.get_observations(1))

    # Now the archive gains coverage for only the FIRST observation's time.
    db.store_pressure_reading("2026-06-02T09:55:00Z", 1002.0)
    db.store_pressure_reading("2026-06-02T10:05:00Z", 1002.0)

    res = db.backfill_observation_pressure()
    assert res == {"scanned": 2, "updated": 1, "still_missing": 1}

    by_ts = {o["timestamp"]: o for o in db.get_observations(1)}
    assert by_ts["2026-06-02T10:00:00Z"]["pressure_hpa"] == pytest.approx(1002.0)
    assert by_ts["2026-06-05T10:00:00Z"]["pressure_hpa"] is None

    # Re-running touches nothing new (already-filled row is not rescanned).
    res2 = db.backfill_observation_pressure()
    assert res2 == {"scanned": 1, "updated": 0, "still_missing": 1}
