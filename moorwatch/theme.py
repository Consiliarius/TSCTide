"""Appearance — a deliberate mirror of SYLog's ``logbook/ui/theme.py``.

Moorwatch sits next to SYLog on one netbook screen, so the two must look like
one set of tools rather than two hobby projects that met by accident. Every
value here is SYLog's, and the intent is that a diff between this file and
``logbook/ui/theme.py`` shows nothing but the deliberate differences noted below.

**Why a copy and not an import.** The two live in separate repositories. TSCTide
has to run without SYLog present — on the dev machine, in CI, in a fresh clone —
so importing across is not available. A copy has a real cost: change SYLog's
palette and this silently disagrees. The cost is accepted because the
alternative is worse, and the mitigation is that it is ONE file, named for what
it is, rather than hex values scattered through the UI.

**If you change SYLog's theme, change this too.** The mismatch will be visible
on the boat before it is visible in a test.

Deliberate differences from the original, all of them consequences of this being
a glanceable readout rather than an app you work in:

  * ``SIZE_LARGE`` is defined for fidelity but unused. Netbook feedback capped
    moorwatch's type at the old access line's size; a 22pt heading here would
    be the shouting that feedback was about.
  * No button factory. SYLog's ``_big_button`` is the button style if moorwatch
    ever grows one — copy it then. Mirroring it now would be dead code in a
    window whose only controls are F2, F11 and Esc.
  * Window floor is moorwatch's own (see ui.py): SYLog's 800x480 is right for
    the app you work in, not for a sidecar beside it.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

LIGHT = {
    "BG": "#f2f4f7", "BG_PANEL": "#e2e8ee", "BG_BUTTON": "#ccd6df",
    "FG": "#11202e", "FG_MUTED": "#54626f", "ACCENT": "#0b5fbe",
    "OK": "#0f7b3a", "WARN": "#8a5300", "BAD": "#b3261e",
}
DARK = {
    "BG": "#0b0f14", "BG_PANEL": "#151d26", "BG_BUTTON": "#20303f",
    "FG": "#e8edf2", "FG_MUTED": "#8695a3", "ACCENT": "#3fa7ff",
    "OK": "#37c871", "WARN": "#f2b134", "BAD": "#e5484d",
}
PALETTES = {"light": LIGHT, "dark": DARK}

MODE = "light"
BG = BG_PANEL = BG_BUTTON = FG = FG_MUTED = ACCENT = OK = WARN = BAD = ""


def use(mode: str) -> str:
    """Switch palette. Unknown modes fall back to light. Returns the mode applied.

    Rebinds this module's colour names, exactly as SYLog's does. Widgets read
    them when they are configured, so switching theme means re-rendering — which
    for moorwatch is what every tick does anyway.
    """
    global MODE, BG, BG_PANEL, BG_BUTTON, FG, FG_MUTED, ACCENT, OK, WARN, BAD
    MODE = mode if mode in PALETTES else "light"
    palette = PALETTES[MODE]
    BG = palette["BG"]
    BG_PANEL = palette["BG_PANEL"]
    BG_BUTTON = palette["BG_BUTTON"]
    FG = palette["FG"]
    FG_MUTED = palette["FG_MUTED"]
    ACCENT = palette["ACCENT"]
    OK = palette["OK"]
    WARN = palette["WARN"]
    BAD = palette["BAD"]
    return MODE


def other(mode: str | None = None) -> str:
    """The mode that isn't the current one."""
    return "dark" if (mode or MODE) == "light" else "light"


def mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours; ``t=0`` -> a, ``t=1`` -> b."""
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


use("light")

# Font sizes (points), applied to Tk's named fonts at startup
SIZE_BASE = 16
SIZE_SMALL = 12
SIZE_LARGE = 22     # mirrored for fidelity; moorwatch caps at SIZE_BASE (see above)

# Sizing (pixels)
TOUCH_MIN = 36
PAD = 8


def preferred_font_family(root) -> str:
    """A clean sans-serif if the system has one, else Tk's default.

    Verbatim from SYLog, and the load-bearing half of matching it: the candidate
    ORDER is what makes both tools land on the same face. Moorwatch previously
    hard-coded "DejaVu Sans", which meant that on a Debian box carrying Noto
    Sans, SYLog rendered in Noto and moorwatch in DejaVu — two tools, one
    screen, two fonts, for no reason anyone could see.
    """
    available = set(tkfont.families(root))
    for family in ("Segoe UI", "Noto Sans", "DejaVu Sans", "Cantarell", "Helvetica"):
        if family in available:
            return family
    return tkfont.nametofont("TkDefaultFont").cget("family")


def apply_fonts(root) -> str:
    """Point Tk's named fonts at the preferred family, as SYLog does at startup.

    Without this, any widget left on its default font keeps Tk's stock face
    while the explicitly-fonted ones change — which reads as a rendering fault
    rather than a choice. Returns the family applied.
    """
    family = preferred_font_family(root)
    tkfont.nametofont("TkDefaultFont").configure(family=family, size=SIZE_BASE)
    for name in ("TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=SIZE_BASE)
        except tk.TclError:
            pass
    return family
