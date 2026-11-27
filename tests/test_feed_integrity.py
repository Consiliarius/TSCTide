"""
Feed integrity tests for v2.11.1.

Covers the three defects fixed in this release, each reproduced before the
fix on a temporary database:

- A1: a baseline recompute (02:00 refresh, calibration Apply, Save, Update
  Feed) overwrote a stored wind adjustment, and because that tide's wind job
  had already fired, the adjustment was lost.
- A2: the no-access marker was titled as a zero-length access window in the
  served feed.
- A3: a recompute that found a high water below the threshold left the old
  window in the feed, and Save did not recompute at all.

Plus the input checks added alongside: shallow_direction must be a compass
point, and Update Feed ignores client-supplied wind state.

Network calls are stubbed: UKHO (app.ukho.fetch_tidal_events), OWM wind
(app.wind.fetch_current_wind), OWM pressure forecast
(app.wind.fetch_pressure_forecast) and harmonic synthesis
(app.harmonic.predict_events). The barometric master is switched off unless
a test turns it on.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from icalendar import Calendar

from app.config import to_utc_str


PIN = "123456"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _tides(hw_heights=(4.5,), count: int = 30) -> list[dict]:
    """Alternating LW 0.5 m / HW events 6h13m apart from 12 h ago. HW heights
    cycle through ``hw_heights``."""
    out = []
    t = NOW - timedelta(hours=12)
    hw_i = 0
    for i in range(count):
        if i % 2:
            height = hw_heights[hw_i % len(hw_heights)]
            hw_i += 1
        else:
            height = 0.5
        out.append({
            "timestamp": to_utc_str(t),
            "height_m": height,
            "event_type": ["LowWater", "HighWater"][i % 2],
        })
        t += timedelta(hours=6, minutes=13)
    return out


def _next_hw(events) -> str:
    """First HW at least an hour ahead: the one a wind job would adjust."""
    cutoff = to_utc_str(NOW + timedelta(hours=1))
    return min(
        e["timestamp"] for e in events
        if e["event_type"] == "HighWater" and e["timestamp"] > cutoff
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp DB and feed dir, stubbed network, barometric master off."""
    import app.config as config
    import app.database as database
    import app.harmonic as harmonic
    import app.ical_manager as ical
    import app.pin as pin
    import app.scheduler as sched
    import app.ukho as ukho
    import app.wind as wind

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FEEDS_DIR", tmp_path / "feeds")
    monkeypatch.setattr(ical, "FEEDS_DIR", tmp_path / "feeds")
    monkeypatch.setattr(pin, "PIN_HASH_SALT", "test-salt")
    monkeypatch.setattr(config, "get_barometric_enabled", lambda default=False: False)
    monkeypatch.setattr(harmonic, "predict_events", lambda *a, **k: [])

    async def no_pressure():
        return []

    monkeypatch.setattr(wind, "fetch_pressure_forecast", no_pressure)
    database.init_db()

    state = {"events": _tides()}

    async def fake_ukho(*a, **k):
        return state["events"], "0066"

    monkeypatch.setattr(ukho, "fetch_tidal_events", fake_ukho)

    def seed(events):
        state["events"] = events
        database.store_tide_events(events, source="ukho", station="langstone")

    seed(state["events"])
    state["seed"] = seed
    state["db"] = database
    yield state
    sched._purge_wind_jobs()


def _save(env, mooring_id=1, **overrides):
    cfg = {
        "mooring_id": mooring_id, "boat_name": "", "draught_m": 1.0,
        "drying_height_m": 2.0, "safety_margin_m": 0.3,
        "wind_offset_enabled": 1, "shallow_direction": "W",
        "shallow_extra_depth_m": 0.5, "calendar_enabled": 1,
    }
    cfg.update(overrides)
    env["db"].save_mooring(cfg)
    return cfg


def _wind(monkeypatch, compass="E", speed=10.0):
    import app.wind as wind

    deg = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}[compass]

    async def fake():
        return {
            "timestamp": to_utc_str(datetime.now(timezone.utc)),
            "direction_deg": deg, "direction_compass": compass, "speed_ms": speed,
        }

    monkeypatch.setattr(wind, "fetch_current_wind", fake)


def _daily():
    import app.scheduler as sched
    asyncio.run(sched.daily_ukho_fetch())


def _wind_job(mooring_id, hw):
    import app.scheduler as sched
    asyncio.run(sched.wind_observation_job(mooring_id, hw))


def _row(env, mooring_id, hw):
    rows = [r for r in env["db"].get_calendar_events(mooring_id) if r["hw_timestamp"] == hw]
    return rows[0] if rows else None


def _feed(mooring_id) -> dict:
    """Served feed for a mooring: {uid: (summary, description)}."""
    from app.ical_manager import generate_feed_for_mooring
    cal = Calendar.from_ical(generate_feed_for_mooring(mooring_id).read_bytes())
    return {
        str(e.get("uid")): (str(e.get("summary")), str(e.get("description")))
        for e in cal.walk("VEVENT")
    }


def _client(env, mooring_id=1):
    from fastapi.testclient import TestClient
    import app.main as main
    from app.pin import hash_pin

    env["db"].set_mooring_pin_hash(mooring_id, hash_pin(PIN))
    return TestClient(main.app), {"X-Mooring-PIN": PIN}


def _adjusted(env, monkeypatch, mooring_id=1, **overrides):
    """Baseline daily run, then a wind job with wind pushing to the shallows.
    Returns (hw, baseline_row, adjusted_row)."""
    _save(env, mooring_id, **overrides)
    _daily()
    hw = _next_hw(env["events"])
    baseline = _row(env, mooring_id, hw)
    _wind(monkeypatch, "E")
    _wind_job(mooring_id, hw)
    adjusted = _row(env, mooring_id, hw)
    assert adjusted["wind_adjusted"] == 1
    return hw, baseline, adjusted


def _latest_log(env, event_type, trigger=None):
    """Newest activity entry by id (timestamps tie within one second)."""
    entries = [
        e for e in env["db"].get_activity_log(scope="mooring", mooring_id=1, event_type=event_type)
        if trigger is None or (e.get("details") or {}).get("trigger") == trigger
    ]
    return max(entries, key=lambda e: e["id"])


# --- A1: wind adjustments survive recomputes ---------------------------------

def test_daily_refresh_keeps_adjusted_start(env, monkeypatch):
    hw, baseline, adjusted = _adjusted(env, monkeypatch)
    assert adjusted["start_time"] > baseline["start_time"]
    _daily()
    after = _row(env, 1, hw)
    assert after["start_time"] == adjusted["start_time"]
    assert after["end_time"] == baseline["end_time"]
    assert after["wind_adjusted"] == 1
    assert after["wind_direction"] == "E"
    assert after["wind_offset_m"] == 0.5
    _daily()  # a second refresh changes nothing
    assert _row(env, 1, hw)["start_time"] == adjusted["start_time"]


def test_daily_refresh_keeps_no_access_marker_and_feed_titles_it(env, monkeypatch):
    hw, _baseline, adjusted = _adjusted(env, monkeypatch, shallow_extra_depth_m=1.5)
    assert adjusted["start_time"] == adjusted["end_time"] == hw
    _daily()
    after = _row(env, 1, hw)
    assert after["start_time"] == after["end_time"] == hw
    summary, description = _feed(1)[after["event_uid"]]
    assert summary == "⚓ #1 - No access (wind-blown to shallows)"
    assert "No safe access this tide" in description


def test_favourable_wind_check_survives(env, monkeypatch):
    _save(env)
    _daily()
    hw = _next_hw(env["events"])
    baseline = _row(env, 1, hw)
    _wind(monkeypatch, "W")
    _wind_job(1, hw)
    _daily()
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 1
    assert after["wind_offset_m"] == 0.0
    assert after["start_time"] == baseline["start_time"]
    _summary, description = _feed(1)[after["event_uid"]]
    assert "Offset: not applied (wind favourable)" in description


def test_reapply_uses_current_extra_depth(env, monkeypatch):
    hw, _baseline, adjusted = _adjusted(env, monkeypatch)
    _save(env, shallow_extra_depth_m=0.8)  # direct save: no recompute
    _daily()
    after = _row(env, 1, hw)
    assert after["wind_offset_m"] == 0.8
    assert after["start_time"] > adjusted["start_time"]


def test_reapply_re_evaluates_trigger_against_current_shallow_side(env, monkeypatch):
    hw, baseline, _adjusted_row = _adjusted(env, monkeypatch)
    # Wind from E pushes toward W; with the shallow side now E it is favourable.
    _save(env, shallow_direction="E")
    _daily()
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 1
    assert after["wind_offset_m"] == 0.0
    assert after["start_time"] == baseline["start_time"]


def test_wind_offset_disabled_reverts_to_baseline(env, monkeypatch):
    hw, baseline, _adjusted_row = _adjusted(env, monkeypatch)
    _save(env, wind_offset_enabled=0, shallow_direction="", shallow_extra_depth_m=0.0)
    _daily()
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 0
    assert after["start_time"] == baseline["start_time"]
    assert after["wind_direction"] is None


def test_calibration_apply_rebuild_keeps_adjustment(env, monkeypatch):
    from app.main import _recompute_future_windows

    hw, _baseline, adjusted = _adjusted(env, monkeypatch)
    _recompute_future_windows(1)
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 1
    assert after["start_time"] == adjusted["start_time"]


def test_update_feed_keeps_adjustment_and_ignores_client_wind(env, monkeypatch):
    hw, _baseline, adjusted = _adjusted(env, monkeypatch)
    client, headers = _client(env)
    calc = client.post("/api/calculate", json={"mooring_id": 1, "source": "ukho"}).json()
    forged = [
        dict(w, wind_adjusted=True, wind_no_access=False,
             wind_details={"direction": "N", "speed_ms": 1.0, "offset_m": 9.0})
        for w in calc["windows"]
    ]
    resp = client.post("/api/moorings/1/feed/update", headers=headers, json={
        "source": "ukho", "windows": forged, "parameters": calc["parameters"],
    })
    assert resp.status_code == 200
    assert resp.json()["wind_preserved"] == 1
    after = _row(env, 1, hw)
    assert after["start_time"] == adjusted["start_time"]
    assert after["wind_direction"] == "E"
    assert after["wind_offset_m"] == 0.5
    # Every other tide was stored as a plain baseline window.
    others = [r for r in env["db"].get_calendar_events(1) if r["hw_timestamp"] != hw]
    assert others and all(r["wind_adjusted"] == 0 for r in others)

    # A harmonic push for the same tides cannot replace the UKHO row.
    resp = client.post("/api/moorings/1/feed/update", headers=headers, json={
        "source": "harmonic", "windows": calc["windows"], "parameters": calc["parameters"],
    })
    assert resp.status_code == 200
    after = _row(env, 1, hw)
    assert after["source"] == "ukho"
    assert after["start_time"] == adjusted["start_time"]


def test_deadband_skips_unchanged_reapplied_window(env, monkeypatch):
    import app.barometric as barometric
    import app.config as config

    monkeypatch.setattr(config, "get_barometric_enabled", lambda default=False: True)
    monkeypatch.setattr(
        barometric, "make_pressure_provider",
        lambda *a, **k: (lambda t: (993.25, 1.0)),
    )
    hw, _baseline, adjusted = _adjusted(env, monkeypatch, barometric_enabled=1)
    _daily()
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 1
    assert after["start_time"] == adjusted["start_time"]
    # Within the deadband the stored row is left untouched, not rewritten.
    assert after["updated_at"] == adjusted["updated_at"]


def test_tender_start_adjustment_survives(env, monkeypatch):
    hw, baseline, adjusted = _adjusted(
        env, monkeypatch, tender_access_enabled=1, tender_min_depth_m=0.3,
    )
    assert adjusted["tender_start_time"] > baseline["tender_start_time"]
    _daily()
    after = _row(env, 1, hw)
    assert after["tender_start_time"] == adjusted["tender_start_time"]
    assert after["tender_end_time"] == baseline["tender_end_time"]


def test_daily_refresh_uses_fresh_events_after_a_revised_hw(env, monkeypatch):
    from app.access_calc import compute_next_window_with_wind

    hw, _baseline, _adjusted_row = _adjusted(env, monkeypatch)
    # UKHO revises that HW 20 minutes later; the superseded row stays in
    # tide_data (unique on timestamp), but the fetch returns only the new one.
    revised = []
    for e in env["events"]:
        if e["timestamp"] == hw:
            shifted = datetime.fromisoformat(hw.replace("Z", "+00:00")) + timedelta(minutes=20)
            e = dict(e, timestamp=to_utc_str(shifted))
        revised.append(e)
    env["seed"](revised)
    new_hw = to_utc_str(
        datetime.fromisoformat(hw.replace("Z", "+00:00")) + timedelta(minutes=20)
    )
    _daily()
    after = _row(env, 1, new_hw)
    assert after is not None and after["wind_adjusted"] == 1
    expected = compute_next_window_with_wind(revised, 1.0, 2.0, 0.3, new_hw, 0.5)
    assert after["start_time"] == expected["start_time"]


# --- R3: re-apply failure handling -------------------------------------------

def test_failed_reapply_keeps_row_only_with_same_vessel_parameters(env, monkeypatch):
    from app.access_calc import compute_access_windows
    from app.scheduler import reapply_stored_wind

    hw, _baseline, adjusted = _adjusted(env, monkeypatch)
    m = env["db"].get_mooring(1)
    windows = [w for w in compute_access_windows(env["events"], 1.0, 2.0, 0.3)
               if w["hw_timestamp"] == hw]
    rows = env["db"].get_calendar_events(1)

    # No tide data reaches the recompute: same parameters, row kept, cycle
    # dropped from the write.
    out, _tender, stats = reapply_stored_wind(m, windows, None, rows, events=[])
    assert out == [] and stats["failed"] == 1 and stats["deleted"] == 0
    assert _row(env, 1, hw)["start_time"] == adjusted["start_time"]

    # Same failure with different vessel parameters: the stale row is removed.
    params = {"draught_m": 1.2, "drying_height_m": 2.0, "safety_margin_m": 0.3}
    out, _tender, stats = reapply_stored_wind(
        m, windows, None, rows, events=[], params=params,
    )
    assert out == [] and stats["failed"] == 1 and stats["deleted"] == 1
    assert _row(env, 1, hw) is None


# --- A3: below-threshold recomputes and Save ---------------------------------

def test_save_with_higher_threshold_removes_stale_windows_at_once(env):
    from fastapi.testclient import TestClient
    import app.main as main

    env["seed"](_tides(hw_heights=(4.5, 4.0)))
    cfg = _save(env)
    _daily()
    low_hws = {e["timestamp"] for e in env["events"]
               if e["event_type"] == "HighWater" and e["height_m"] == 4.0}
    stored = {r["hw_timestamp"] for r in env["db"].get_calendar_events(1)}
    assert stored & low_hws  # 4.0 m tides clear the 3.3 m threshold

    # Draught 1.9 m puts the threshold at 4.2 m. No daily run in between.
    resp = TestClient(main.app).post("/api/moorings", json=dict(cfg, draught_m=1.9))
    assert resp.status_code == 200
    rows = env["db"].get_calendar_events(1)
    future = [r for r in rows if r["hw_timestamp"] > to_utc_str(NOW)]
    assert future and not ({r["hw_timestamp"] for r in future} & low_hws)
    assert all(r["draught_m"] == 1.9 for r in future)


def test_daily_refresh_removes_window_that_fell_below_threshold(env):
    env["seed"](_tides(hw_heights=(4.5, 4.0)))
    _save(env)
    _daily()
    low_hws = {e["timestamp"] for e in env["events"]
               if e["event_type"] == "HighWater" and e["height_m"] == 4.0}
    _save(env, draught_m=1.9)  # direct save: no recompute, as before v2.11.1
    _daily()
    stored = {r["hw_timestamp"] for r in env["db"].get_calendar_events(1)}
    assert not (stored & low_hws)
    feed_hws = set()
    for uid in _feed(1):
        row = [r for r in env["db"].get_calendar_events(1) if r["event_uid"] == uid][0]
        feed_hws.add(row["hw_timestamp"])
    assert not (feed_hws & low_hws)


def test_harmonic_below_threshold_does_not_remove_ukho_row(env):
    from app.ical_manager import store_windows_as_events

    _save(env)
    _daily()
    row = env["db"].get_calendar_events(1)[-1]
    result = store_windows_as_events(
        [{"hw_timestamp": row["hw_timestamp"], "below_threshold": True}], 1, "harmonic",
    )
    assert result["removed"] == 0
    assert _row(env, 1, row["hw_timestamp"]) is not None


def test_name_only_save_does_not_recompute(env):
    from fastapi.testclient import TestClient
    import app.main as main

    cfg = _save(env)
    _daily()
    before = {r["event_uid"]: r["updated_at"] for r in env["db"].get_calendar_events(1)}
    resp = TestClient(main.app).post("/api/moorings", json=dict(cfg, boat_name="Renamed"))
    assert resp.status_code == 200
    after = {r["event_uid"]: r["updated_at"] for r in env["db"].get_calendar_events(1)}
    assert after == before


# --- Input checks ------------------------------------------------------------

@pytest.mark.parametrize("value,status", [
    ("NW", 200), ("", 200), ("<img src=x onerror=alert(1)>", 400), ("north", 400),
])
def test_shallow_direction_must_be_a_compass_point(env, value, status):
    from fastapi.testclient import TestClient
    import app.main as main

    cfg = {
        "mooring_id": 2, "draught_m": 1.0, "drying_height_m": 2.0,
        "safety_margin_m": 0.3, "shallow_direction": value,
    }
    assert TestClient(main.app).post("/api/moorings", json=cfg).status_code == status


# --- Independent review follow-ups -------------------------------------------

def test_wind_check_survives_its_window_being_removed(env, monkeypatch):
    """Marker -> tide below threshold (row removed) -> tide clears again: the
    recorded wind check is re-applied, so the marker comes back rather than a
    plain baseline window."""
    hw, _baseline, adjusted = _adjusted(env, monkeypatch, shallow_extra_depth_m=1.5)
    assert adjusted["start_time"] == adjusted["end_time"] == hw
    _save(env, shallow_extra_depth_m=1.5, draught_m=2.5)  # threshold 4.8 > HW 4.5
    _daily()
    assert _row(env, 1, hw) is None
    assert [c["event_uid"] for c in env["db"].get_wind_checks(1)] == [adjusted["event_uid"]]
    _save(env, shallow_extra_depth_m=1.5)  # back to draught 1.0
    _daily()
    after = _row(env, 1, hw)
    assert after["start_time"] == after["end_time"] == hw
    assert after["wind_adjusted"] == 1
    assert _feed(1)[after["event_uid"]][0].endswith("No access (wind-blown to shallows)")


def test_wind_job_removes_window_when_tide_no_longer_clears(env, monkeypatch):
    _save(env)
    _daily()
    hw = _next_hw(env["events"])
    assert _row(env, 1, hw) is not None
    _save(env, draught_m=2.5)  # direct save, no recompute: threshold 4.8 > 4.5
    _wind(monkeypatch, "E")
    _wind_job(1, hw)
    assert _row(env, 1, hw) is None
    assert len(env["db"].get_wind_checks(1)) == 1


def test_wind_job_uses_config_saved_during_the_wind_fetch(env, monkeypatch):
    import app.wind as wind

    _save(env)
    _daily()
    hw = _next_hw(env["events"])
    baseline = _row(env, 1, hw)

    async def wind_while_saving():
        # A Save lands while the OWM request is in flight.
        _save(env, wind_offset_enabled=0, shallow_direction="", shallow_extra_depth_m=0.0)
        return {"timestamp": to_utc_str(datetime.now(timezone.utc)),
                "direction_deg": 90.0, "direction_compass": "E", "speed_ms": 10.0}

    monkeypatch.setattr(wind, "fetch_current_wind", wind_while_saving)
    _wind_job(1, hw)
    after = _row(env, 1, hw)
    assert after["wind_adjusted"] == 0
    assert after["start_time"] == baseline["start_time"]


def test_daily_refresh_starting_at_a_wind_checked_hw_reports_no_failure(env, monkeypatch):
    hw, _baseline, adjusted = _adjusted(env, monkeypatch)
    # The next fetch starts at that HW: no LW before it, so its baseline has
    # no start. The stored adjusted row must be left alone, not flagged.
    env["events"] = [e for e in env["events"] if e["timestamp"] >= hw]
    _daily()
    after = _row(env, 1, hw)
    assert after["start_time"] == adjusted["start_time"] and after["wind_adjusted"] == 1
    entry = _latest_log(env, "feed_generation", "daily_scheduler")
    assert entry["severity"] == "info"
    assert entry["details"]["windows_wind_failed"] == 0
    assert entry["details"]["windows_incomplete"] >= 1
    for key in ("windows_wind_preserved", "windows_wind_reverted",
                "windows_removed_below_threshold", "windows_wind_failed_deleted"):
        assert key in entry["details"]


def test_update_feed_reports_removals_and_rejects_windows_without_hw(env):
    env["seed"](_tides(hw_heights=(4.5, 4.0)))
    _save(env)
    _daily()
    client, headers = _client(env)
    calc = client.post("/api/calculate", json={
        "mooring_id": 1, "source": "ukho", "draught_m": 1.9,
    }).json()
    resp = client.post("/api/moorings/1/feed/update", headers=headers, json={
        "source": "ukho", "windows": calc["windows"], "parameters": calc["parameters"],
    })
    assert resp.status_code == 200 and resp.json()["removed"] > 0
    bad = [dict(calc["windows"][0])]
    del bad[0]["hw_timestamp"]
    resp = client.post("/api/moorings/1/feed/update", headers=headers, json={
        "source": "ukho", "windows": bad, "parameters": calc["parameters"],
    })
    assert resp.status_code == 400


def test_save_without_ukho_data_clears_and_says_so(env):
    from fastapi.testclient import TestClient
    import app.main as main
    import app.scheduler as sched

    cfg = _save(env)
    _daily()
    assert env["db"].get_calendar_events(1)
    with env["db"].db_connection() as conn:
        conn.execute("DELETE FROM tide_data")
    calls = []
    real = sched.ensure_wind_jobs_scheduled
    main_ensure = main.ensure_wind_jobs_scheduled

    def counting():
        calls.append(1)
        return real()

    try:
        main.ensure_wind_jobs_scheduled = counting
        resp = TestClient(main.app).post("/api/moorings", json=dict(cfg, draught_m=1.2))
    finally:
        main.ensure_wind_jobs_scheduled = main_ensure
    assert resp.status_code == 200
    future = [r for r in env["db"].get_calendar_events(1)
              if r["hw_timestamp"] > to_utc_str(NOW)]
    assert future == []
    assert calls, "wind jobs must still be rescheduled"
    saved = _latest_log(env, "mooring_config")
    assert saved["details"]["windows_recomputed"] is False
    assert saved["details"]["windows_cleared_no_ukho_data"] is True
    feed = _latest_log(env, "feed_generation", "config_save")
    assert feed["severity"] == "warning" and feed["details"]["status"] == "no_data"


def _with_sentinel(env):
    """A long-range harmonic row (day 20), which only a recompute clears."""
    from app.access_calc import generate_event_uid
    hw = to_utc_str(NOW + timedelta(days=20))
    env["db"].upsert_calendar_event({
        "event_uid": generate_event_uid(1, hw), "mooring_id": 1, "hw_timestamp": hw,
        "start_time": hw, "end_time": to_utc_str(NOW + timedelta(days=20, hours=3)),
        "source": "harmonic", "title": "sentinel",
    })
    return hw


@pytest.mark.parametrize("base,change,master,recomputes", [
    ({}, {"draught_m": 1.1}, False, True),
    ({}, {"drying_height_m": 2.1}, False, True),
    ({}, {"safety_margin_m": 0.4}, False, True),
    ({}, {"tender_access_enabled": 1}, False, True),
    ({"tender_access_enabled": 1}, {"tender_min_depth_m": 0.5}, False, True),
    ({"tender_access_enabled": 0}, {"tender_min_depth_m": 0.5}, False, False),
    ({}, {"shallow_extra_depth_m": 0.7}, False, True),
    ({}, {"shallow_direction": "NW"}, False, True),
    ({}, {"wind_offset_enabled": 0}, False, True),
    ({"wind_offset_enabled": 0}, {"shallow_direction": "NW"}, False, False),
    ({}, {"barometric_enabled": 1}, False, False),
    ({}, {"barometric_enabled": 1}, True, True),
    ({}, {"boat_name": "Renamed"}, False, False),
    ({}, {"calendar_enabled": 0}, False, False),
])
def test_save_recomputes_only_on_effective_setting_changes(env, monkeypatch, base, change,
                                                          master, recomputes):
    from fastapi.testclient import TestClient
    import app.config as config
    import app.main as main

    monkeypatch.setattr(config, "get_barometric_enabled", lambda default=False: master)
    cfg = _save(env, **base)
    sentinel = _with_sentinel(env)
    resp = TestClient(main.app).post("/api/moorings", json=dict(cfg, **change))
    assert resp.status_code == 200
    assert (_row(env, 1, sentinel) is None) == recomputes


def test_harmonic_push_skips_tides_inside_the_ukho_range(env):
    from app.access_calc import compute_access_windows

    _save(env)
    client, headers = _client(env)
    ukho_hw = _next_hw(env["events"])
    inside = [w for w in compute_access_windows(env["events"], 1.0, 2.0, 0.3, source="harmonic")
              if w["hw_timestamp"] == ukho_hw][0]
    far = to_utc_str(NOW + timedelta(days=12))
    beyond = {"hw_timestamp": far, "hw_height_m": 4.5, "duration_minutes": 180,
              "start_time": far, "end_time": to_utc_str(NOW + timedelta(days=12, hours=3))}
    resp = client.post("/api/moorings/1/feed/update", headers=headers, json={
        "source": "harmonic", "windows": [inside, beyond],
        "parameters": {"draught_m": 1.0, "drying_height_m": 2.0, "safety_margin_m": 0.3},
    })
    assert resp.status_code == 200 and resp.json()["dropped_in_ukho_range"] == 1
    assert _row(env, 1, ukho_hw) is None
    assert _row(env, 1, far)["source"] == "harmonic"


def test_wind_checks_are_pruned_and_removed_with_the_mooring(env):
    _save(env)
    old = to_utc_str(NOW - timedelta(days=20))
    new = to_utc_str(NOW + timedelta(hours=5))
    env["db"].record_wind_check(1, "u-old", old, "E", 9.0)
    env["db"].record_wind_check(1, "u-new", new, "E", 9.0)
    env["db"].cleanup_old_events(14)
    assert [c["event_uid"] for c in env["db"].get_wind_checks(1)] == ["u-new"]
    env["db"].delete_mooring(1)
    assert env["db"].get_wind_checks(1) == []
