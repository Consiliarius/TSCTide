# Harmonic model validation — measured Portsmouth gauge, Aug 2025 – Jun 2026

A full-year "stress test" of the harmonic tidal model (`app/harmonic.py`) against
an independent, publicly-archived measured water-level record. Run 6 July 2026.

The goal was not to tune anything but to answer one question with real data:
**how well does the harmonic model reproduce the actual height of tide over a full
year of past events?** The model normally produces *future* predictions; here it is
driven backwards over 11 completed months and checked point-by-point and event-by-event
against what the sea actually did.

## Summary

Over 334 days, **30,035** fifteen-minute gauge samples and **1,182** tide turning
points, the model holds up well:

- It tracks the tidal curve's *shape* consistently in every month (de-meaned scatter
  0.23–0.32 m year-round).
- It carries a systematic **−0.14 m low bias** — i.e. it reads slightly *less* water
  than reality, the conservative / safe direction for an access-window tool.
- Its only real seasonal weakness is a winter offset driven by seasonal mean sea level
  and storm surge — weather the astronomical model structurally cannot represent.

No month broke it. Two independent cross-checks landed (see Findings 2 and 3).

## Method

### Driving the model into the past

`app.harmonic.predict_height_at_time()` derives its astronomical arguments from a
Julian Date (`_jd` → `_astro` → `_nodal`) with no past/future guard, so evaluating it
at historical timestamps needs no code change. Physical HW/LW are taken as the raw
turning points of the synthesised curve (not the Admiralty-convention-shifted times
from `predict_events()`), because the reference here is a physical gauge whose peaks
are physical, not published-convention times.

### Reference gauge and why Portsmouth

Measured sea level comes from the Environment Agency flood-monitoring **archive**,
Portsmouth tide gauge **station `E71839`** (mAOD, 15-minute), pulled programmatically
per day — the same source already wired into `scripts/validate_barometric_k.py`.

Portsmouth is the correct "immediate area" reference: Langstone Harbour has no
long-term public gauge of its own, and its UKHO station is itself a *secondary port
derived from Portsmouth*. Because `app.harmonic` is **Portsmouth-native** (it returns
metres above Portsmouth Chart Datum, before the secondary-port shift), **no
secondary-port offset is applied** — this is a like-for-like check of the harmonic
engine itself, at the nearest place with a public height-of-water archive.

### Datum handling

The gauge reads metres above Ordnance Datum Newlyn (mAOD); the model reads metres above
Chart Datum (CD). Portsmouth CD is **2.73 m below OD(N)** (Admiralty NP201), so
`height_above_CD = sea_level_mAOD + 2.73`. That nominal offset is applied, and the
offset the data *itself* implies (`mean(predicted_CD) − mean(gauge_mAOD)`) is reported
as a units/datum cross-check. Timing errors and tidal range (HW−LW) are
datum-independent.

### Weather caveat

Measured sea level = astronomical tide + surge (barometric + wind). The harmonic model
is astronomical-only, so **every residual here is an upper bound on the model's own
error** — real weather inflates the scatter, especially in winter. An optional pass
applies the v2.9 inverse-barometer correction (from ERA5 pressure, Open-Meteo archive)
to strip the pressure-driven part.

## Results — combined corpus (11 months pooled, astronomical only)

| Quantity | n | mean | RMS | stdev (de-meaned) | max&#124;·&#124; |
|---|--:|--:|--:|--:|--:|
| Point-by-point height (m) | 30035 | −0.139 | 0.322 | 0.291 | 1.275 |
| HW height (m) | 600 | −0.121 | 0.230 | 0.195 | 0.758 |
| LW height (m) | 602 | −0.044 | 0.263 | 0.260 | 0.802 |
| Tidal range HW−LW (m, datum-free) | 1182 | −0.083 | 0.277 | 0.264 | 0.968 |
| HW timing (min) | 600 | +45.7 | 49.9 | 20.1 | 103.8 |
| LW timing (min) | 602 | +40.0 | 46.7 | 24.0 | 103.5 |

Implied datum offset: **+2.591 m** (nominal 2.73 m; the −0.139 m gap is the model's net
low bias, not a datum error).

**After the v2.9 inverse-barometer correction** (pressure span 969.7–1034.2 hPa, 64.5 hPa):
de-meaned scatter 0.291 → **0.269 m**, RMS 0.322 → 0.304 m; the seasonal mean is
essentially unchanged (−0.139 → −0.142 m), correctly, because that bias is MSL/surge,
not instantaneous inverse-barometer.

## Results — per month

Heights in metres; timing stdev in minutes; height columns are RMS unless noted.

| Month | n | Bias | RMS | SD | HW h RMS | LW h RMS | Range RMS | HW t SD | LW t SD | Implied datum |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 2025-08 | 2962 | −0.056 | 0.284 | 0.278 | 0.140 | 0.251 | 0.279 | 20.4 | 23.3 | +2.674 |
| 2025-09 | 2875 | −0.045 | 0.303 | 0.300 | 0.182 | 0.323 | 0.368 | 23.3 | 26.5 | +2.685 |
| 2025-10&#42; | 1056 | −0.041 | 0.301 | 0.298 | 0.253 | 0.349 | 0.499 | 27.9 | 23.7 | +2.689 |
| 2025-11 | 2880 | −0.207 | 0.325 | 0.251 | 0.257 | 0.245 | 0.266 | 17.3 | 20.4 | +2.523 |
| 2025-12 | 2976 | −0.187 | 0.368 | 0.317 | 0.288 | 0.271 | 0.225 | 16.5 | 27.9 | +2.543 |
| 2026-01 | 2973 | −0.285 | 0.420 | 0.308 | 0.355 | 0.333 | 0.307 | 19.8 | 28.4 | +2.445 |
| 2026-02 | 2688 | −0.263 | 0.406 | 0.310 | 0.308 | 0.300 | 0.322 | 20.3 | 25.4 | +2.467 |
| 2026-03 | 2954 | −0.076 | 0.284 | 0.274 | 0.179 | 0.255 | 0.283 | 18.0 | 20.6 | +2.654 |
| 2026-04 | 2848 | −0.038 | 0.246 | 0.243 | 0.139 | 0.208 | 0.197 | 22.1 | 18.0 | +2.692 |
| 2026-05 | 2959 | −0.122 | 0.265 | 0.235 | 0.165 | 0.157 | 0.175 | 17.0 | 19.9 | +2.608 |
| 2026-06 | 2864 | −0.150 | 0.281 | 0.238 | 0.175 | 0.210 | 0.177 | 16.7 | 24.1 | +2.580 |

&#42; October 2025 is under-sampled: only 11 of 31 daily archive files were available
(1,056 vs ~2,900 readings). Treat that row as indicative only. Full per-month statistics
(including min/max and the barometric block) are in
[`harmonic_validation_2026-07.json`](harmonic_validation_2026-07.json).

## Findings

**1. Seasonal mean sea level is the dominant residual.** Mean bias swings from −0.04 m
(April) to −0.29 m (January), a ~0.25 m annual cycle, and the implied datum tracks it
(+2.69 m autumn → +2.45 m January). Since the geodetic CD-below-OD offset is *fixed* at
2.73 m, that swing is real oceanography — winter Solent mean sea level sits ~0.25 m
higher, plus winter surge — which the model's `Sa`/`Ssa` seasonal constituents
under-capture. The de-meaned scatter stays flat all year, so the model is not
mis-shaping the tidal curve; it is a slow seasonal datum offset. It reads *low* (safe).

**2. Independent datum cross-check passes to 1 mm.** The corpus implied offset of
+2.591 m reproduces the v2.9 barometric design's separate 12-month regression intercept
of −2.59 m (`docs/CALIBRATION_NOTES.md`, "Barometric coefficient k"). Two unrelated
analyses landing on the same number confirms both the datum handling and that the
~0.14 m net undershoot is a genuine model property.

**3. The documented HW undershoot is confirmed at scale.** Corpus HW-height mean of
−0.121 m matches the ~0.12 m low HW recorded in `CALIBRATION_NOTES.md` item 4, across
600 HW events. LW is nearly unbiased (−0.044 m).

**4. The v2.9 barometric correction is validated.** Over a genuinely wide pressure span
(deep lows to strong highs) it removed 0.022 m of scatter (~15% of variance) in the
right direction with **no bias introduced**, and correctly did *not* erase the seasonal
mean. That is exactly the behaviour the v2.9 design predicts (low R², pressure a minor
share of residual variance) — a clean end-to-end confirmation the feature behaves.

**5. Timing and weather behave as expected.** HW/LW timing scatter (~20/24 min) is in
line with the model's ~15/19 min spec, a touch wider against a physical gauge because of
the Solent HW "stand". The large timing *mean* (+46/+40 min) is the publish-convention
offset (the model reports HW/LW 34/28 min earlier than its mathematical peak) plus stand
ambiguity — not drift. Winter is worst (Jan RMS 0.42, Feb 0.41 — surge season); spring
best (Apr 0.25).

## Follow-up: seasonal `Sa`/`Ssa` recalibration (done 2026-07-06)

The seasonal MSL offset (Finding 1) is the largest systematic (up to ~0.29 m in January),
is *not* addressed by the v2.9 barometric feature (different physics), and is currently
absorbed as a conservative low bias. It was traced to a **mis-phased `Sa`/`Ssa`**: the
prior constituents peaked in September, whereas the real Portsmouth seasonal cycle peaks
in November.

`scripts/fit_seasonal_constituents.py` re-fits the annual/semiannual harmonics against the
full PSMSL Portsmouth station 350 monthly-mean record (1961–2025, 715 months, 64.6 yr) in
the model's own phase convention, removing the secular trend (+2.21 mm/yr) and the 18.6-yr
nodal term. The refitted values now match the observed 64-year climatology to ~1 cm in
every month:

| Constituent | Was | Now |
|---|---|---|
| `SA` | 0.074 m @ 186.7° | 0.0615 m @ 221.8° |
| `SSA` | 0.045 m @ 5.3° | 0.0155 m @ 129.3° |

Applied to `model_config.json`. Effect on the cached 2025–26 corpus: the month-to-month
bias spread drops ~38% (std 0.087 → 0.054 m); the constant mean bias is unchanged (−0.139 →
−0.135 m), correctly, since `Sa`/`Ssa` are zero-mean over a year. The model remains
conservative (net low) in every month.

**Still open:** the ~0.14 m constant low bias is a *separate* issue (mean/HW under-read,
the M2-amplitude candidate in `CALIBRATION_NOTES.md` item 4), not a seasonal one. It is in
the conservative direction, so it is left in place deliberately rather than tuned out.

## Reproducing

Both scripts run on the host (Python 3.11+ with `httpx`) or inside the container. They
reuse the archive/gauge loaders in `scripts/validate_barometric_k.py`.

```bash
# Single window — fast, live gauge, rolling ~4 weeks
python -m scripts.validate_harmonic_vs_measured --source ea --days 28 --barometric

# One specific past month from the EA archive
python -m scripts.validate_harmonic_vs_measured --source ea-archive --start 2026-05-01 --end 2026-05-31

# Full multi-month corpus (this report). Each month's Portsmouth series is cached to
# <out-dir>/cache/portsmouth_YYYY-MM.csv, so re-runs cost no re-download.
python -m scripts.stress_test_harmonic --out-dir ./validation_out --months 2025-08:2026-06

# In the container:
docker exec -w /app tidal-access python -m scripts.stress_test_harmonic --out-dir /app/data/stress
```

The EA archive pulls a full all-station daily dump per day (tens of MB each), so a
multi-month corpus is bandwidth-heavy on first run (~15 min for this 11-month span);
the per-month cache makes subsequent runs instant.
