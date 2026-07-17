"""Pure formatting for the readout. Imports no Tk -- the CLI and the GUI share it.

Everything here turns a MooringState into words. Two rules it exists to enforce:

  * A dried-out mooring has no depth to report. "-0.86 m of water" is not a
    depth, it is a seabed level; it reads as a number when it is really a state.
  * Times are shown in the vessel's local zone because that is what the skipper
    reads off a watch, but every value computed upstream is UTC. Convert only
    here, at the edge -- the same split app/config.py's to_utc_str enforces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from moorwatch.state import AFLOAT, AGROUND, DRIED_OUT, MARGIN, MooringState

_STATUS_LABELS = {
    DRIED_OUT: "DRIED OUT",
    AGROUND: "AGROUND",
    MARGIN: "IN MARGIN",
    AFLOAT: "AFLOAT",
}

_STATUS_DETAIL = {
    DRIED_OUT: "the mooring is dry",
    AGROUND: "keel is on the bottom",
    MARGIN: "afloat, but inside the safety margin",
    AFLOAT: "clear to move",
}


def tzinfo_for(name: str):
    """The vessel's display zone, falling back to UTC if tzdata is absent.

    A missing zone must not stop the readout: the depth is still right, and on a
    minimal Debian install (no tzdata) UTC is a survivable answer where a crash
    is not.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def status_label(state: MooringState) -> str:
    return _STATUS_LABELS.get(state.status, state.status.upper())


def status_detail(state: MooringState) -> str:
    return _STATUS_DETAIL.get(state.status, "")


def depth_text(state: MooringState) -> str:
    """Depth of water over the seabed, or the honest absence of it."""
    if state.dried_out:
        return "dried out"
    return f"{state.depth_m:.2f} m"


def clearance_text(state: MooringState) -> str:
    """Water under the keel, annotated. Negative means aground by that much.

    For the CLI, where a reader scanning the numbers column has no other cue.
    """
    if state.clearance_m < 0:
        return f"{state.clearance_m:.2f} m (aground)"
    return f"{state.clearance_m:.2f} m"


def clearance_value(state: MooringState) -> str:
    """Water under the keel, bare.

    For the GUI, which already says AGROUND in 26pt directly above: the "(aground)"
    suffix is redundant there, and it is what pushed the detail line past the
    800px design floor and clipped the access-line figure off the right edge.
    """
    return f"{state.clearance_m:.2f} m"


def format_time(dt: Optional[datetime], tz) -> str:
    """A wall-clock time in the vessel's zone, e.g. "23:27 BST"."""
    if dt is None:
        return "--:--"
    local = dt.astimezone(tz)
    return f"{local:%H:%M} {local:%Z}".strip()


def format_datetime(dt: Optional[datetime], tz) -> str:
    """A full local stamp, e.g. "Tue 16 Jul 2026, 17:11 BST".

    The day is interpolated rather than using %-d, which is a glibc extension:
    it works on the Debian target but raises on a Windows dev machine.
    """
    if dt is None:
        return "--"
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b %Y}, {local:%H:%M %Z}"


def format_duration(seconds: Optional[float]) -> str:
    """A countdown a skipper can act on: "5h 16m", "42m", "2d 5h".

    Rounds to the minute. The model's own timing accuracy is ~15-20 minutes, so
    seconds would be invented precision; days are shown coarsely because a
    two-day wait does not need its minutes.
    """
    if seconds is None:
        return "--"
    if seconds < 0:
        return "now"
    minutes = int(seconds // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"


def _shown_at(transition, display_window) -> Optional[datetime]:
    """The transition instant as it should be *shown*.

    Prefers the display-rounded window edge over the raw one, for two reasons:
    the feed rounds the same way, so the netbook and the calendar agree to the
    minute rather than differing by a confusing three minutes; and inward
    rounding always errs the safe way (opens later, closes earlier). Falls back
    to the raw edge when the window has no display form -- a collapsed or
    always-accessible window.

    Rounding stays here in render, never in state: window_display.py is
    explicit that it is render-only and computation keeps full precision.
    """
    if transition is None:
        return None
    if display_window is not None:
        start, end = display_window
        if transition.kind == "opens":
            return start
        if transition.kind == "closes":
            return end
    return transition.at


def transition_at(state: MooringState) -> Optional[datetime]:
    """When access next changes (the ICS feed's line)."""
    return _shown_at(state.transition, state.display_window)


def float_at(state: MooringState) -> Optional[datetime]:
    """When the keel next lifts or touches (the physical line)."""
    return _shown_at(state.float_transition, state.float_display_window)


def _countdown(at: Optional[datetime], now: datetime) -> str:
    return format_duration((at - now).total_seconds() if at else None)


def float_line(state: MooringState, tz) -> str:
    """"How long until the boat is afloat?" -- the physical question.

    Empty when it adds nothing: an always-afloat mooring, or a state where the
    keel is not about to change what it is doing.
    """
    t = state.float_transition
    if t is None or t.kind == "none":
        return ""
    at = float_at(state)
    verb = "Lifts off at" if t.kind == "opens" else "Touches down at"
    return f"{verb} {format_time(at, tz)} - in {_countdown(at, state.now)}"


def transition_line(state: MooringState, tz) -> str:
    """"When can I move?" -- the access question, matching the feed.

    Says "access", not "floats": this line is the drying+draught+margin
    crossing, which is not the moment the boat lifts. See MooringState.
    """
    t = state.transition
    if t is None:
        return "No access window found."
    if t.kind == "none":
        return "Access all cycle - tide never drops below the line."
    at = transition_at(state)
    verb = "Access from" if t.kind == "opens" else "Access until"
    return f"{verb} {format_time(at, tz)} - in {_countdown(at, state.now)}"


def window_line(state: MooringState, tz) -> str:
    """The governing access window, on the same 5-minute display grid the ICS
    feed uses, so the two agree to the minute."""
    if state.negligible_access:
        return "Access window too short to show."
    if state.display_window is None:
        return ""
    start, end = state.display_window
    span = format_duration((end - start).total_seconds())
    return f"Window {format_time(start, tz)} - {format_time(end, tz)}  ({span})"


def config_age_line(cfg, now: Optional[datetime] = None) -> str:
    """Surface the config's age. See moorwatch/config.py for why this is not
    a detail: a stale drying height is wrong silently and confidently."""
    age = cfg.config_age_days(now)
    if age is None:
        return "Config never synced - drying height may not be the calibrated one."
    if cfg.is_stale(now):
        return f"Config {int(age)} days old - sync ashore (python3 -m moorwatch --sync)."
    return f"Config {int(age)} days old."


def render_cli(state: MooringState, cfg) -> str:
    """The whole readout as text. The GUI shows the same values, laid out."""
    tz = tzinfo_for(cfg.timezone)
    name = cfg.boat_name or f"Mooring {cfg.mooring_id}"

    lines = [
        f"{name} - {format_datetime(state.now, tz)}",
        "",
        f"  {status_label(state):<12}  {status_detail(state)}",
        "",
        f"  Depth of water   {depth_text(state):>16}",
        f"  Under the keel   {clearance_text(state):>16}",
        f"  Height above CD  {state.height_cd_m:>14.2f} m",
        f"  Access threshold {state.threshold_m:>14.2f} m",
        "",
    ]

    afloat = float_line(state, tz)
    if afloat:
        lines.append(f"  {afloat}")
    lines.append(f"  {transition_line(state, tz)}")

    window = window_line(state, tz)
    if window:
        lines.append(f"  {window}")
    if state.note:
        lines += ["", f"  {state.note}"]
    for warning in state.warnings:
        lines += ["", f"  ! {warning}"]

    lines += [
        "",
        "  Harmonic model, no barometric correction.",
        f"  {config_age_line(cfg, state.now)}",
    ]
    return "\n".join(lines)
