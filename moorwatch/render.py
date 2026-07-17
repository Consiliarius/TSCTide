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

from moorwatch.state import MooringState

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


def title_text(cfg) -> str:
    """Window title: the boat, and the tide it needs.

    The access line lives here rather than in the body because it never changes
    — it is drying + draught + margin, all three from config. A number that is
    the same at every glance is chrome, not a reading.
    """
    name = cfg.boat_name or f"Mooring {cfg.mooring_id}"
    return f"Moorwatch - {name} - {cfg.threshold_m:g}m Tide Required"


def height_row(state: MooringState) -> tuple[str, str]:
    """The tide itself, as charted."""
    return ("Current Height of Tide:", f"{state.height_cd_m:.2f} m above CD")


def keel_row(state: MooringState) -> tuple[str, str]:
    """Water under the keel — the one number that says what the boat is doing.

    Negative means aground by that much. Reported next to the height of tide
    rather than in place of it: the two are different quantities (one is the sea
    level, one is the gap under this hull), and the drying height between them
    is what makes a positive tide and a negative clearance consistent.
    """
    return ("Est. under Keel:", f"{state.clearance_m:.2f} m at mooring")


def float_row(state: MooringState, tz) -> Optional[tuple[str, str]]:
    """When the keel next lifts or touches. None when it never does.

    The label carries the state: "Afloat at" can only mean the boat is aground
    now, and "Aground at" can only mean it is floating. Saying so a second time
    in a banner adds nothing.
    """
    t = state.float_transition
    if t is None or t.kind == "none":
        return None
    at = float_at(state)
    label = "Afloat at:" if t.kind == "opens" else "Aground at:"
    return (label, f"{format_time(at, tz)} - in {_countdown(at, state.now)}")


def access_row(state: MooringState, tz) -> tuple[str, str]:
    """When the boat may leave, or must be back — the line the feed publishes.

    Says "Depart" and "Moor", never "Access". This threshold is about the boat
    having water to move off the mooring and back onto it. "Access" reads just
    as naturally as getting *to* the boat, which is the tender's problem and a
    different depth entirely — TSCTide models that separately as
    tender_min_depth_m, and this tool does not compute it at all. A word that
    could mean either is the wrong word for the one line a skipper acts on.

    They are also actions rather than states, which is what the reader wants:
    not "the window opens at 11:55" but "you may go after 11:55".
    """
    t = state.transition
    if t is None:
        return ("Depart after:", "no window in the next 7 days")
    if t.kind == "none":
        return ("Depart or moor:", "any time - tide never drops below the line")
    at = transition_at(state)
    label = "Depart after:" if t.kind == "opens" else "Moor by:"
    return (label, f"{format_time(at, tz)} - in {_countdown(at, state.now)}")


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


def window_line(state: MooringState, tz) -> str:
    """The governing access window, on the same 5-minute display grid the ICS
    feed uses, so the two agree to the minute.

    "Current" or "Next" — because a bare pair of times does not say whether the
    boat is in that window or waiting on it, and those are opposite situations.
    The distinction is the same one the row above draws: a window that will
    CLOSE is one the boat is inside; one that will OPEN is one it is waiting on.
    Read from the transition rather than re-derived, so the two lines cannot
    contradict each other.
    """
    if state.negligible_access:
        return "Access window too short to show."
    if state.display_window is None:
        return ""
    start, end = state.display_window
    span = format_duration((end - start).total_seconds())
    inside = state.transition is not None and state.transition.kind == "closes"
    prefix = "Current window" if inside else "Next window"
    return f"{prefix} {format_time(start, tz)} - {format_time(end, tz)}  ({span})"


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
    """The whole readout as text. The same four readings the GUI shows, in the
    same words — one wording, so the two cannot drift apart."""
    tz = tzinfo_for(cfg.timezone)

    rows = [height_row(state), keel_row(state),
            float_row(state, tz), access_row(state, tz)]
    rows = [r for r in rows if r is not None]
    width = max(len(label) for label, _ in rows)

    lines = [
        title_text(cfg),
        f"  {format_datetime(state.now, tz)}",
        "",
    ]
    lines += [f"  {label:<{width}}  {value}" for label, value in rows]
    lines.append("")

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
