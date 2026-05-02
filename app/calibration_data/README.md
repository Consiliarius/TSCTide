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

Combined corpus: **16 days, 768 half-hourly samples**, spanning a complete
neaps-springs-neaps progression in mid-April 2026 plus a near-springs
week at the end of April 2026.

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
docker exec tidal-access python /app/run_calibration.py
```

(Assumes the script and any new CSVs have been copied into the running
container via `docker cp`. After the next `docker compose up -d --build`,
both `scripts/` and `app/calibration_data/` will be baked into the image
and direct copies will not be needed.)

## What this data has been used for

- **April 2026 calibration run (29 Apr 2026)**: produced the residual
  analysis that motivated the structural fix to the tidal-curve model
  (adding a pre-HW stand symmetric to the existing post-HW stand).
- **Future use**: incremental new weeks will be added as further CSVs;
  the analysis script will load all of them automatically. This avoids
  the trap of tuning to the most recent week alone.

## Adding new data

To add another week:

1. Drop a new CSV into this directory following the format above.
2. Update this README's table to record the file's span and source.
3. Re-run the analysis script.

Filename convention: `<station>_<source>_<YYYY-MM-DD>_to_<YYYY-MM-DD>.csv`.
