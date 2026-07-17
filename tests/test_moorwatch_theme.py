"""Moorwatch's theme is a mirror of SYLog's. These tests are the mirror's glue.

The two tools sit side by side on one netbook screen, so a divergence is visible
on the boat. But they live in separate repositories and TSCTide must run without
SYLog present, so the values are copied rather than imported — and a copy drifts
silently by nature.

Where a SYLog checkout is available (the dev machine and the netbook both have
one), these compare the two files directly and fail on any divergence. Where it
is not (CI, a fresh clone), they skip: moorwatch must not depend on SYLog to
test, which is the reason it does not import it in the first place.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moorwatch import theme

# Both repos are checked out side by side on the machines that have them.
_SYLOG = Path(__file__).resolve().parents[2] / "SYLog"
_SYLOG_THEME = _SYLOG / "logbook" / "ui" / "theme.py"

sylog_theme = pytest.importorskip  # keeps the linter quiet about the guard below


def _load_sylog_theme():
    """Import SYLog's theme module by path, without importing SYLog itself."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_sylog_theme", _SYLOG_THEME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


needs_sylog = pytest.mark.skipif(
    not _SYLOG_THEME.exists(),
    reason="no SYLog checkout beside this one; the mirror cannot be compared",
)


@needs_sylog
def test_palettes_match_sylog_exactly():
    """A colour that differs is a colour the skipper sees differ."""
    other = _load_sylog_theme()
    assert theme.LIGHT == other.LIGHT
    assert theme.DARK == other.DARK


@needs_sylog
def test_type_scale_matches_sylog():
    other = _load_sylog_theme()
    assert theme.SIZE_BASE == other.SIZE_BASE
    assert theme.SIZE_SMALL == other.SIZE_SMALL
    assert theme.SIZE_LARGE == other.SIZE_LARGE


@needs_sylog
def test_spacing_and_touch_target_match_sylog():
    other = _load_sylog_theme()
    assert theme.PAD == other.PAD
    assert theme.TOUCH_MIN == other.TOUCH_MIN


def _font_candidates(path: Path, func_name: str) -> list[str]:
    """The font families a picker tries, in the order it tries them.

    Read out of the `for family in (...)` loop by AST rather than by searching
    the text: both files NAME these families in their prose, so a string search
    finds the docstring, not the list.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.For) and isinstance(inner.iter, ast.Tuple):
                    return [e.value for e in inner.iter.elts
                            if isinstance(e, ast.Constant)]
    raise AssertionError(f"no candidate tuple found in {func_name} ({path})")


@needs_sylog
def test_font_candidate_order_matches_sylog():
    """The ORDER is the load-bearing part, not the set. Both tools must land on
    the same face on the same machine, and they only do if they prefer families
    in the same sequence: a box carrying Noto Sans but not Segoe UI must not
    give SYLog Noto and moorwatch DejaVu."""
    ours = _font_candidates(Path(theme.__file__), "preferred_font_family")
    theirs = _font_candidates(_SYLOG / "logbook" / "ui" / "app.py",
                              "_preferred_font_family")
    assert ours == theirs


# -- properties that hold with or without a SYLog checkout --------------------

def test_use_switches_palette_and_rebinds_names():
    theme.use("dark")
    assert theme.MODE == "dark"
    assert theme.BG == theme.DARK["BG"]
    theme.use("light")
    assert theme.BG == theme.LIGHT["BG"]


def test_unknown_mode_falls_back_to_light():
    assert theme.use("chartreuse") == "light"
    assert theme.BG == theme.LIGHT["BG"]


def test_other_returns_the_opposite_mode():
    assert theme.other("light") == "dark"
    assert theme.other("dark") == "light"


def test_mix_blends_endpoints():
    assert theme.mix("#000000", "#ffffff", 0.0) == "#000000"
    assert theme.mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert theme.mix("#000000", "#ffffff", 0.5) == "#808080"
