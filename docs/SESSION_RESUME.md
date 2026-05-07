# TSCTide v2 — Session Resume Summary
# Last updated: 7 May 2026

## Project context

- **Repo**: `C:\Users\awpch\OneDrive\Documents\GitHub\TSCTide`
- **GitHub**: https://github.com/Consiliarius/TSCTide
- **Container**: `tidal-access`, DB at `/app/data/tides.db`, feeds at `/app/data/feeds/`
- **Current branch**: `main` (all feature branches merged)
- **Deploy**: `docker compose down && docker compose up -d --build`

## Standing orders for this project

- Short responses, accuracy > speed, flag uncertainties.
- No first-person pronouns ("I", "us").
- No em-dashes in generated code comments (ASCII only); em-dashes OK in UI display strings.
- Complete all fixes for one file before writing it; one write per file.
- **Never assume file/function contents. Read source to verify before commenting, recommending, or planning work around any function.**

## Current state (committed on main)

### v2.5.2 — Tidal model calibration (committed)

Calibration corpus: 23 days, 1104 half-hourly UKHO samples across three
CSV files in `app/calibration_data/`. Analysis scripts in `scripts/`.

Calibration accuracy:

| Path                      | Mean bias | RMS error |
|---------------------------|-----------|-----------|
| UKHO curve (overall)      | +0.025m   | 0.172m    |
| UKHO curve (flood phase)  | +0.037m   | 0.200m    |
| UKHO curve (ebb phase)    | +0.008m   | 0.124m    |
| Harmonic (production)     | +0.024m   | 0.222m    |

Key parameters: `stand_duration_minutes=75`, `stand_height_fraction=0.94`,
`HW_HEIGHT_OFFSET_M=0.0` (height offset removed, timing offset +9min retained).

Full calibration state documented in `docs/CALIBRATION_NOTES.md` including:
- Outstanding items (flood mid-cycle bias, harmonic mid-tide bias, per-day
  drift, LW heights under-amplified)
- Items rejected (pre-HW flood stand, wider stand duration, removing timing offset)
- Future calibration guidance (data acquisition, cadence, step-by-step procedure)
- `model_config.json` persistence notes

### v2.6 — Current Conditions (committed, merged to main)

Persistent dashboard above tab bar showing real-time tide and weather at
Langstone Harbour. Auto-refreshes every 15 minutes.

**Tide panel**: current height above CD (interpolated from UKHO data via
tidal curve), flood/ebb/HW/LW state indicator, next 3 upcoming events
(full HW/LW/HW or LW/HW/LW cycle) with countdown on the first.

**Weather panel**: wind direction/speed/gusts with Beaufort scale, atmospheric
pressure with 7-state trend (steady/slowly/moderate/rapidly, rising/falling),
precipitation with intensity classification, visibility with range description.

**Pressure trend thresholds** (converted from inHg, validated):

| State               | hPa delta / 3h |
|---------------------|----------------|
| Steady              | |delta| < 0.10 |
| Slowly rising/falling | 0.10 - 1.35 |
| Rising/falling      | 1.35 - 6.10   |
| Rapidly rising/falling | > 6.10      |

**Architecture**: `app/conditions.py` provides `get_current_conditions()`
with in-memory cache (15-min TTL). Weather from `app/wind.py::fetch_current_weather()`.
Pressure history in `pressure_history` SQLite table. Scheduler runs
`conditions_refresh()` every 15 minutes. API: `GET /api/conditions` (ungated).

### UI changes (committed on main, same session)

- Tab order changed: Tides (default) > Access Windows > Mooring Configs > System Activity
- Tides tab auto-loads 7-day forecast data on page open
- Panel header icons: info circle on Getting Started, cog on Configuration,
  abacus on Calculate, calendar on Access Windows
- Getting Started header text in `--sea-light` colour; all toggle arrows in `--sea-light`
- Footer: safety disclaimer (justified), links to UKHO EasyTide, Met Office,
  GitHub issues (centred)
- Source attribution on conditions panel (UKHO Admiralty / OpenWeatherMap)
- "Precipitation" label (was "Rain"), "Current height of tide" label (was "Height")

## Calibration reminders (in memory)

- ~30 May 2026: review harmonic-residual monitoring 30-day window
- ~28 July 2026: review harmonic-residual monitoring 90-day window

## Half-hourly data acquisition

UKHO Discovery API (free tier) provides HW/LW events ONLY. Half-hourly
data is NOT available. Foundation tier (GBP 120/year) provides configurable-
interval heights for current+13 days. Until upgraded, manual weekly copy-paste
from Admiralty EasyTide is required; missed weeks are lost permanently.

Full guidance in `docs/CALIBRATION_NOTES.md` section "Future calibration:
planning and data acquisition".

## Known issues

### Harmonic duplicate rows on within-day refresh

Documented in `docs/CALIBRATION_NOTES.md` item 0. Multiple refreshes of
harmonic predictions within the same day produce near-duplicate rows because
event timestamps drift by seconds between runs. Workaround:

```
docker exec tidal-access sqlite3 /app/data/tides.db "
DELETE FROM harmonic_predictions
WHERE generated_at < (SELECT MAX(generated_at) FROM harmonic_predictions);
"
```

Permanent fix: cycle-number-based deduplication in the schema.

## Next feature: v2.7 — Tender Access

### Background

Swing moorings that dry require a tender/dinghy to reach the boat from
shore. The main access window shows when the boat can sail; a secondary
"tender access" window shows when the mooring location has enough water
for a small boat to reach it at all. This is a wider window (lower
threshold) and is useful even on tides where the main boat cannot float.

### Confirmed requirements

**Configuration:**
- Two new fields on mooring: `tender_access_enabled` (boolean, default off),
  `tender_min_depth_m` (float, default 0.3m)
- Toggle only visible in UI when `drying_height_m > 0`
- Single depth field — no separate tender draught or safety margin.
  The user specifies the minimum water depth they need for their tender.

**Calculation:**
- Tender threshold: `drying_height_m + tender_min_depth_m`
- Wind offset applies identically (same ground topography at the mooring)
- Computed for ALL tides including below-threshold (boat can't sail but
  tender may still reach)
- Implementation: second call to `compute_access_windows()` with
  `draught_m=0, safety_margin_m=tender_min_depth_m`
- Tender window is always wider than or equal to the main access window
- "Always accessible" case: "Tender access available throughout this tidal cycle"

**Output — Calendar events / .ics export:**
- New line appended to `_build_description()` in `app/ical_manager.py`:
  - Normal: "Access via tender likely available from HH:MM to HH:MM"
  - Below-threshold: "No sailing access this tide. Tender access likely
    available from HH:MM to HH:MM"
  - Always accessible: "Tender access available throughout this tidal cycle"

**Output — On-screen (Access Windows panel):**
- Sub-row below each access window row in the results table
- Only rendered when `tender_access_enabled` is true
- Visually distinct but unobtrusive (smaller text, indented)

**Not in scope:**
- Tender-only calendar events (always paired with main access window)
- Separate tender safety margin
- Tender draught as a distinct concept from minimum depth

### Files that need changes

| File | Change | Scope |
|------|--------|-------|
| `app/database.py` | Add `tender_access_enabled` and `tender_min_depth_m` columns to moorings table | Schema migration |
| `app/access_calc.py` | No structural change — second call to `compute_access_windows` with tender params | Small |
| `app/main.py` | New fields on mooring POST/GET, tender calc in `/api/calculate`, tender data in response dict | Moderate |
| `app/ical_manager.py` | Extend `_build_description()` with tender window line | Small |
| `app/scheduler.py` | Daily refresh and wind-offset recalculation include tender computation | Small |
| `app/static/index.html` | Config panel: conditional toggle + input. Results table: sub-row rendering. JS: pass tender config to API | Moderate |

### Build order (recommended)

1. Database schema migration (add columns)
2. Backend: compute tender windows alongside main windows in `main.py` / `scheduler.py`
3. Calendar description: extend `_build_description()`
4. Frontend: configuration toggle + input (conditional visibility)
5. Frontend: results table sub-row rendering
6. Testing: verify calendar export, feed regeneration, and on-screen display

### Design decisions already made

- Q1: Tender computed for below-threshold tides — YES
- Q2: Single field (tender_min_depth_m), not draught + margin — CONFIRMED
- Q3: Wind offset applies to tender calculation — YES (same ground topography)
- Q4: Always-accessible wording — "Tender access available throughout this tidal cycle"
- Q5: No additional safety margin for tender — user specifies total depth needed
- Q6: Default tender_min_depth_m — 0.3m

## Operational reference

### Forcing fresh harmonic predictions

```powershell
docker exec tidal-access python -c "
import asyncio
from datetime import datetime, timedelta, timezone
from app.harmonic import predict_events as harmonic_predict_events
from app.secondary_port import apply_offset
from app.database import store_harmonic_predictions
from app.ical_manager import generate_langstone_harmonic_180d_feed
start = datetime.now(timezone.utc)
end = start + timedelta(days=180)
raw = harmonic_predict_events(start, end)
langstone = apply_offset(raw) if raw else []
n = store_harmonic_predictions(langstone)
print(f'Stored {n} harmonic predictions')
generate_langstone_harmonic_180d_feed()
print('Regenerated Langstone_Harmonic_180d.ics')
"
```

### Calibration scripts

```powershell
docker exec tidal-access python -m scripts.calibrate_from_ukho_week
docker exec tidal-access python -m scripts.sweep_ebb_params
docker exec tidal-access python -m scripts.diagnose_residual_position
docker exec tidal-access python -m scripts.sweep_flood_curve
```

### model_config.json persistence

As of v2.5.5, `model_config.json` is read from the bundled image only.
Rebuilds pick up changes automatically. No manual copy step needed.
