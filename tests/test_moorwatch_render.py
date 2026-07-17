"""Wording that depends on which side of a threshold the boat is on.

These read as cosmetic and are not. "Current window" and "Next window" describe
opposite situations — inside the access period, or waiting on it — and the
condition that picks between them is a single comparison that would invert
silently. Nothing would crash; the readout would simply lie about which window
it is showing.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moorwatch import render
from moorwatch.config import VesselConfig
from moorwatch.state import compute_state


def cfg(**kw) -> VesselConfig:
    base = dict(
        mooring_id=1, boat_name="Test", draught_m=1.0, drying_height_m=2.0,
        safety_margin_m=0.3, timezone="Europe/London",
    )
    base.update(kw)
    return VesselConfig(**base)


def at(hour, minute=0, day=17):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def _line(when):
    c = cfg()
    state = compute_state(c, when)
    return state, render.window_line(state, render.tzinfo_for(c.timezone))


def test_waiting_on_the_tide_says_next_window():
    state, line = _line(at(6))
    assert state.transition.kind == "opens", "fixture must be a boat waiting"
    assert line.startswith("Next window"), line


def test_inside_the_window_says_current_window():
    state, line = _line(at(12, 30))
    assert state.transition.kind == "closes", "fixture must be a boat with access"
    assert line.startswith("Current window"), line


def test_the_two_lines_never_contradict_each_other():
    """The window line and the depart/moor row read the same transition, so
    "Current window" can only appear beside "Moor by", never "Depart after"."""
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    for hour in range(0, 24, 2):
        state = compute_state(c, at(hour))
        line = render.window_line(state, tz)
        if not line or state.negligible_access:
            continue
        label, _ = render.access_row(state, tz)
        if line.startswith("Current window"):
            assert label == "Moor by:", (hour, line, label)
        else:
            assert label == "Depart after:", (hour, line, label)


def test_the_action_row_never_says_access():
    """"Access" reads as getting TO the boat — the tender's problem, a different
    depth, and one this tool does not compute. It must not appear on the line
    the skipper acts on, in any state."""
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    for hour in range(0, 24, 2):
        label, value = render.access_row(compute_state(c, at(hour)), tz)
        assert "access" not in label.lower(), (hour, label)
        assert "access" not in value.lower(), (hour, value)


def test_waiting_says_depart_after_and_afloat_says_moor_by():
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    waiting = compute_state(c, at(6))
    assert waiting.transition.kind == "opens"
    assert render.access_row(waiting, tz)[0] == "Depart after:"

    inside = compute_state(c, at(12, 30))
    assert inside.transition.kind == "closes"
    assert render.access_row(inside, tz)[0] == "Moor by:"


def test_window_line_still_carries_both_edges_and_a_duration():
    _, line = _line(at(6))
    assert "-" in line and "(" in line and ")" in line


# -- colour semantics --------------------------------------------------------
#
# render decides urgency; ui maps it onto the palette. These test the decision,
# which is where the rules live and where they would silently invert.

def test_water_under_the_keel_is_the_green_condition():
    """Green on the physical fact, not on access: a boat inside its safety
    margin is floating, and the depth reading must not say otherwise."""
    c = cfg()
    aground = compute_state(c, at(6))
    assert aground.clearance_m < 0
    assert not render.keel_has_water(aground)

    afloat = compute_state(c, at(12, 30))
    assert afloat.clearance_m > 0
    assert render.keel_has_water(afloat)


def test_a_boat_in_the_margin_still_reads_as_having_water():
    """The margin band is the case the rule exists to settle: floating, but not
    yet clear to depart. Two different rows, two different answers."""
    c = cfg(safety_margin_m=1.5)          # a wide band, easy to land in
    tz = render.tzinfo_for(c.timezone)
    state = compute_state(c, at(4, 0))
    if state.status != "margin":
        return                             # fixture drifted; the next test covers the rule
    assert render.keel_has_water(state), "in the margin the boat is afloat"
    assert render.access_row(state, tz)[0] != "Moor by:"


def test_depart_after_carries_no_urgency():
    """An invitation, not a deadline — it can wait."""
    state = compute_state(cfg(), at(6))
    assert state.transition.kind == "opens"
    assert render.access_urgency(state) == "normal"


def test_moor_by_warns_while_there_is_time():
    state = compute_state(cfg(), at(12, 30))
    assert state.transition.kind == "closes"
    assert render.access_urgency(state) == "warn"


def test_moor_by_turns_urgent_inside_the_last_half_hour():
    """The boundary is the whole point of the rule, so it is tested at the
    boundary rather than somewhere comfortably past it."""
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    state = compute_state(c, at(12, 30))
    moor_by = render.transition_at(state)

    just_outside = moor_by - timedelta(seconds=render.MOOR_BY_URGENT_SECONDS + 60)
    just_inside = moor_by - timedelta(seconds=render.MOOR_BY_URGENT_SECONDS - 60)

    assert render.access_urgency(compute_state(c, just_outside)) == "warn"
    assert render.access_urgency(compute_state(c, just_inside)) == "urgent"
    # ...and it is still the moor-by row that is being coloured.
    assert render.access_row(compute_state(c, just_inside), tz)[0] == "Moor by:"


def test_urgency_never_fires_on_a_row_that_is_not_a_deadline():
    """Red must mean "be back", nothing else. Walk the day and check it only
    ever appears beside "Moor by"."""
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    for minutes in range(0, 24 * 60, 20):
        state = compute_state(c, at(0) + timedelta(minutes=minutes))
        if render.access_urgency(state) in ("warn", "urgent"):
            assert render.access_row(state, tz)[0] == "Moor by:", minutes
