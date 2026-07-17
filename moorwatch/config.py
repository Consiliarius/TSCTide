"""Vessel configuration for the on-board readout.

``config.json`` is a local cache of the mooring's TSCTide configuration, seeded
from ``GET /api/moorings/{id}`` (a PIN-free read; the server strips pin_hash).
It is machine-specific and not committed; on first run the example is copied,
following the same convention as SYLog's config.

Why the age of this file matters
--------------------------------
``drying_height_m`` is not a surveyed constant. It is the calibrated OUTPUT of
TSCTide's observation corpus and moves as calibration improves. A stale
config means the readout is modelling the wrong seabed, and it will say so with
complete confidence -- there is no symptom to notice. Hence ``fetched_at`` and
``config_age_days``, which the display surfaces rather than hides.

Sync it whenever the netbook has wifi ashore:

    python3 -m moorwatch --sync
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).parent
CONFIG_PATH = _DIR / "config.json"
EXAMPLE_PATH = _DIR / "config.example.json"

# Age past which the display warns that the calibrated drying height may have
# moved under it. Calibration shifts are gradual, so this is a nudge to sync
# ashore, not a hard error -- the readout keeps working.
STALE_CONFIG_DAYS = 90


class ConfigError(Exception):
    """Raised when config.json is missing required fields or is unreadable."""


@dataclass(frozen=True)
class VesselConfig:
    """The mooring configuration needed to answer the float question.

    Mirrors the subset of TSCTide's ``moorings`` row that the readout uses.
    ``shallow_*`` are carried but unused in v1; they exist for the wind offset.
    """

    mooring_id: int
    boat_name: str
    draught_m: float
    drying_height_m: float
    safety_margin_m: float
    timezone: str
    shallow_direction: str = ""
    shallow_extra_depth_m: float = 0.0
    source_url: str = ""
    fetched_at: Optional[str] = None
    tsctide_commit: Optional[str] = None

    @property
    def threshold_m(self) -> float:
        """Height above CD at which TSCTide considers the mooring accessible.

        Must stay identical to ``access_calc.compute_access_windows``'s own
        ``base_threshold``, or the readout's state and its countdown would be
        answering different questions.
        """
        return self.drying_height_m + self.draught_m + self.safety_margin_m

    def config_age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        """Days since this config was synced, or None if never synced."""
        if not self.fetched_at:
            return None
        try:
            fetched = _parse_iso_z(self.fetched_at)
        except ValueError:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - fetched).total_seconds() / 86400.0

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """True when the config has never been synced, or is old enough that
        the calibrated drying height may have moved."""
        age = self.config_age_days(now)
        return age is None or age > STALE_CONFIG_DAYS


def _parse_iso_z(value: str) -> datetime:
    """Parse TSCTide's canonical ISO-Z timestamp form (see config.to_utc_str).

    Uses the stdlib rather than dateutil: this module is imported by the Tk UI,
    and the only timestamps it parses are ones this package wrote itself.
    """
    iso = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_REQUIRED = ("draught_m", "drying_height_m", "safety_margin_m")


def read_raw(path: Optional[Path] = None) -> dict:
    """The config file's raw contents, seeding from the example on first run.

    Separate from ``load`` because ``--sync`` has to work *before* the file is
    valid -- it is the command that makes it valid. Going through ``load``
    there would deadlock: the only way to fix an unconfigured install would
    require an already-configured install.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        if not EXAMPLE_PATH.exists():
            raise ConfigError(f"No config at {path} and no example to copy.")
        shutil.copy(EXAMPLE_PATH, path)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"Could not read {path}: {e}") from e


def load(path: Optional[Path] = None) -> VesselConfig:
    """Load the local vessel config, copying the example on first run.

    Raises ConfigError rather than falling back to defaults. The example ships
    with draught_m and drying_height_m null precisely so this fires: a guessed
    draught or drying height produces a fully-formed, confident readout that is
    wrong about whether the boat floats, and the only clue would be a footer
    line nobody reads. Refusing to show a number is the correct answer to not
    knowing the boat.

    safety_margin_m keeps a real 0.3 m default -- it is a policy choice, and
    TSCTide's own schema defaults it the same way. The other two are facts about
    a specific hull and a specific patch of seabed, and cannot be defaulted.
    """
    path = path or CONFIG_PATH
    first_run = not path.exists()
    raw = read_raw(path)

    missing = [k for k in _REQUIRED if raw.get(k) is None]
    if missing:
        created = "Created " if first_run else ""
        raise ConfigError(
            f"{created}{path} has no {', '.join(missing)}.\n"
            f"Moorwatch will not guess a draught or a drying height.\n\n"
            f"Either sync from TSCTide (needs wifi):\n"
            f"    python3 -m moorwatch --sync --url https://tsctide.uk --mooring <id>\n"
            f"or edit that file by hand."
        )

    try:
        return VesselConfig(
            mooring_id=int(raw.get("mooring_id") or 0),
            boat_name=str(raw.get("boat_name", "") or ""),
            draught_m=float(raw["draught_m"]),
            drying_height_m=float(raw["drying_height_m"]),
            safety_margin_m=float(raw["safety_margin_m"]),
            timezone=str(raw.get("timezone") or "Europe/London"),
            shallow_direction=str(raw.get("shallow_direction", "") or ""),
            shallow_extra_depth_m=float(raw.get("shallow_extra_depth_m") or 0.0),
            source_url=str(raw.get("source_url", "") or ""),
            fetched_at=raw.get("fetched_at"),
            tsctide_commit=raw.get("tsctide_commit"),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path} has a malformed value: {e}") from e


def save(cfg: VesselConfig, path: Optional[Path] = None) -> None:
    """Write a config back, atomically. Used by --sync."""
    path = path or CONFIG_PATH
    payload = {
        "mooring_id": cfg.mooring_id,
        "boat_name": cfg.boat_name,
        "draught_m": cfg.draught_m,
        "drying_height_m": cfg.drying_height_m,
        "safety_margin_m": cfg.safety_margin_m,
        "timezone": cfg.timezone,
        "shallow_direction": cfg.shallow_direction,
        "shallow_extra_depth_m": cfg.shallow_extra_depth_m,
        "source_url": cfg.source_url,
        "fetched_at": cfg.fetched_at,
        "tsctide_commit": cfg.tsctide_commit,
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    tmp.replace(path)
