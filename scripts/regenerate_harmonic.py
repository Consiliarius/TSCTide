"""
Manual regeneration of the harmonic_predictions table.

Regenerates the next 180 days of harmonic HW/LW predictions for
Langstone Harbour using the current `app/model_config.json` constants,
applies the Portsmouth->Langstone secondary-port offset, and inserts
the predictions into `harmonic_predictions` with a fresh `generated_at`
timestamp.

When to run:
  - After a manual recalibration of the harmonic model (changes to
    `app/model_config.json` followed by a `docker compose up -d --build`).
    The next 02:00 scheduler job would also pick up the new constants,
    but this script avoids the wait.
  - To restore harmonic coverage if the table has been truncated for any
    reason and you do not want to wait for 02:00 or restart the
    container (the lifespan warm-up only runs when the table is empty).

Behaviour:
  - Idempotent in effect: each invocation produces a new `generated_at`
    row per cycle. `get_harmonic_predictions(latest_only=True)` always
    returns the freshest version. Old rows are retained for 365 days
    by `cleanup_old_harmonic_predictions` for residual analysis.
  - No PIN required (operator-only command, executed inside the
    container).

Usage:
  docker exec tidal-access python -m scripts.regenerate_harmonic
"""

import logging
import sys
from datetime import datetime, timedelta, timezone

# Initialise app config + logging in the same way scheduled jobs do.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    from app.config import ensure_dirs, load_model_config
    from app.database import init_db, store_harmonic_predictions, log_activity
    from app.harmonic import predict_events as harmonic_predict_events
    from app.secondary_port import apply_offset

    ensure_dirs()
    init_db()
    load_model_config()

    start = datetime.now(timezone.utc)
    end = start + timedelta(days=180)

    logger.info(f"Regenerating harmonic predictions for {start.isoformat()} -> {end.isoformat()}")
    raw = harmonic_predict_events(start, end)
    if not raw:
        logger.error("harmonic_predict_events returned no events")
        log_activity(
            event_type="harmonic_refresh",
            message="Manual regeneration returned no events",
            severity="error",
            details={"trigger": "scripts.regenerate_harmonic"},
        )
        return 2

    langstone = apply_offset(raw)
    inserted = store_harmonic_predictions(langstone)
    logger.info(f"Stored {inserted} harmonic predictions (180-day horizon).")

    log_activity(
        event_type="harmonic_refresh",
        message=f"Manual regeneration: stored {inserted} harmonic predictions",
        severity="success" if inserted else "warning",
        details={
            "event_count": inserted,
            "window_days": 180,
            "trigger": "scripts.regenerate_harmonic",
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
