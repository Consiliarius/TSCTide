"""The float question, answered for a single instant.

``compute_state`` is the whole tool. Everything the display shows comes from one
call; the CLI and the GUI are renderers over it. It is pure apart from reading
the bundled model config, so it can be driven at any instant for testing.

Height path -- the load-bearing decision
----------------------------------------
Height comes from ``access_calc.interpolate_height_at_time`` over the SAME
events list the windows are computed from, never from
``harmonic.predict_height_at_time``. The two disagree: ``compute_access_windows``
derives every height internally through the Langstone asymmetric curve laid over
Admiralty-shifted event times (``_find_crossing`` -> ``_interpolate_from_parsed``
-> ``_curve_interpolate``), whereas the raw harmonic is the unshifted
mathematical curve. Measured over a full cycle at 15-minute steps:

    RMS difference 0.184 m, worst +0.37 m

which exceeds a typical 0.3 m safety margin. Taking the raw harmonic height here
would let the display read "afloat" while the countdown -- derived from the
windows -- still showed twenty minutes to go. The two numbers must come from one
model or they will contradict each other on the number under the keel.

Data source
-----------
Harmonic only. UKHO is Langstone-native and better, but it is a 7-day
network fetch and this tool runs with no connectivity at the mooring. The
harmonic model is the offline-native source and is what ``source="harmonic"``
means to the window engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import access_calc, harmonic, secondary_port
from app.config import to_utc_str
from app.window_display import round_window_conservative

from moorwatch.config import VesselConfig

# How far either side of `now` to predict events.
#
# Back: one full tidal cycle (12.42 h) plus margin, so the LW bracketing the
# current or previous HW is always present -- both the curve interpolation and
# compute_access_windows' always-accessible test need it.
# Forward: enough for the next window plus its end, comfortably.
EVENTS_BACK_HOURS = 14
EVENTS_FWD_HOURS = 30

# On neaps a mooring can go days without HW clearing the threshold at all
# (every window comes back below_threshold). When the default range finds no
# usable window, widen a day at a time rather than reporting "unknown" for a
# situation the model can answer perfectly well.
EVENTS_WIDEN_STEP_HOURS = 24
EVENTS_MAX_FWD_HOURS = 7 * 24

# How far the access threshold must sit above the bracketing low water before
# the window's START time can be trusted.
#
# The start crossing happens on the flood. Low in the flood the Langstone curve
# is deliberately flat -- model_config.json's young-flood stand holds the water
# nearly level for the first 60 minutes after LW -- so a small height error
# there becomes a large timing error. The harmonic model has exactly such an
# error: measured against this container's UKHO data over four cycles in July
# 2026, its LW heights run +0.143 m HIGH (and its HW heights -0.105 m low).
# Phantom water at LW makes the threshold appear to be crossed early.
#
# Measured effect on the window start, harmonic vs UKHO, 7 days, draught 1.0 m,
# margin 0.3 m, varying only the drying height:
#
#     threshold 0.40 m above LW -> start  -26.3 min mean,  -73.6 min worst
#     threshold 0.90 m above LW -> start   -7.6 min mean
#     threshold 1.40 m above LW -> start   -4.7 min mean
#     threshold 2.40 m above LW -> start   +1.0 min mean,  +25.2 min worst
#
# Re-measured after the golden-section refinement landed in predict_events, and
# unchanged by it to within 0.2 min: that fix removed a zero-mean TIMING
# artifact, whereas this is a HEIGHT error in the constituents. The two are
# orthogonal, and the height biases above were identical before and after.
#
# Early is the UNSAFE direction: it says there is water to leave on before there
# is. A mooring that properly dries is unaffected (its threshold sits well up
# the steep part of the flood); one that barely covers at LW is badly affected.
# The readout cannot fix this without UKHO data, so it says so instead.
NEAR_LW_WARNING_M = 1.0

# Status ladder, shallowest first.
DRIED_OUT = "dried_out"   # no water over the seabed at all
AGROUND = "aground"       # water, but not enough to lift the keel
MARGIN = "margin"         # keel lifted, but inside the safety margin
AFLOAT = "afloat"         # above TSCTide's access threshold


@dataclass(frozen=True)
class Transition:
    """The next crossing of a threshold, and the window that implies it.

    ``kind`` is "opens" (below the line now, it is crossed at ``at``), "closes"
    (above the line now, it is crossed at ``at``), or "none" (the tide never
    crosses this line during the cycle).

    Deliberately not called "floats"/"grounds": a Transition describes whichever
    threshold it was computed against, and there are two. See the note on
    MooringState.float_transition.
    """

    kind: str
    at: Optional[datetime]
    window: dict

    def seconds_from(self, now: datetime) -> Optional[float]:
        if self.at is None:
            return None
        return (self.at - now).total_seconds()


@dataclass(frozen=True)
class MooringState:
    """Everything the display needs, for one instant.

    Two thresholds, two transitions -- the distinction is the whole point:

      * ``transition`` is the ACCESS line (drying + draught + margin), the one
        compute_access_windows uses and the one the ICS feed publishes.
      * ``float_transition`` is the FLOAT line (drying + draught, margin zero):
        when the keel physically lifts.

    The boat lifts before it has access -- by roughly 15-20 minutes on a
    flooding spring, since the margin is ~0.3 m and the tide rises ~1 m/h. A
    single countdown cannot honestly be labelled both. Reporting the access
    edge as "floats at" would tell a skipper the boat is aground when it is
    already swinging; reporting the float edge as the access time would
    contradict the calendar and hand back the safety margin.
    """

    now: datetime
    height_cd_m: float          # tide height above Chart Datum
    depth_m: float              # water over the seabed: height - drying_height
    clearance_m: float          # under the keel: depth - draught
    threshold_m: float          # drying + draught + margin (the access line)
    float_threshold_m: float    # drying + draught       (the float line)
    status: str                 # one of the ladder constants above
    transition: Optional[Transition]          # against the access line
    float_transition: Optional[Transition]    # against the float line
    window: Optional[dict]      # governing access window, raw / full precision
    display_window: Optional[tuple[datetime, datetime]]   # rounded for display
    float_display_window: Optional[tuple[datetime, datetime]]
    negligible_access: bool     # window collapsed under the rounding grid
    note: str                   # why, when the state is unusual
    warnings: tuple[str, ...] = ()   # model limitations that bite this mooring

    @property
    def accessible(self) -> bool:
        """True when TSCTide would call the mooring accessible -- i.e. this
        instant lies inside an access window as the ICS feed means it."""
        return self.height_cd_m > self.threshold_m

    @property
    def afloat(self) -> bool:
        """True when the keel is physically off the bottom, margin or not."""
        return self.clearance_m >= 0

    @property
    def dried_out(self) -> bool:
        return self.depth_m <= 0


def _tide_events(start: datetime, end: datetime) -> list[dict]:
    """Langstone HW/LW events over a range: harmonic, secondary-port corrected.

    Portsmouth-native harmonic output shifted to Langstone via the one
    authoritative offset (app/secondary_port.py). Do not shift anywhere else.

    The default step_min is correct here despite this tool recomputing against
    a moving `now`: since the golden-section refinement landed, predict_events
    converges on each turning point against the curve itself, so event times no
    longer depend on where the sampling grid falls. Passing a finer step buys
    the identical answer for several times the work.
    """
    return secondary_port.apply_offset(harmonic.predict_events(start, end))


def _parse(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-Z timestamp as written by app.config.to_utc_str."""
    if not ts:
        return None
    iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _crossings(windows: list[dict]) -> tuple[list, list]:
    """Every real threshold crossing in a window set, as (ups, downs).

    A window edge is a crossing found by ``_find_crossing``, so it is a genuine
    time at which the tide passes the threshold -- EXCEPT in two cases the
    engine flags, which must not be read as crossings:

      * ``below_threshold``: both edges are None; HW never reaches the line.
      * ``always_accessible``: the edges are the bracketing LW troughs, not
        crossings. The tide never touches the line at all during that cycle;
        treating its "end" as a down-crossing would invent a grounding.

    ``incomplete_data`` windows are deliberately KEPT. A window with a real end
    and no start means the tide never rose through the line within the 7-hour
    backward search -- because it was already above it. That is not missing
    data, it is a boat that has simply been afloat, and its end is a real
    touch-down time worth showing. Discarding those loses the answer entirely
    for a mooring whose line sits near the LW height, where some cycles never
    ground the boat.
    """
    ups, downs = [], []
    for w in windows:
        if w.get("below_threshold") or w.get("always_accessible"):
            continue
        start, end = _parse(w.get("start_time")), _parse(w.get("end_time"))
        if start is not None:
            ups.append((start, w))
        if end is not None:
            downs.append((end, w))
    ups.sort(key=lambda pair: pair[0])
    downs.sort(key=lambda pair: pair[0])
    return ups, downs


def _governing(windows: list[dict], now: datetime, above: bool) -> Optional[Transition]:
    """The next crossing of this window set's threshold.

    ``above`` says which side of the line the tide is on *now*, taken from the
    measured height rather than inferred from window membership. That matters:
    a boat can be above the line with no window containing it (the window's
    start crossing does not exist, because the tide never came up through the
    line -- it was already there). Deciding the side from the height and using
    the windows only to find the crossing time handles that without a special
    case, and cannot disagree with the depth shown beside it.
    """
    ups, downs = _crossings(windows)

    if above:
        for at, w in downs:
            if at >= now:
                return Transition(kind="closes", at=at, window=w)
        # No crossing ahead. If an always-accessible cycle brackets us, that is
        # the answer -- the tide genuinely never reaches the line.
        for w in windows:
            if not w.get("always_accessible"):
                continue
            start, end = _parse(w.get("start_time")), _parse(w.get("end_time"))
            if start and end and start <= now <= end:
                return Transition(kind="none", at=None, window=w)
        return None

    for at, w in ups:
        if at > now:
            return Transition(kind="opens", at=at, window=w)
    return None


def _windows_for(events: list[dict], cfg: VesselConfig, margin_m: float) -> list[dict]:
    """Windows against a given safety margin. margin=0 gives the float line."""
    return access_calc.compute_access_windows(
        events,
        cfg.draught_m,
        cfg.drying_height_m,
        margin_m,
        source="harmonic",
    )


def _rounded(w: Optional[dict]) -> Optional[tuple[datetime, datetime]]:
    """Display-grid edges for a genuine bounded window, else None.

    Special states are not window edges and must not be rounded -- see
    window_display.round_window_conservative's own contract.
    """
    if w is None or w.get("always_accessible"):
        return None
    return round_window_conservative(w["start_time"], w["end_time"])


def _lw_before(events: list[dict], target: datetime) -> Optional[dict]:
    """The last low water at or before ``target``."""
    lws = [
        e for e in events
        if e["event_type"] == "LowWater" and _parse(e["timestamp"]) <= target
    ]
    return max(lws, key=lambda e: _parse(e["timestamp"])) if lws else None


def _near_lw_warning(events: list[dict], transition: Optional[Transition],
                     threshold_m: float) -> Optional[str]:
    """Warn when the access threshold sits low enough in the flood that the
    harmonic model's LW bias makes the start time unreliable and early.

    See NEAR_LW_WARNING_M for the measurements. Only the START is affected, so
    this is raised only when a start is what the skipper is about to act on.
    """
    if transition is None or transition.kind != "opens":
        return None
    start = _parse(transition.window.get("start_time"))
    if start is None:
        return None
    lw = _lw_before(events, start)
    if lw is None:
        return None
    above_lw = threshold_m - lw["height_m"]
    if above_lw >= NEAR_LW_WARNING_M:
        return None
    return (
        f"Access line sits only {above_lw:.2f} m above low water, in the flat "
        f"part of the flood. The harmonic model reads high at low water, so "
        f"this start time is optimistic - it can be up to an hour early. "
        f"Treat it as the earliest possible, not a time to leave on."
    )


def _status(depth_m: float, clearance_m: float, height_m: float,
            threshold_m: float) -> str:
    """Place the current water on the ladder.

    The three lines are distinct and the display shows all of them: the boat can
    be physically afloat while still inside the safety margin, which is neither
    "aground" nor what the ICS feed calls accessible.
    """
    if depth_m <= 0:
        return DRIED_OUT
    if clearance_m < 0:
        return AGROUND
    if height_m <= threshold_m:
        return MARGIN
    return AFLOAT


def compute_state(cfg: VesselConfig, now: Optional[datetime] = None) -> MooringState:
    """Answer the float question for ``now`` (default: this instant, UTC)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    float_threshold = cfg.drying_height_m + cfg.draught_m
    now_iso = to_utc_str(now)

    fwd = EVENTS_FWD_HOURS
    while True:
        events = _tide_events(
            now - timedelta(hours=EVENTS_BACK_HOURS), now + timedelta(hours=fwd)
        )
        height = access_calc.interpolate_height_at_time(now_iso, events)
        if height is None:
            # Only reachable if `now` fell outside the event span, which the
            # bracketing range above is chosen to prevent.
            raise RuntimeError(
                f"No tide events bracket {now_iso} despite a "
                f"{EVENTS_BACK_HOURS + fwd}h event span."
            )
        windows = _windows_for(events, cfg, cfg.safety_margin_m)
        transition = _governing(windows, now, above=height > cfg.threshold_m)
        if transition is not None or fwd >= EVENTS_MAX_FWD_HOURS:
            break
        fwd = min(fwd + EVENTS_WIDEN_STEP_HOURS, EVENTS_MAX_FWD_HOURS)

    # The float line, from the same events: identical geometry, zero margin.
    float_transition = _governing(
        _windows_for(events, cfg, 0.0), now, above=height > float_threshold
    )

    depth = height - cfg.drying_height_m
    clearance = depth - cfg.draught_m
    status = _status(depth, clearance, height, cfg.threshold_m)

    window = transition.window if transition else None
    display_window = _rounded(window)
    float_display_window = _rounded(
        float_transition.window if float_transition else None
    )

    # Conservative-inward rounding can collapse a genuine but tiny window. It is
    # real water, but under a grid the model's own timing accuracy (~15-20 min)
    # cannot support -- so it is shown as a warning, not as times.
    negligible = (
        window is not None
        and not window.get("always_accessible")
        and display_window is None
    )

    note = ""
    if transition is None:
        note = (
            f"No access window within {EVENTS_MAX_FWD_HOURS // 24} days: high "
            f"water never reaches {cfg.threshold_m:.2f} m above chart datum."
        )
    elif transition.kind == "none":
        note = "Tide does not drop below the access line this cycle."
    elif negligible:
        note = "Window too short to show at the display's 5-minute resolution."

    warnings = tuple(
        w for w in (_near_lw_warning(events, transition, cfg.threshold_m),) if w
    )

    return MooringState(
        now=now,
        height_cd_m=height,
        depth_m=depth,
        clearance_m=clearance,
        threshold_m=cfg.threshold_m,
        float_threshold_m=float_threshold,
        status=status,
        transition=transition,
        float_transition=float_transition,
        window=window,
        display_window=display_window,
        float_display_window=float_display_window,
        negligible_access=negligible,
        note=note,
        warnings=warnings,
    )
