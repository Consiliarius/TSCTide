# Portsmouth sea-level sourcing — input for barometric k validation

`scripts/validate_barometric_k.py` (v2.9 Session H) estimates the empirical
inverse-barometer coefficient `k` by regressing measured Portsmouth sea level
against pressure deviation. The slope is `k`; the gauge-vs-prediction datum
offset falls into the intercept. See `docs/V2.9_BAROMETRIC_DESIGN.md` §6.

## Why a long span

A short window (the rolling 4 weeks from the EA live API, `--source ea`) is
enough to validate the pipeline but **not** the coefficient: over a few weeks
storm surge co-varies with low pressure, the harmonic model's own ~0.2 m error
dominates the residual variance, and there are too few independent weather
systems, so the slope comes out biased high. Aim for **≥ 6–12 months** spanning
both deep lows and strong highs.

## Recommended route — EA archive (programmatic, no download, no registration)

The Environment Agency flood-monitoring **archive** publishes daily all-station
reading dumps, and Portsmouth's 15-minute tidal level (`E71839`, mAOD) is
included. The script pulls a date range directly — no manual download, no BODC
account:

```
docker exec -w /app tidal-access python -m scripts.validate_barometric_k \
  --source ea-archive --start 2025-06-01 --end 2026-05-31
```

How it works: for each day it streams `…/flood-monitoring/archive/readings-YYYY-MM-DD.csv`
(a ~0.5 M-row all-station dump) and keeps only the ~96 Portsmouth tidal rows.
This is bandwidth-heavy over long spans (each daily file is tens of MB) but
fully automated and reaches back to the archive's earliest date (~2016).
Historical pressure + wind for the period come from the Open-Meteo ERA5 archive
(free, no key), interpolated to each reading.

## Optional alternative — BODC quality-controlled archive (manual)

If a quality-controlled record is preferred over EA telemetry, the British
Oceanographic Data Centre hosts processed Portsmouth data (15-minute from 1993):

- https://www.bodc.ac.uk/data/hosted_data_systems/sea_level/uk_tide_gauge_network/processed/

Download (a BODC web-user registration may be required), then convert to a
normalised two-column CSV and run with `--source csv`:

```
timestamp_utc,sea_level_m
2024-01-01T00:00:00Z,2.413
2024-01-01T00:15:00Z,2.561
```

- `timestamp_utc` — ISO 8601 UTC (`Z` or `+00:00` both accepted).
- `sea_level_m` — metres, any **consistent** datum (offset → intercept).
- Blank / non-numeric `sea_level_m` rows are skipped.

```
docker exec -w /app tidal-access python -m scripts.validate_barometric_k \
  --source csv --file app/calibration_data/bodc_portsmouth_2023_2025.csv
```

## Interpreting the output

- **`k (slope)`** — empirical coefficient, m/hPa. Compare to the shipped prior
  `0.00882` (UKHO). Only change `barometric.coefficient_m_per_hpa` in
  `model_config.json` if a long-span fit gives a stable, materially different
  slope with a small standard error.
- **`intercept`** ≈ the mAOD→Chart-Datum offset (~−2.7 m for the mAOD series)
  plus harmonic mean bias. A wildly different value signals a datum/unit problem.
- **`R^2`** stays modest even with good data — most residual variance is the
  harmonic model's own error, uncorrelated with pressure, so it does not bias
  the slope.
- **`--wind-max`** reports a fit excluding high local wind as a rough surge
  check; instantaneous 10 m wind is a weak surge proxy, so treat it as
  indicative only.
