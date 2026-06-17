"""
Kendall's Wharf sill depth (v2.9.2).

Boat-agnostic estimate of the depth of water over the Kendall's Wharf sill
for the Current Conditions display. The sill is a shoaling bar across the
channel just north of TSC1/TSC2 that boats on the moorings cross to reach
open water.

This module owns sill geometry. v2.9.2 uses only depth_over_sill() (display).
v2.9.3 (access-window gating) will add a boat-specific threshold here
(crest + draught + transit clearance) and wire it into access_calc; that
function is intentionally absent for now. Keeping the display primitive
(depth_over_sill, boat-agnostic) separate from the future gating primitive
(threshold, boat-specific) is the forward-compatibility boundary, and no
code in access_calc references this module in v2.9.2.

Crest height is read from model_config.json (wharf_sill.crest_above_cd_m)
via app.config, with the module-level default below as fallback per the
v2.5.6 lenient-config convention.
"""

from __future__ import annotations

from app.config import get_sill_crest_above_cd_m

# v2.5.6 convention: this module holds the reference default; the JSON value
# overrides when present and well-formed. Metres above Chart Datum, positive
# up. 0.5 is the charted shoal-edge crest from the 1 September 2023 Channel
# Surveys multibeam survey; the deeper slot alongside reads about a metre
# lower but lies against the wharf and the often-moored dredger, so the shoal
# edge is the conservative controlling level. The eastern edge is silting, so
# the true crest is probably higher than the 2023 value.
SILL_CREST_ABOVE_CD_M_DEFAULT = 0.5


def crest_above_cd_m() -> float:
    """Sill crest, metres above Chart Datum (positive up)."""
    return get_sill_crest_above_cd_m(SILL_CREST_ABOVE_CD_M_DEFAULT)


def _depth_over_crest(height_above_cd_m: float, crest_above_cd_m_value: float) -> float:
    """Pure helper: depth = height - crest, clamped at 0, rounded to 0.1 m."""
    return round(max(0.0, height_above_cd_m - crest_above_cd_m_value), 1)


def depth_over_sill(height_above_cd_m: float) -> float:
    """
    Estimated depth of water over the sill at a given tide height, metres.
    Boat-agnostic (no draught or clearance). The clamp means the rare
    lowest-water case (sill exposed) reads 0.0 rather than a negative depth.
    """
    return _depth_over_crest(height_above_cd_m, crest_above_cd_m())
