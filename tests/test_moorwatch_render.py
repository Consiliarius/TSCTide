"""Wording that depends on which side of a threshold the boat is on.

These read as cosmetic and are not. "Current window" and "Next window" describe
opposite situations — inside the access period, or waiting on it — and the
condition that picks between them is a single comparison that would invert
silently. Nothing would crash; the readout would simply lie about which window
it is showing.
"""

import sys
from datetime import datetime, timezone
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
    """The window line and the access row read the same transition, so "Current
    window" can only appear beside "Access ends at", never "Access starts at"."""
    c = cfg()
    tz = render.tzinfo_for(c.timezone)
    for hour in range(0, 24, 2):
        state = compute_state(c, at(hour))
        line = render.window_line(state, tz)
        if not line or state.negligible_access:
            continue
        label, _ = render.access_row(state, tz)
        if line.startswith("Current window"):
            assert label == "Access ends at:", (hour, line, label)
        else:
            assert label == "Access starts at:", (hour, line, label)


def test_window_line_still_carries_both_edges_and_a_duration():
    _, line = _line(at(6))
    assert "-" in line and "(" in line and ")" in line
