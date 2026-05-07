# Calibration data

Half-hourly tide-height data used to refine the asymmetric tidal-curve
model (`app/access_calc.py`) and to validate the harmonic prediction model
(`app/harmonic.py`).

The data here is reference material for offline analysis, not consumed by
the running app. It is preserved in the repo so future calibration runs
can use the full historical corpus rather than re-acquiring it each time.

It has been copied from the Admiralty predictions for Langstone Harbour, available here: https://easytide.admiralty.co.uk/?PortID=0066

## Contents

| File | Span | Days | Source | Provenance |
|------|------|------|--------|------------|
| `langstone_ukho_2026-04-14_to_2026-04-22.csv` | 14 Apr 2026 - 22 Apr 2026 | 9 | UKHO Admiralty (station 0066, Langstone Harbour) | Provided via chat 29 Apr 2026, copy-pasted from Admiralty UKHO site. Half-hourly samples only - no HW/LW summary lines. |
| `langstone_ukho_2026-04-29_to_2026-05-05.csv` | 29 Apr 2026 - 5 May 2026 | 7 | UKHO Admiralty (station 0066, Langstone Harbour) | Provided via chat 29 Apr 2026 day-by-day, includes HW/LW summary lines per day. |
| `langstone_ukho_2026-05-06_to_2026-05-12.csv` | 6 May 2026 - 12 May 2026 | 7 | UKHO Admiralty (station 0066, Langstone Harbour) | 6-7 May provided via chat 1 May 2026; 8-12 May provided 6 May 2026. Includes HW/LW summary lines. Covers neap tides (8-10 May, range ~2.1-2.4m) through to building springs (12 May, range ~2.9m). |

Combined corpus: **23 days, 1104 half-hourly samples**, spanning
14-22 April (neaps to springs), 29 April-5 May (springs), and
6-12 May (springs through neaps and back toward springs). This gives
good coverage of the full spring-neap cycle and includes the smallest
tidal ranges captured so far (~2.1m on 9-10 May).

## Format

Each file is a CSV (with optional comment lines starting `#`):

```
2026-04-14 00:00,3.5
2026-04-14 00:30,3.1
...
```

- Timestamps in BST (Europe/London, UTC+1) for all current files.
- Heights in metres above chart datum.
- Comment lines may include HW/LW summary metadata when available.

## Use

The analysis script `scripts/calibrate_from_ukho_week.py` loads all CSVs
in this directory and produces a residual-analysis report against:

1. The current Langstone asymmetric tidal-curve interpolation
   (`interpolate_height_at_time` in `app/access_calc.py`)
2. The current harmonic prediction model (`predict_height_at_time` in
   `app/harmonic.py`), with and without the secondary-port HW correction.

To run the analysis:

```bash
docker exec tidal-access python -m scripts.calibrate_from_ukho_week
```

Phase-position diagnostic:

```bash
docker exec tidal-access python -m scripts.diagnose_residual_position
```

Parameter sweeps:

```bash
docker exec tidal-access python -m scripts.sweep_ebb_params
docker exec tidal-access python -m scripts.sweep_flood_curve
```

## What this data has been used for

- **April 2026 calibration (v2.5.0-v2.5.2)**: ebb-stand parameter sweep
  (40min/0.98 -> 70min/0.96), HW height offset removed (0.05 -> 0.0).
- **April-May 2026 calibration (v2.5.3-v2.5.7)**: flood young-flood-stand
  added (60min/0.08), ebb stand re-tuned (75min/0.94), harmonic duplicate
  rows resolved, phase-position diagnostic created, continuous monitoring
  added, hardcoded constants migrated to model_config.json.
- **May 2026 corpus extension**: neap-tide coverage added (8-12 May) to
  test whether model parameters calibrated on near-spring data generalise
  to small-range tides.

## Adding new data

To add another week:

1. Drop a new CSV into this directory following the format above.
2. Update this README's table to record the file's span and source.
3. Re-run the analysis script.

Filename convention: `<station>_<source>_<YYYY-MM-DD>_to_<YYYY-MM-DD>.csv`.
