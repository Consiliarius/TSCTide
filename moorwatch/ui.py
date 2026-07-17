"""The always-on window. Tkinter, stdlib only.

Sits next to SYLog and OpenCPN on the netbook, so it borrows SYLog's visual
conventions rather than inventing its own: the same palettes, a light default,
F2 to switch to the night scheme, F11 for fullscreen, an 800x480 design floor.

    Light default is deliberate. SYLog's scope chose dark, then reversed it --
    logbook/ui/theme.py records that dark "proved too dark" on this netbook in
    daylight. Same screen, same sun; no reason to relearn it.

Refresh
-------
compute_state is called on the tick, synchronously. Measured at 59 ms on a
modern machine, so an estimated 235-350 ms on the C-50 -- about 1% of one core
at a 30-second tick. That does not warrant the event cache the plan sketched,
and a cache would have to be invalidated correctly for no measurable gain. If
the netbook ever feels the hitch, the fix is SYLog's gps.py pattern: compute on
a daemon thread and hand the result to the main thread through a queue.

Colour
------
Coloured by ACCESS, not by whether the boat is aground. A drying mooring is
aground for half of every cycle by design; painting that red would cry wolf
twice a day and teach the skipper to ignore the colour.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone

from moorwatch import render
from moorwatch.state import AFLOAT, AGROUND, DRIED_OUT, MARGIN, compute_state

# Palettes lifted from SYLog's logbook/ui/theme.py so the two tools look like
# they belong on the same screen. Kept as a copy rather than an import: the
# repos are separate, and this is a handful of hex values, not a dependency.
LIGHT = {
    "BG": "#f2f4f7", "BG_PANEL": "#e2e8ee",
    "FG": "#11202e", "FG_MUTED": "#54626f", "ACCENT": "#0b5fbe",
    "OK": "#0f7b3a", "WARN": "#8a5300", "BAD": "#b3261e",
}
DARK = {
    "BG": "#0b0f14", "BG_PANEL": "#151d26",
    "FG": "#e8edf2", "FG_MUTED": "#8695a3", "ACCENT": "#3fa7ff",
    "OK": "#37c871", "WARN": "#f2b134", "BAD": "#e5484d",
}
PALETTES = {"light": LIGHT, "dark": DARK}

TICK_MS = 30_000
MIN_WIDTH, MIN_HEIGHT = 800, 480


class MoorwatchApp:
    """One fixed window, rebuilt from a fresh MooringState on each tick."""

    def __init__(self, root: tk.Tk, cfg, mode: str = "light"):
        self.root = root
        self.cfg = cfg
        self.mode = mode if mode in PALETTES else "light"
        self.tz = render.tzinfo_for(cfg.timezone)
        self._after_id = None

        root.title(f"Moorwatch - {cfg.boat_name or cfg.mooring_id}")
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        root.bind("<F2>", self.toggle_theme)
        root.bind("<F11>", self.toggle_fullscreen)
        root.bind("<Escape>", lambda _e: root.destroy())
        root.bind("<q>", lambda _e: root.destroy())

        self._fullscreen = False
        self._build_fonts()
        self._build_widgets()
        self.tick()

    # -- chrome ---------------------------------------------------------

    def _build_fonts(self):
        # Large enough to read standing up, at arm's length, on a 1024x600
        # screen in a cockpit. Tk's defaults are far too small for that.
        self.f_depth = tkfont.Font(family="DejaVu Sans", size=64, weight="bold")
        self.f_status = tkfont.Font(family="DejaVu Sans", size=26, weight="bold")
        self.f_line = tkfont.Font(family="DejaVu Sans", size=15)
        self.f_big_line = tkfont.Font(family="DejaVu Sans", size=19, weight="bold")
        self.f_small = tkfont.Font(family="DejaVu Sans", size=11)

    def _build_widgets(self):
        p = PALETTES[self.mode]
        self.root.configure(bg=p["BG"])

        self.header = tk.Label(self.root, font=self.f_small, anchor="w")
        self.header.pack(fill="x", padx=16, pady=(10, 0))

        # Label and detail are separate widgets, not one string: at the 800px
        # design floor "IN MARGIN - afloat, but inside the safety margin" on one
        # 26pt line runs off the edge, and the part that clips is the meaning.
        self.status = tk.Label(self.root, font=self.f_status, anchor="w")
        self.status.pack(fill="x", padx=16, pady=(6, 0))

        self.status_detail = tk.Label(self.root, font=self.f_line, anchor="w")
        self.status_detail.pack(fill="x", padx=16)

        self.depth = tk.Label(self.root, font=self.f_depth, anchor="w")
        self.depth.pack(fill="x", padx=16)

        # wraplength as insurance: this line carries three numbers whose widths
        # vary with the tide, so it must wrap rather than clip a figure off the
        # right edge at the 800px floor.
        self.keel = tk.Label(self.root, font=self.f_line, anchor="w",
                             justify="left", wraplength=MIN_WIDTH - 40)
        self.keel.pack(fill="x", padx=16, pady=(0, 8))

        self.float_line = tk.Label(self.root, font=self.f_line, anchor="w")
        self.float_line.pack(fill="x", padx=16)

        self.access_line = tk.Label(self.root, font=self.f_big_line, anchor="w")
        self.access_line.pack(fill="x", padx=16)

        self.window_line = tk.Label(self.root, font=self.f_line, anchor="w")
        self.window_line.pack(fill="x", padx=16, pady=(0, 6))

        self.warning = tk.Label(self.root, font=self.f_small, anchor="w",
                                justify="left", wraplength=MIN_WIDTH - 40)
        self.warning.pack(fill="x", padx=16)

        self.footer = tk.Label(self.root, font=self.f_small, anchor="w",
                               justify="left")
        self.footer.pack(side="bottom", fill="x", padx=16, pady=8)

        self._labels = [
            self.header, self.status, self.status_detail, self.depth, self.keel,
            self.float_line, self.access_line, self.window_line, self.warning,
            self.footer,
        ]
        # Keep the warning wrapping to the real window width rather than the
        # design floor, so a maximised window does not waste the line.
        self.root.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if event.widget is self.root:
            width = max(MIN_WIDTH - 40, event.width - 40)
            self.warning.configure(wraplength=width)
            self.keel.configure(wraplength=width)

    def toggle_theme(self, _event=None):
        self.mode = "dark" if self.mode == "light" else "light"
        self.render(self._state)

    def toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    # -- data -----------------------------------------------------------

    def tick(self):
        try:
            self._state = compute_state(self.cfg)
        except Exception as e:  # noqa: BLE001 - a readout must not die silently
            self._show_error(e)
        else:
            self.render(self._state)
        self._after_id = self.root.after(TICK_MS, self.tick)

    def _show_error(self, exc: Exception):
        p = PALETTES[self.mode]
        self.root.configure(bg=p["BG"])
        for label in self._labels:
            label.configure(bg=p["BG"], text="")
        self.status.configure(fg=p["BAD"], text="NO READING")
        self.warning.configure(
            fg=p["FG"],
            text=f"{type(exc).__name__}: {exc}\n\nRetrying every {TICK_MS // 1000}s.",
        )

    def _accent_for(self, state) -> str:
        p = PALETTES[self.mode]
        return {
            AFLOAT: p["OK"],
            MARGIN: p["WARN"],
            AGROUND: p["FG"],
            DRIED_OUT: p["FG_MUTED"],
        }.get(state.status, p["FG"])

    def render(self, state):
        p = PALETTES[self.mode]
        accent = self._accent_for(state)
        self.root.configure(bg=p["BG"])
        for label in self._labels:
            label.configure(bg=p["BG"])

        name = self.cfg.boat_name or f"Mooring {self.cfg.mooring_id}"
        self.header.configure(
            fg=p["FG_MUTED"],
            text=f"{name}   {render.format_datetime(state.now, self.tz)}",
        )
        self.status.configure(fg=accent, text=render.status_label(state))
        self.status_detail.configure(
            fg=p["FG_MUTED"], text=render.status_detail(state)
        )
        self.depth.configure(fg=accent, text=render.depth_text(state))
        self.keel.configure(
            fg=p["FG_MUTED"],
            text=f"under the keel {render.clearance_value(state)}"
                 f"    |    height {state.height_cd_m:.2f} m above CD"
                 f"    |    access line {state.threshold_m:.2f} m",
        )

        float_text = render.float_line(state, self.tz)
        self.float_line.configure(fg=p["FG_MUTED"], text=float_text)
        self.access_line.configure(
            fg=p["FG"], text=render.transition_line(state, self.tz)
        )
        self.window_line.configure(
            fg=p["FG_MUTED"], text=render.window_line(state, self.tz)
        )

        notes = list(state.warnings)
        if state.note:
            notes.insert(0, state.note)
        self.warning.configure(
            fg=p["WARN"] if state.warnings else p["FG_MUTED"],
            text="\n".join(notes),
        )

        stale = self.cfg.is_stale(state.now)
        self.footer.configure(
            fg=p["BAD"] if stale else p["FG_MUTED"],
            text="Harmonic model, no barometric correction.   "
                 + render.config_age_line(self.cfg, state.now)
                 + "    F2 night  |  F11 fullscreen  |  Esc quit",
        )


def run(cfg, mode: str = "light") -> int:
    """Open the window. Returns a process exit code."""
    root = tk.Tk()
    MoorwatchApp(root, cfg, mode=mode)
    root.mainloop()
    return 0
