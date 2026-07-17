"""The always-on window. Tkinter, stdlib only.

Sits next to SYLog and OpenCPN on the netbook, so it borrows SYLog's visual
conventions rather than inventing its own: the same palettes, a light default,
F2 to switch to the night scheme, F11 for fullscreen.

    Light default is deliberate. SYLog's scope chose dark, then reversed it --
    logbook/ui/theme.py records that dark "proved too dark" on this netbook in
    daylight. Same screen, same sun; no reason to relearn it.

Layout
------
Four readings of equal standing, label and value, values in a column so the eye
drops straight down them. Nothing above 19pt.

The first cut was louder: a 26pt status banner, a 64pt depth figure, and a
detail line under both. It said the same fact three times in three sizes --
"DRIED OUT", "the mooring is dry", "dried out" -- and the 64pt figure was not
carrying 64pt of information. It also put the access line in the body, where a
number that is identical at every glance sits in the reader's way. That number
is in the title bar now, with the boat: both are chrome, and chrome belongs in
the frame.

What survives from the loud version is the colour, on the clearance figure
alone. It is the one number that says what the boat is doing; colouring the rest
would make the window a traffic light with nothing to point at.

Refresh
-------
compute_state is called on the tick, synchronously. **Measured at 109 ms on the
netbook itself** (Acer Aspire One 522, AMD C-50) -- about 0.36% of one core at a
30-second tick, and a pause roughly the length of a blink on a display nobody
interacts with. No event cache: it would be invalidation logic to get wrong in
exchange for saving 109 ms every 30 seconds.

If a future change makes that materially worse, the fix is SYLog's gps.py
pattern -- compute on a daemon thread and hand the result to the main thread
through a queue -- not a cache.

To re-measure on the target:

    python3 -c "
    import time; from moorwatch import config, state
    c = config.load(); state.compute_state(c)
    t=time.perf_counter(); state.compute_state(c); print('%.0f ms' % ((time.perf_counter()-t)*1000))"

Colour
------
Coloured by ACCESS, not by whether the boat is aground. A drying mooring is
aground for half of every cycle by design; painting that red would cry wolf
twice a day and teach the skipper to ignore the colour.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

from moorwatch import render, theme
from moorwatch.state import AFLOAT, AGROUND, DRIED_OUT, MARGIN, compute_state

TICK_MS = 30_000

# Sized to its content (695 x 314 measured), not to SYLog's 800x480 design
# floor. That floor is right for SYLog, which is the app you are working in;
# this is a sidecar that sits beside it and OpenCPN on a 1024x600 screen, and a
# sidecar demanding 78% of the width is one that gets closed. The window still
# opens at a comfortable size and resizes up; the floor just stops it being
# dragged smaller than its own text.
MIN_WIDTH, MIN_HEIGHT = 700, 320
DEFAULT_WIDTH, DEFAULT_HEIGHT = 760, 360

# Wrap width for the long-form messages, independent of the window floor.
WRAP_WIDTH = 660


class MoorwatchApp:
    """One fixed window, rebuilt from a fresh MooringState on each tick."""

    def __init__(self, root: tk.Tk, cfg, mode: str = "light"):
        self.root = root
        self.cfg = cfg
        self.mode = theme.use(mode)
        self.tz = render.tzinfo_for(cfg.timezone)
        self._after_id = None

        root.title(render.title_text(cfg))
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        root.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
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
        # SYLog's family, resolved SYLog's way, on SYLog's scale — the whole
        # point being that the two windows read as one set of tools. The family
        # was hard-coded to "DejaVu Sans" before, which on a box carrying Noto
        # Sans put the two tools in different faces on the same screen.
        #
        # SIZE_BASE carries every reading; bold separates value from label.
        # SIZE_LARGE (22) stays unused: four readings of equal standing need no
        # heading, and the netbook feedback capped the type here.
        family = theme.apply_fonts(self.root)
        self.f_value = tkfont.Font(family=family, size=theme.SIZE_BASE, weight="bold")
        self.f_label = tkfont.Font(family=family, size=theme.SIZE_BASE)
        self.f_line = tkfont.Font(family=family, size=theme.SIZE_SMALL)
        self.f_small = tkfont.Font(family=family, size=theme.SIZE_SMALL)

    ROWS = ("height", "keel", "float", "access")

    def _build_widgets(self):
        pad = theme.PAD
        self.root.configure(bg=theme.BG)

        self.clock = tk.Label(self.root, font=self.f_small, anchor="w")
        self.clock.pack(fill="x", padx=pad * 2, pady=(pad + 2, pad))

        # Four readings, one grid, values in a column so the eye drops straight
        # down them. The boat name and the access line are in the title bar:
        # neither changes between ticks, and a number that is the same at every
        # glance is chrome, not a reading.
        body = tk.Frame(self.root, bg=theme.BG)
        body.pack(fill="x", padx=pad * 2)
        body.columnconfigure(1, weight=1)
        self._body = body

        self._row_label = {}
        self._row_value = {}
        for index, key in enumerate(self.ROWS):
            label = tk.Label(body, font=self.f_label, anchor="w")
            label.grid(row=index, column=0, sticky="w", pady=3)
            value = tk.Label(body, font=self.f_value, anchor="w")
            value.grid(row=index, column=1, sticky="w", padx=(pad * 2, 0), pady=3)
            self._row_label[key] = label
            self._row_value[key] = value

        self.window_line = tk.Label(self.root, font=self.f_line, anchor="w")
        self.window_line.pack(fill="x", padx=pad * 2, pady=(pad, 0))

        self.warning = tk.Label(self.root, font=self.f_small, anchor="w",
                                justify="left", wraplength=WRAP_WIDTH)
        self.warning.pack(fill="x", padx=pad * 2, pady=(pad, 0))

        self.footer = tk.Label(self.root, font=self.f_small, anchor="w",
                               justify="left")
        self.footer.pack(side="bottom", fill="x", padx=pad * 2, pady=pad)

        self._labels = [self.clock, self.window_line, self.warning, self.footer]
        self._labels += list(self._row_label.values())
        self._labels += list(self._row_value.values())
        # Keep the warning wrapping to the real window width rather than the
        # design floor, so a maximised window does not waste the line.
        self.root.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if event.widget is self.root:
            self.warning.configure(wraplength=max(WRAP_WIDTH, event.width - 40))

    def toggle_theme(self, _event=None):
        self.mode = theme.use(theme.other(self.mode))
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
        self.root.configure(bg=theme.BG)
        self._body.configure(bg=theme.BG)
        for label in self._labels:
            label.configure(bg=theme.BG, text="")
        # Blank every reading rather than leaving the last good one on screen:
        # a stale depth that looks live is worse than an empty window.
        self._row_label["height"].configure(fg=theme.BAD, text="No reading")
        self.warning.configure(
            fg=theme.FG,
            text=f"{type(exc).__name__}: {exc}\n\nRetrying every {TICK_MS // 1000}s.",
        )

    def _accent_for(self, state) -> str:
        return {
            AFLOAT: theme.OK,
            MARGIN: theme.WARN,
            AGROUND: theme.FG,
            DRIED_OUT: theme.FG_MUTED,
        }.get(state.status, theme.FG)

    def render(self, state):
        accent = self._accent_for(state)
        self.root.configure(bg=theme.BG)
        self._body.configure(bg=theme.BG)
        for label in self._labels:
            label.configure(bg=theme.BG)

        self.clock.configure(
            fg=theme.FG_MUTED, text=render.format_datetime(state.now, self.tz))

        rows = {
            "height": render.height_row(state),
            "keel": render.keel_row(state),
            "float": render.float_row(state, self.tz),
            "access": render.access_row(state, self.tz),
        }
        # Only the clearance is coloured. It is the one figure that says what
        # the boat is doing, and colouring the others would make the window a
        # traffic light with nothing to point at.
        colours = {"keel": accent}

        for key in self.ROWS:
            row = rows[key]
            if row is None:
                # An always-afloat mooring has no lift-off to report. Blank the
                # row rather than dropping it, so the others do not jump up the
                # window on the tick where it disappears.
                self._row_label[key].configure(text="")
                self._row_value[key].configure(text="")
                continue
            label, value = row
            self._row_label[key].configure(fg=theme.FG_MUTED, text=label)
            self._row_value[key].configure(fg=colours.get(key, theme.FG), text=value)

        self.window_line.configure(
            fg=theme.FG_MUTED, text=render.window_line(state, self.tz)
        )

        notes = list(state.warnings)
        if state.note:
            notes.insert(0, state.note)
        self.warning.configure(
            fg=theme.WARN if state.warnings else theme.FG_MUTED,
            text="\n".join(notes),
        )

        stale = self.cfg.is_stale(state.now)
        self.footer.configure(
            fg=theme.BAD if stale else theme.FG_MUTED,
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
