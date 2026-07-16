"""Turning-point refinement in the harmonic model.

Regression cover for sampling-grid aliasing. predict_events samples a coarse
grid anchored at its `start` argument, and refinement used to fit a quadratic
through three of those samples. Through the Solent HW stand the curve is
flat-topped, so that fit was ill-conditioned and the refined peak tracked the
grid rather than the tide: sweeping the anchor across one 6-minute step moved
the same physical HW over a 10-minute span. Both production callers anchor
their range at `now`, so they re-aliased on every call.

Refinement now converges against the curve itself, so these properties hold for
any anchor and any step.
"""
from datetime import datetime, timedelta, timezone

from app import harmonic

# A HW through the Solent stand — the flat-topped case that aliased worst.
STAND_HW = datetime(2026, 7, 17, 1, 2, tzinfo=timezone.utc)
# The LW roughly half a cycle later, covering the trough branch of the search.
STAND_LW = STAND_HW + timedelta(hours=6, minutes=15)

# Refinement converges well inside the whole-second output precision, so the
# only variation left is a peak rounding either side of a second boundary.
TOLERANCE = timedelta(seconds=2)


def _parse(event):
    return datetime.strptime(event["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _event_near(target, kind, anchor, step_min=6):
    """The `kind` event predicted closest to `target`, from a range anchored at
    `anchor` — the degree of freedom that used to change the answer."""
    events = harmonic.predict_events(
        anchor, anchor + timedelta(hours=28), step_min=step_min
    )
    matches = [e for e in events if e["event_type"] == kind]
    assert matches, f"no {kind} predicted in range anchored at {anchor}"
    return _parse(min(matches, key=lambda e: abs(_parse(e) - target)))


def test_event_times_independent_of_range_anchor():
    # Sweep the anchor across a full sampling step; the tide must not move.
    for target, kind in ((STAND_HW, "HighWater"), (STAND_LW, "LowWater")):
        base = target - timedelta(hours=14)
        seen = {
            _event_near(target, kind, base + timedelta(minutes=m)) for m in range(6)
        }
        assert max(seen) - min(seen) <= TOLERANCE, f"{kind} moved with the anchor: {seen}"


def test_event_times_independent_of_sampling_step():
    # step_min only brackets turning points; it must not set their timing.
    for target, kind in ((STAND_HW, "HighWater"), (STAND_LW, "LowWater")):
        base = target - timedelta(hours=14)
        seen = {_event_near(target, kind, base, step_min=s) for s in (1, 2, 3, 6)}
        assert max(seen) - min(seen) <= TOLERANCE, f"{kind} moved with the step: {seen}"


def test_refined_peak_matches_a_brute_force_scan_of_the_curve():
    # The refined stand HW must be the curve's actual maximum, not an artefact
    # of the samples. Compare against a 1-second scan of predict_height_at_time.
    reported = _event_near(STAND_HW, "HighWater", STAND_HW - timedelta(hours=14))
    peak = reported + timedelta(minutes=harmonic.HW_ADMIRALTY_OFFSET_MINUTES)
    scan_start = peak - timedelta(minutes=10)
    truth = max(
        (scan_start + timedelta(seconds=s) for s in range(20 * 60)),
        key=harmonic.predict_height_at_time,
    )
    assert abs(peak - truth) <= TOLERANCE
