"""Moorwatch vessel config: the refuse-to-guess guard and the sync bootstrap.

The property under test is a safety one. A guessed draught or drying height
does not fail loudly -- it produces a complete, confident readout about a boat
that does not exist, with nothing on screen to say so. The shipped example must
therefore be unusable until someone states the truth about the hull and the
berth, and the guard must not be quietly defeated by editing the example.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moorwatch import config as cfg_mod
from moorwatch.config import EXAMPLE_PATH, ConfigError, VesselConfig, load, read_raw


def test_shipped_example_cannot_be_used_to_compute(tmp_path):
    """A fresh install must refuse, not invent a boat."""
    target = tmp_path / "config.json"
    with pytest.raises(ConfigError) as excinfo:
        load(target)
    message = str(excinfo.value)
    assert "draught_m" in message and "drying_height_m" in message
    assert "will not guess" in message


def test_first_run_still_creates_the_file_to_edit(tmp_path):
    """Refusing to compute is not refusing to help: the file must appear."""
    target = tmp_path / "config.json"
    with pytest.raises(ConfigError):
        load(target)
    assert target.exists(), "the example should have been copied for editing"
    assert json.loads(target.read_text())["draught_m"] is None


def test_example_ships_with_the_critical_fields_null():
    """Guards the guard: filling these in would silently re-enable guessing."""
    example = json.loads(EXAMPLE_PATH.read_text())
    assert example["draught_m"] is None
    assert example["drying_height_m"] is None
    # A margin IS a policy default, and TSCTide's schema defaults it the same.
    assert example["safety_margin_m"] == 0.3


def test_read_raw_tolerates_the_incomplete_file(tmp_path):
    """--sync must work before the config is valid, or a fresh install
    deadlocks: the only fix would require an already-fixed install."""
    target = tmp_path / "config.json"
    raw = read_raw(target)
    assert raw["draught_m"] is None
    assert raw["source_url"]


def test_partial_config_is_still_rejected(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"draught_m": 1.0, "safety_margin_m": 0.3}))
    with pytest.raises(ConfigError) as excinfo:
        load(target)
    assert "drying_height_m" in str(excinfo.value)


def test_complete_config_loads(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({
        "mooring_id": 7, "boat_name": "Moonshadow", "draught_m": 1.0,
        "drying_height_m": 0.0, "safety_margin_m": 0.3,
        "timezone": "Europe/London",
    }))
    cfg = load(target)
    assert cfg.boat_name == "Moonshadow"
    assert cfg.threshold_m == pytest.approx(1.3)


def test_malformed_json_is_reported_not_swallowed(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{not json")
    with pytest.raises(ConfigError):
        load(target)


# --- staleness --------------------------------------------------------

def _cfg(fetched_at):
    return VesselConfig(
        mooring_id=7, boat_name="Moonshadow", draught_m=1.0, drying_height_m=0.0,
        safety_margin_m=0.3, timezone="Europe/London", fetched_at=fetched_at,
    )


def test_threshold_matches_the_window_engine():
    """Must equal compute_access_windows' own base_threshold, or the readout
    and its countdown would answer different questions."""
    cfg = _cfg(None)
    assert cfg.threshold_m == cfg.drying_height_m + cfg.draught_m + cfg.safety_margin_m


def test_never_synced_counts_as_stale():
    """drying_height_m is a calibration output, not a survey. Never having
    synced is not 'age zero', it is 'unknown'."""
    cfg = _cfg(None)
    assert cfg.config_age_days() is None
    assert cfg.is_stale()


def test_fresh_sync_is_not_stale():
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    cfg = _cfg("2026-07-14T09:00:00Z")
    assert cfg.config_age_days(now) == pytest.approx(2.125, abs=0.01)
    assert not cfg.is_stale(now)


def test_old_sync_is_stale():
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    old = (now - timedelta(days=cfg_mod.STALE_CONFIG_DAYS + 1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert _cfg(old).is_stale(now)
