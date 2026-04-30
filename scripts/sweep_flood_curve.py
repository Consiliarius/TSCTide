"""
Parameter sweep over flood-phase tidal curve forms.

The current production flood curve is a pure half-cosine, which on the
16-day corpus has +0.134m mean bias and 0.290m RMS through the mid-flood
region (production overshoots the actual height). This script tests
bounded alternatives and reports their flood-only residuals, with the
ebb branch held fixed at the v2.5.2 production parameters so that any
change is attributable to the flood form alone.

Forms tested:

  cosine          (baseline)  t = (1 - cos(pi*f)) / 2
  halfcosine_pow  t = ((1 - cos(pi*f)) / 2) ^ p   for swept p
  lw_stand        Linear hold near LW, then cosine. Symmetric in role to
                  the existing ebb stand near HW; uses fraction of range
                  rather than fraction of absolute height (because LW
                  heights are small, so absolute fractions of LW would
                  be physically negligible).
  lw_stand_pow    Combination: LW stand + halfcosine_pow for the post-
                  stand portion. Run conditionally (see below).

Form 3 (lw_stand_pow) is run only if neither of the simpler forms brings
|mean bias| within +/-0.05m. It doubles the search dimensions, and its
results are only interesting when the simpler forms fall short.

Patching strategy:
  Monkey-patches access_calc._curve_interpolate with an instrumented
  replacement for the flood branch. The ebb branch is reproduced exactly
  as in production (reading stand_duration_minutes / stand_height_fraction
  from the cached curve params). No model_config.json edits.

Usage:
    docker exec tidal-access python -m scripts.sweep_flood_curve

Override defaults via env vars (comma-separated):
    SWEEP_PS=1.00,1.05,1.10,...
    SWEEP_DURS=20,30,40,50,60
    SWEEP_FRACS=0.03,0.05,0.08,0.10,0.15
    SWEEP_PS_F3=1.05,1.10,1.15            (form 3 only)
    SWEEP_DURS_F3=40                       (form 3 only)
    SWEEP_FRACS_F3=0.05,0.08,0.10          (form 3 only)
"""

from __future__ import annotations

import os
from datetime import datetime

# Reuse the loader and residual-bookkeeping helpers from the main
# calibration script. Keeps sweep behaviour consistent with the headline
# analysis and avoids duplicating the data-parsing logic.
from scripts.calibrate_from_ukho_week import (
    DATA_DIR,
    load_all_data, classify_phase, _bracket_too_wide,
    stats,
)

from app import access_calc
from app.access_calc import interpolate_height_at_time


# Save the production _curve_interpolate so the script can restore it at
# the end. Not strictly necessary for a short-lived process but keeps the
# script safe to import for ad-hoc analysis from a REPL.
_PRODUCTION_CURVE_INTERPOLATE = access_calc._curve_interpolate


def parse_list(env_value, default):
    """Parse a comma-separated env var override into floats/ints."""
    if not env_value:
        return default
    out = []
    for s in env_value.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(float(s) if "." in s else int(s))
        except ValueError:
            print(f"WARNING: ignoring unparseable sweep value '{s}'")
    return out or default


def _flood_t(form: str, fraction: float, total_seconds: float, **params) -> float:
    """
    Compute the smoothstep t for a given flood-curve form. t maps
    fraction-of-elapsed-time to fraction-of-height-rise; t in [0, 1].

    access_calc._cosine_interp(f, 0.0, 1.0) is used wherever the standard
    half-cosine smoothstep is wanted, so clamping behaviour at the edges
    matches production exactly.
    """
    if form == "cosine":
        return access_calc._cosine_interp(fraction, 0.0, 1.0)

    if form == "halfcosine_pow":
        base = access_calc._cosine_interp(fraction, 0.0, 1.0)
        return base ** params["p"]

    if form == "lw_stand":
        stand_minutes = params["stand_minutes"]
        rise_fraction = params["rise_fraction"]
        if total_seconds > 0:
            stand_proportion = (stand_minutes * 60.0) / total_seconds
        else:
            stand_proportion = 0.0
        # Cap stand at 50% of the flood span so the post-stand cosine
        # has room to reach HW. Combinations beyond this are unphysical
        # and should not be picked anyway; the cap just keeps the
        # arithmetic well-defined if a wide grid is supplied.
        stand_proportion = min(stand_proportion, 0.5)
        if fraction < stand_proportion:
            if stand_proportion <= 0:
                return 0.0
            return rise_fraction * (fraction / stand_proportion)
        adjusted = (fraction - stand_proportion) / (1.0 - stand_proportion)
        post_stand_t = access_calc._cosine_interp(adjusted, 0.0, 1.0)
        return rise_fraction + (1.0 - rise_fraction) * post_stand_t

    if form == "lw_stand_pow":
        stand_minutes = params["stand_minutes"]
        rise_fraction = params["rise_fraction"]
        p = params["p"]
        if total_seconds > 0:
            stand_proportion = (stand_minutes * 60.0) / total_seconds
        else:
            stand_proportion = 0.0
        stand_proportion = min(stand_proportion, 0.5)
        if fraction < stand_proportion:
            if stand_proportion <= 0:
                return 0.0
            return rise_fraction * (fraction / stand_proportion)
        adjusted = (fraction - stand_proportion) / (1.0 - stand_proportion)
        base = access_calc._cosine_interp(adjusted, 0.0, 1.0)
        return rise_fraction + (1.0 - rise_fraction) * (base ** p)

    raise ValueError(f"Unknown flood form: {form}")


def make_curve_interpolate(form: str, **flood_params):
    """
    Build a _curve_interpolate replacement that uses `form` for the flood
    branch and reproduces production behaviour for the ebb branch.

    The returned callable can be assigned to access_calc._curve_interpolate
    to redirect interpolate_height_at_time through the parameterized form.
    """
    def _curve_interpolate(target, before, after):
        curve = access_calc._get_curve_params()
        t_before, h_before, et_before = before
        t_after, h_after, et_after = after
        total_seconds = (t_after - t_before).total_seconds()
        if total_seconds <= 0:
            return h_before
        elapsed = (target - t_before).total_seconds()
        fraction = elapsed / total_seconds

        if et_before == "LowWater" and et_after == "HighWater":
            t = _flood_t(form, fraction, total_seconds, **flood_params)
            return h_before + (h_after - h_before) * t

        if et_before == "HighWater" and et_after == "LowWater":
            # Ebb: reproduce production behaviour exactly so the only
            # variable across the sweep is the flood form. The cached
            # curve params carry whatever the running container's
            # model_config.json holds (v2.5.2 defaults at time of writing).
            stand_mins = curve.get("stand_duration_minutes", 30)
            stand_frac = curve.get("stand_height_fraction", 0.95)
            if total_seconds > 0:
                stand_proportion = (stand_mins * 60) / total_seconds
            else:
                stand_proportion = 0
            if fraction < stand_proportion:
                stand_drop = h_before * (1 - stand_frac)
                return h_before - stand_drop * (fraction / stand_proportion)
            adjusted = (fraction - stand_proportion) / (1 - stand_proportion)
            stand_height = h_before * stand_frac
            return access_calc._cosine_interp(1 - adjusted, h_after, stand_height)

        # Same event types either side - linear fallback (production).
        return h_before + (h_after - h_before) * fraction

    return _curve_interpolate


def run_iteration(samples: list[dict], events: list[dict],
                  form: str, **flood_params) -> dict:
    """
    Run one residual pass for the given flood form. Returns a dict with
    flood / ebb / all stats. The same anchor-skip and bracket-width
    filters as run_curve_test in calibrate_from_ukho_week.py are applied
    so the residuals are directly comparable.
    """
    access_calc._curve_interpolate = make_curve_interpolate(form, **flood_params)

    flood: list[float] = []
    ebb: list[float] = []
    all_resid: list[float] = []

    event_dts = {
        datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        for ev in events
    }

    for s in samples:
        if any(abs((s["dt_utc"] - et).total_seconds()) < 180 for et in event_dts):
            continue
        if _bracket_too_wide(s["dt_utc"], events):
            continue
        target_iso = s["dt_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
        predicted = interpolate_height_at_time(target_iso, events)
        if predicted is None:
            continue
        residual = predicted - s["height_m"]
        all_resid.append(residual)
        phase = classify_phase(s["dt_utc"], events)
        if phase == "flood":
            flood.append(residual)
        elif phase == "ebb":
            ebb.append(residual)

    return {
        "flood": stats(flood),
        "ebb": stats(ebb),
        "all": stats(all_resid),
    }


LABEL_WIDTH = 28


def _print_header() -> None:
    print()
    print("=" * (LABEL_WIDTH + 56))
    print(
        f"{'parameters':>{LABEL_WIDTH}}  "
        f"{'flood_mean':>10} {'flood_RMS':>10} {'flood_max':>10}  "
        f"{'ebb_RMS':>9}"
    )
    print("=" * (LABEL_WIDTH + 56))


def _print_row(label: str, r: dict) -> None:
    print(
        f"{label:>{LABEL_WIDTH}}  "
        f"{r['flood']['mean']:>+10.3f} "
        f"{r['flood']['rms']:>10.3f} "
        f"{r['flood']['max_abs']:>10.2f}  "
        f"{r['ebb']['rms']:>9.3f}"
    )


def main() -> int:
    pow_ps = parse_list(
        os.environ.get("SWEEP_PS"),
        [0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30],
    )
    stand_durs = parse_list(
        os.environ.get("SWEEP_DURS"),
        [20, 30, 40, 50, 60],
    )
    stand_fracs = parse_list(
        os.environ.get("SWEEP_FRACS"),
        [0.03, 0.05, 0.08, 0.10, 0.15],
    )

    print(f"\nLoading calibration data from: {DATA_DIR}\n")
    samples, events = load_all_data()
    if not samples:
        print("No data loaded. Exiting.")
        return 2
    print(f"\nCorpus: {len(samples)} samples, {len(events)} events")
    print(
        f"Form 1 (halfcosine_pow): {len(pow_ps)} p values\n"
        f"Form 2 (lw_stand):       "
        f"{len(stand_durs)} x {len(stand_fracs)} = "
        f"{len(stand_durs) * len(stand_fracs)} (duration, fraction) combinations\n"
        f"Form 3 (lw_stand_pow):   conditional on forms 1 and 2 falling short"
    )

    # Baseline.
    print()
    print("BASELINE: production half-cosine")
    _print_header()
    baseline = run_iteration(samples, events, "cosine")
    _print_row("cosine", baseline)

    # Form 1.
    print()
    print("FORM 1: halfcosine_pow  t = ((1 - cos(pi*f)) / 2) ^ p")
    _print_header()
    pow_results: list[tuple] = []
    for p in pow_ps:
        r = run_iteration(samples, events, "halfcosine_pow", p=p)
        pow_results.append((p, r))
        _print_row(f"p={p:.2f}", r)

    # Form 2.
    print()
    print("FORM 2: lw_stand  linear hold then cosine")
    _print_header()
    stand_results: list[tuple] = []
    for d in stand_durs:
        for f in stand_fracs:
            r = run_iteration(
                samples, events, "lw_stand",
                stand_minutes=d, rise_fraction=f,
            )
            stand_results.append((d, f, r))
            _print_row(f"dur={d:>2}m frac={f:.2f}", r)

    # Form 3 condition: only run if neither form 1 nor form 2 brings
    # |mean bias| within 0.05m. Doubles the search dimensions, so gating
    # on the simpler forms having failed avoids unnecessary work and
    # keeps the output focused.
    best_pow_bias = min(abs(r["flood"]["mean"]) for _, r in pow_results)
    best_stand_bias = min(abs(r["flood"]["mean"]) for _, _, r in stand_results)

    if best_pow_bias <= 0.05 or best_stand_bias <= 0.05:
        print()
        print(
            "FORM 3 SKIPPED: a simpler form (1 or 2) already brings "
            "|mean bias| within +/-0.05m."
        )
    else:
        print()
        print("FORM 3: lw_stand_pow  LW stand + halfcosine_pow post-stand")
        print("(triggered: forms 1 and 2 both left |mean bias| > 0.05m)")
        _print_header()
        f3_durs = parse_list(
            os.environ.get("SWEEP_DURS_F3"),
            [stand_durs[len(stand_durs) // 2]],
        )
        f3_fracs = parse_list(
            os.environ.get("SWEEP_FRACS_F3"),
            [0.05, 0.08, 0.10],
        )
        f3_ps = parse_list(
            os.environ.get("SWEEP_PS_F3"),
            [1.05, 1.10, 1.15],
        )
        for d in f3_durs:
            for f in f3_fracs:
                for p in f3_ps:
                    r = run_iteration(
                        samples, events, "lw_stand_pow",
                        stand_minutes=d, rise_fraction=f, p=p,
                    )
                    _print_row(f"dur={d}m frac={f:.2f} p={p:.2f}", r)

    # Restore production curve_interpolate before exit so any subsequent
    # use of access_calc in this process is unpatched.
    access_calc._curve_interpolate = _PRODUCTION_CURVE_INTERPOLATE

    # --- Summary ---
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(
        f"Baseline (cosine):  flood mean={baseline['flood']['mean']:+.3f}m  "
        f"RMS={baseline['flood']['rms']:.3f}m  "
        f"max={baseline['flood']['max_abs']:.2f}m  "
        f"ebb_RMS={baseline['ebb']['rms']:.3f}m"
    )
    print()
    p, r = min(pow_results, key=lambda x: abs(x[1]["flood"]["mean"]))
    print(
        f"Best form 1 by |mean bias|:  p={p:.2f}  ->  "
        f"flood mean={r['flood']['mean']:+.3f}m  "
        f"RMS={r['flood']['rms']:.3f}m  "
        f"max={r['flood']['max_abs']:.2f}m  "
        f"ebb_RMS={r['ebb']['rms']:.3f}m"
    )
    p, r = min(pow_results, key=lambda x: x[1]["flood"]["rms"])
    print(
        f"Best form 1 by RMS:          p={p:.2f}  ->  "
        f"flood mean={r['flood']['mean']:+.3f}m  "
        f"RMS={r['flood']['rms']:.3f}m  "
        f"max={r['flood']['max_abs']:.2f}m  "
        f"ebb_RMS={r['ebb']['rms']:.3f}m"
    )
    print()
    d, f, r = min(stand_results, key=lambda x: abs(x[2]["flood"]["mean"]))
    print(
        f"Best form 2 by |mean bias|:  dur={d}m frac={f:.2f}  ->  "
        f"flood mean={r['flood']['mean']:+.3f}m  "
        f"RMS={r['flood']['rms']:.3f}m  "
        f"max={r['flood']['max_abs']:.2f}m  "
        f"ebb_RMS={r['ebb']['rms']:.3f}m"
    )
    d, f, r = min(stand_results, key=lambda x: x[2]["flood"]["rms"])
    print(
        f"Best form 2 by RMS:          dur={d}m frac={f:.2f}  ->  "
        f"flood mean={r['flood']['mean']:+.3f}m  "
        f"RMS={r['flood']['rms']:.3f}m  "
        f"max={r['flood']['max_abs']:.2f}m  "
        f"ebb_RMS={r['ebb']['rms']:.3f}m"
    )
    print()
    print(
        "Sanity check: ebb_RMS should be approximately constant across all\n"
        "rows above (the sweep varies only the flood branch). If it varies\n"
        "materially, the ebb branch was inadvertently affected and results\n"
        "are suspect."
    )
    print()
    print("Decision criteria for closing item 1 of CALIBRATION_NOTES.md:")
    print("  - Flood |mean bias| <= 0.05m AND flood RMS not worse than the")
    print("    baseline (currently 0.290m): bounded form succeeds. Apply.")
    print("  - Otherwise: escalate to NP 159 lookup as a separate workstream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
