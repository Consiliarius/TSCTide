"""
Backfill observations.pressure_hpa from the durable pressure archive.

Since v2.11 each observation freezes the measured sea-level pressure at its
time (add_observation -> get_pressure_at) so calibration can correct the tide
height to the ACTUAL barometric conditions when backing out the mooring's
static drying height. Observations recorded before v2.11 — and any that were
entered while no archive reading was within tolerance — carry a NULL
pressure_hpa and are treated pressure-blind in calibration.

This script fills those NULLs for observations whose time the pressure_history
archive now covers. It is idempotent (only NULL rows are touched; frozen values
are never overwritten) and safe to re-run: rows whose time predates the archive
stay NULL.

When to run:
  - Once after deploying v2.11, to correct historical observations that fall
    within the archive's coverage.
  - Occasionally thereafter is harmless but rarely useful, since new
    observations are stamped at entry.

No PIN required (operator-only command, executed inside the container).

Usage:
  docker exec tidal-access python -m scripts.backfill_observation_pressure
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    from app.config import ensure_dirs
    from app.database import init_db, backfill_observation_pressure, log_activity

    ensure_dirs()
    init_db()

    result = backfill_observation_pressure()
    logger.info(
        "Backfill complete: scanned %d NULL rows, filled %d, %d still missing "
        "(outside archive coverage).",
        result["scanned"], result["updated"], result["still_missing"],
    )

    log_activity(
        event_type="pressure_backfill",
        message=(
            f"Observation pressure backfill: filled {result['updated']} of "
            f"{result['scanned']} rows ({result['still_missing']} outside "
            f"archive coverage)"
        ),
        severity="success" if result["updated"] else "info",
        details={**result, "trigger": "scripts.backfill_observation_pressure"},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
