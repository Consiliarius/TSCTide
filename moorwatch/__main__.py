"""Command-line entry point:  python3 -m moorwatch

    python3 -m moorwatch                 one-shot readout
    python3 -m moorwatch --watch         refreshing console readout
    python3 -m moorwatch --at 2026-07-16T05:00Z    readout for a given instant
    python3 -m moorwatch --sync          refresh vessel config from TSCTide
    python3 -m moorwatch --gui           the always-on window

``--at`` exists for verification: it makes the whole tool a pure function of an
instant, so its numbers can be checked against /api/calculate and the ICS feed
for a known tide rather than whatever the tide happens to be doing right now.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from moorwatch import config as cfg_mod
from moorwatch import render
from moorwatch.state import compute_state

WATCH_INTERVAL_SECONDS = 30


def _parse_instant(text: str) -> datetime:
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise SystemExit(
            f"Could not read --at {text!r}. Use an ISO instant, "
            f"e.g. 2026-07-16T05:00Z (UTC assumed if no offset given)."
        )
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _do_sync(args) -> int:
    """Populate the vessel config from TSCTide.

    Deliberately does NOT go through cfg_mod.load(): this is the command that
    makes an install valid, so it has to run on an invalid one. It reads the
    file raw for its defaults and treats a load failure as "nothing to diff
    against yet" rather than an error.
    """
    from moorwatch.sync import SyncError, sync

    try:
        raw = cfg_mod.read_raw()
    except cfg_mod.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    base_url = args.url or raw.get("source_url")
    mooring_id = args.mooring if args.mooring is not None else raw.get("mooring_id")

    if not base_url:
        print("No TSCTide URL. Pass --url https://tsctide.uk", file=sys.stderr)
        return 1
    if mooring_id is None:
        print("Which mooring? Pass --mooring <id>.", file=sys.stderr)
        return 1

    try:
        previous = cfg_mod.load()
    except cfg_mod.ConfigError:
        previous = None      # first sync: nothing to compare against

    try:
        fresh, changes = sync(base_url, int(mooring_id), previous=previous)
    except SyncError as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        return 1

    name = fresh.boat_name or f"mooring {fresh.mooring_id}"
    print(f"Synced {name} (mooring {fresh.mooring_id}) from {fresh.source_url}.")
    print(f"  draught {fresh.draught_m} m, drying height {fresh.drying_height_m} m, "
          f"safety margin {fresh.safety_margin_m} m")
    if previous is None:
        print("First sync - moorwatch is now configured.")
    elif changes:
        print("Changed:")
        for change in changes:
            print(f"  {change}")
    else:
        print("No changes.")
    return 0


def _watch(cfg) -> int:
    try:
        while True:
            state = compute_state(cfg)
            # Clear and home the cursor. Plain ANSI, no curses: this is a
            # convenience view, the GUI is the real one.
            print("\033[2J\033[H", end="")
            print(render.render_cli(state, cfg))
            print(f"\n  Refreshing every {WATCH_INTERVAL_SECONDS}s. Ctrl-C to stop.")
            time.sleep(WATCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="moorwatch",
        description="Depth of water at the mooring, and how long until the boat floats.",
    )
    parser.add_argument("--watch", action="store_true",
                        help="refresh the readout in the console")
    parser.add_argument("--gui", action="store_true",
                        help="open the always-on window")
    parser.add_argument("--at", metavar="INSTANT",
                        help="compute for a given ISO instant instead of now")
    parser.add_argument("--sync", action="store_true",
                        help="refresh the vessel config from TSCTide (needs wifi)")
    parser.add_argument("--url", help="TSCTide base URL, for --sync")
    parser.add_argument("--mooring", type=int, help="mooring id, for --sync")
    args = parser.parse_args(argv)

    # --sync runs before the config is validated, because it is what makes the
    # config valid. Everything else needs a real vessel to talk about.
    if args.sync:
        return _do_sync(args)

    try:
        cfg = cfg_mod.load()
    except cfg_mod.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    if args.gui:
        from moorwatch.ui import run
        return run(cfg)

    if args.watch:
        return _watch(cfg)

    state = compute_state(cfg, _parse_instant(args.at) if args.at else None)
    print(render.render_cli(state, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
