"""Moorwatch state logic: the status ladder, crossings, and the near-LW warning.

These lock down two bugs found during the build that would otherwise regress
silently, because both produce a plausible-looking readout rather than a crash:

  * ``_crossings`` discarding incomplete_data windows, which loses the
    touch-down time entirely for a mooring whose line sits near the LW height.
  * the access line and the float line being conflated, which mislabels a boat
    that is afloat inside the safety margin as still aground.

The tide events here are synthetic and hand-written so each case is exact; the
harmonic model is exercised end-to-end separately by the CLI.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moorwatch.config import VesselConfig
from moorwatch.state import (
    AFLOAT,
    AGROUND,
    DRIED_OUT,
    MARGIN,
    _crossings,
    _governing,
    _near_lw_warning,
    _status,
    compute_state,
)


def cfg(**kw) -> VesselConfig:
    base = dict(
        mooring_id=1,
        boat_name="Test",
        draught_m=1.0,
        drying_height_m=2.0,
        safety_margin_m=0.3,
        timezone="Europe/London",
    )
    base.update(kw)
    return VesselConfig(**base)


def dt(hour, minute=0, day=16):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


# --- the status ladder ------------------------------------------------

def test_status_ladder_distinguishes_all_four_rungs():
    # threshold = drying(2.0) + draught(1.0) + margin(0.3) = 3.3
    assert _status(depth_m=-0.5, clearance_m=-1.5, height_m=1.5, threshold_m=3.3) == DRIED_OUT
    assert _status(depth_m=0.5, clearance_m=-0.5, height_m=2.5, threshold_m=3.3) == AGROUND
    assert _status(depth_m=1.2, clearance_m=0.2, height_m=3.2, threshold_m=3.3) == MARGIN
    assert _status(depth_m=1.5, clearance_m=0.5, height_m=3.5, threshold_m=3.3) == AFLOAT


def test_margin_rung_is_afloat_but_not_accessible():
    """The band the whole two-line design exists for: keel off the bottom,
    still short of the access threshold."""
    state = _status(depth_m=1.2, clearance_m=0.2, height_m=3.2, threshold_m=3.3)
    assert state == MARGIN
    assert state not in (AGROUND, DRIED_OUT), "keel is off the bottom"
    assert state != AFLOAT, "still inside the safety margin"


# --- crossings --------------------------------------------------------

def _window(start, end, **flags):
    w = {
        "hw_timestamp": "2026-07-16T12:00:00Z",
        "hw_height_m": 4.8,
        "start_time": start,
        "end_time": end,
    }
    w.update(flags)
    return w


def test_below_threshold_window_yields_no_crossings():
    ups, downs = _crossings([_window(None, None, below_threshold=True)])
    assert ups == [] and downs == []


def test_always_accessible_edges_are_not_crossings():
    """Its edges are LW troughs, not threshold crossings. Reading the end as a
    down-crossing would invent a grounding that never happens."""
    w = _window("2026-07-16T06:00:00Z", "2026-07-16T18:00:00Z",
                always_accessible=True)
    ups, downs = _crossings([w])
    assert ups == [] and downs == []


def test_incomplete_window_keeps_its_real_edge():
    """A window with an end but no start means the tide never rose through the
    line -- it was already above it. The end is a genuine touch-down time."""
    w = _window(None, "2026-07-17T05:53:00Z", incomplete_data=True)
    ups, downs = _crossings([w])
    assert ups == []
    assert len(downs) == 1
    assert downs[0][0] == datetime(2026, 7, 17, 5, 53, tzinfo=timezone.utc)


# --- governing transition --------------------------------------------

def test_above_the_line_reports_the_next_close():
    w = _window("2026-07-16T10:00:00Z", "2026-07-16T15:00:00Z")
    t = _governing([w], dt(12), above=True)
    assert t.kind == "closes"
    assert t.at == dt(15)


def test_below_the_line_reports_the_next_open():
    w = _window("2026-07-16T22:00:00Z", "2026-07-17T03:00:00Z")
    t = _governing([w], dt(17), above=False)
    assert t.kind == "opens"
    assert t.at == dt(22)


def test_above_the_line_with_no_start_still_reports_touch_down():
    """A barely-covering mooring: the boat stayed afloat through a LW that did
    not quite ground her, so the window has no start crossing. The touch-down
    time must still be reported rather than discarded with the missing start."""
    w = _window(None, "2026-07-17T05:53:00Z", incomplete_data=True)
    t = _governing([w], dt(17), above=True)
    assert t is not None, "a known end must not be discarded with the missing start"
    assert t.kind == "closes"


def test_always_accessible_containing_now_reports_no_crossing():
    w = _window("2026-07-16T06:00:00Z", "2026-07-16T18:00:00Z",
                always_accessible=True)
    t = _governing([w], dt(12), above=True)
    assert t.kind == "none"
    assert t.at is None


def test_side_is_taken_from_height_not_window_membership():
    """Same window, same instant: only the measured side differs, and the
    answer must follow the height."""
    w = _window("2026-07-16T10:00:00Z", "2026-07-16T15:00:00Z")
    assert _governing([w], dt(12), above=True).kind == "closes"
    assert _governing([w], dt(12), above=False) is None  # no open ahead


# --- the near-LW warning ---------------------------------------------

def _events(lw_height):
    return [
        {"timestamp": "2026-07-16T17:00:00Z", "event_type": "LowWater",
         "height_m": lw_height},
        {"timestamp": "2026-07-16T23:00:00Z", "event_type": "HighWater",
         "height_m": 4.8},
    ]


def test_warns_when_access_line_sits_near_low_water():
    from moorwatch.state import Transition
    t = Transition(kind="opens", at=dt(19),
                   window=_window("2026-07-16T19:00:00Z", "2026-07-17T03:00:00Z"))
    warning = _near_lw_warning(_events(1.0), t, threshold_m=1.3)
    assert warning is not None
    assert "0.30 m above low water" in warning


def test_no_warning_for_a_mooring_that_properly_dries():
    from moorwatch.state import Transition
    t = Transition(kind="opens", at=dt(19),
                   window=_window("2026-07-16T19:00:00Z", "2026-07-17T03:00:00Z"))
    assert _near_lw_warning(_events(0.9), t, threshold_m=3.3) is None


def test_no_warning_when_the_skipper_is_not_about_to_act_on_a_start():
    """Only the START is biased by the LW error; a closing window is unaffected."""
    from moorwatch.state import Transition
    t = Transition(kind="closes", at=dt(19),
                   window=_window("2026-07-16T10:00:00Z", "2026-07-16T19:00:00Z"))
    assert _near_lw_warning(_events(1.0), t, threshold_m=1.3) is None


# --- end to end -------------------------------------------------------

def test_compute_state_is_deterministic_for_a_given_instant():
    """The countdown must tick down, not jitter. See EVENT_STEP_MINUTES."""
    c = cfg()
    a = compute_state(c, dt(17, 11))
    b = compute_state(c, dt(17, 11))
    assert a.height_cd_m == b.height_cd_m
    assert a.transition.at == b.transition.at


def test_countdown_does_not_run_backwards_as_now_advances():
    c = cfg()
    seconds = []
    for minute in range(0, 10, 2):
        state = compute_state(c, dt(17, minute))
        assert state.transition is not None
        seconds.append(state.transition.seconds_from(state.now))
    assert seconds == sorted(seconds, reverse=True), (
        f"countdown went up: {seconds}"
    )


def test_dried_out_mooring_reports_negative_depth_and_a_float_time():
    state = compute_state(cfg(drying_height_m=2.0), dt(17, 11))
    assert state.status == DRIED_OUT
    assert state.depth_m < 0
    assert state.dried_out
    assert not state.afloat
    assert state.float_transition is not None
    assert state.float_transition.kind == "opens"


def test_float_threshold_leads_the_access_threshold():
    """The boat lifts before it has access -- never the other way round."""
    state = compute_state(cfg(drying_height_m=2.0), dt(17, 11))
    assert state.float_threshold_m < state.threshold_m
    assert state.float_transition.at < state.transition.at


def test_unreachable_threshold_reports_no_window_rather_than_crashing():
    state = compute_state(cfg(draught_m=9.0), dt(17, 11))
    assert state.transition is None
    assert "No access window" in state.note
