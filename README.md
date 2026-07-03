# Tidal Access Window Predictor

A Docker-containerised application for predicting when a boat on a swing mooring in Langstone Harbour has sufficient water depth to depart and arrive.

**Version 2** — adds per-mooring 6-digit PIN protection, decouples iCal feed updates from calculation, restructures the calibration system to split base drying height from shallow-side wind offset, and adds standalone Langstone tide feeds (UKHO 7-day and combined UKHO+harmonic 180-day). **v2.8** adds an interactive Tidal Curve panel showing predicted heights with crosshair hover, a live "now" line, access-threshold lines and shaded access bands for the loaded mooring, sunrise/sunset markers, a Spring/Neap/Mid classification, and a date selector for panning across the UKHO window; the tab navigation has also been relocated to a static top menu bar flush with the page header. **v2.8.1** extends the curve panel with a date-based UKHO → harmonic source switch (UKHO for today+0..6, harmonic for day 7+ to the 180-day horizon), a native date picker, and an `est.` accuracy disclaimer when harmonic-sourced. **v2.9** adds a barometric (inverse-barometer) correction to predicted tide heights and access windows — a system master plus per-mooring opt-in that shifts heights for forecast pressure (low pressure raises water, high pressure lowers it), a new standalone pressure-corrected 7-day tide feed, and universal conservative 5-minute rounding of all displayed access-window edges. **v2.9.1** removes the legacy KHM Portsmouth copy/paste import path — superseded by the validated harmonic model — leaving UKHO (7-day) and the harmonic model (long-range) as the two tide sources. The v1.0 release is preserved on the tag `v1.0`.

## Overview

The tool computes access windows — the periods around each high water when the tide height exceeds the sum of the mooring's drying height, the boat's draught, and a safety margin. It uses two data sources in priority order:

| Source | Range | Origin | Persisted | Offset Applied |
|--------|-------|--------|-----------|----------------|
| **UKHO** | 7 days | Admiralty Tidal API (Langstone native) | Yes | No (native data) |
| **Harmonic** | Unlimited | Built-in harmonic model (19 constituents, calibrated April 2026) | **No** (display only) | Yes (Portsmouth → Langstone: +9min, +0.05m HW) |

### Key Features

- **Per-mooring configuration** with persistent storage keyed by mooring number (1–100)
- **6-digit PIN protection** — each mooring is protected against casual alteration by third parties; see **PIN Protection** below for the full model
- **Empirical calibration** — record observations (afloat/aground) to refine the drying height estimate, with confidence rating
- **Subscribable iCal feed** per mooring, auto-updated daily, with proper subscription metadata
- **Standalone Langstone tide feeds** — two non-mooring feeds providing HW/LW times and heights at Langstone Harbour: a 7-day UKHO feed and a 180-day combined UKHO+harmonic feed
- **Visual Tidal Curve** (v2.8) — plots predicted tidal height in an interactive panel with crosshair hover for time/height readouts, a red "now" line, shaded access bands for the loaded mooring (solid green for boat access, hatched for tender), sunrise/sunset markers with night shading, a Spring/Neap/Mid classification badge, and a date selector for panning across the UKHO 7-day window
- **Wind offset** — adjusts the start of the next tide's access window based on observed wind direction and the mooring's shallow-water geometry
- **ICS export** from any data source, with harmonic-derived events prefixed "est."
- **XLSX batch import** for observations recorded on a phone over time
- **HTTPS support** via Cloudflare Tunnel (zero inbound ports, automatic certificates)

## Quick Start

### Prerequisites

- Docker and Docker Compose
- UKHO Admiralty Tidal API key ([free Discovery tier](https://admiraltyapi.portal.azure-api.net/))
- OpenWeatherMap API key ([free tier](https://openweathermap.org/api)) — optional, for wind offset feature

### Setup (HTTP — LAN use)

1. Copy `.env.example` to `.env` and add your API keys:
   ```
   cp .env.example .env
   ```

2. Build and start:
   ```
   docker compose up -d --build
   ```

3. Open `http://localhost:8866` in a browser.

### Setup (HTTPS — public access via Cloudflare Tunnel)

Required for Google Calendar and Outlook web feed subscriptions. No inbound ports needed.

1. Complete the HTTP setup above
2. Ensure the domain (tsctide.uk) has its DNS managed by Cloudflare
3. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/):
   - Navigate to **Networks → Tunnels → Create a tunnel**
   - Name the tunnel (e.g. "tidal-access")
   - Copy the tunnel token
   - Under **Public Hostname**, add:
     - Domain: `tsctide.uk`
     - Service type: `HTTP`
     - URL: `tidal-access:8866`
4. Paste the tunnel token in `.env`:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoiNjQ1...
   ```
5. Start with the HTTPS profile:
   ```
   docker compose --profile https up -d
   ```

The tunnel connects outbound to Cloudflare — no port forwarding, no static IP, no firewall changes. SSL certificates are managed by Cloudflare automatically. The feed is accessible at `https://tsctide.uk/feeds/mooring_42.ics`.

### First Use

1. Enter your boat's draught and an initial estimate of the mooring's drying height
2. Click **UKHO (7 days)** to fetch tide data and calculate access windows
3. Optionally enter a Mooring ID and save the configuration for persistence and calendar subscription

## Architecture

```
tidal-access/
├── docker-compose.yml       # App + optional Cloudflare Tunnel
├── Dockerfile
├── .env.example
├── requirements.txt
├── app/
│   ├── main.py              # FastAPI routes
│   ├── config.py             # Environment + model config
│   ├── database.py           # SQLite persistence
│   ├── ukho.py               # UKHO API client
│   ├── harmonic.py           # Harmonic prediction (19 constituents, Doodson args)
│   ├── secondary_port.py     # Portsmouth → Langstone offset
│   ├── wind.py               # OWM client + offset logic
│   ├── access_calc.py        # Window calculation engine
│   ├── ical_manager.py       # iCal feed + export generation
│   ├── scheduler.py          # APScheduler jobs
│   ├── model_config.json     # Model parameters (read-only at runtime)
│   └── static/index.html     # Web UI
└── data/                     # Docker volume (persistent)
    ├── tides.db              # SQLite database
    └── feeds/                # Generated .ics files
```

## Configuration

### Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `UKHO_API_KEY` | Admiralty Tidal API subscription key | (required) |
| `OWM_API_KEY` | OpenWeatherMap API key | (optional) |
| `UKHO_STATION_ID` | Primary UKHO station | `0066` (Langstone) |
| `UKHO_FALLBACK_STATION_ID` | Fallback if primary has limited data | `0065` (Portsmouth) |
| `UKHO_FETCH_HOUR` | Hour for daily auto-fetch (local time) | `2` |
| `UKHO_FETCH_MINUTE` | Minute for daily auto-fetch | `0` |
| `LOCATION_LAT` | Latitude for OWM queries | `50.8185` |
| `LOCATION_LON` | Longitude for OWM queries | `-0.9806` |
| `PIN_HASH_SALT` | Site-wide salt used when hashing mooring PINs (set to any long random string; must stay stable or all PINs are invalidated) | (required for PIN operations) |
| `PORT` | Web interface port | `8866` |

## Event Titles

Calendar events use unicode fraction durations and vary by configuration:

| Configuration | Example Title |
|---------------|---------------|
| No mooring (stateless) | ⚓ Tidal Access (3½h) |
| Mooring number only | ⚓ Access to #27 (3½h) |
| Boat name provided | ⚓ Kerry Dancer Afloat (3½h) |
| Harmonic source | ⚓ est. Kerry Dancer Afloat (3½h) |

Durations are rounded down to the nearest quarter-hour.

## Observations & Calibration

Observations record the boat's state at a given time, tied to a specific mooring. Each observation includes:
- State: afloat or aground
- Wind direction (intercardinal, optional)
- Direction of lay — bow heading (intercardinal, optional)

Observations can be entered manually or batch-imported via XLSX (template downloadable from the UI).

The calibration system computes upper bounds (from afloat observations) and lower bounds (from aground observations) on the mooring's drying height. A confidence rating is displayed:

| Rating | Meaning |
|--------|---------|
| ●●● High | Bounds < 0.2m apart |
| ●●○ Medium | Bounds < 0.5m apart |
| ●○○ Low | Bounds > 0.5m apart |
| ●○○ Partial | Only afloat or only aground data |
| ⚠ Inconsistent | Bounds conflict |

Observations that qualify for wind-offset calibration (see **Wind Offset** below) are routed to that calibration instead of contributing to the base drying height. A running count of such routed observations is shown in the UI.

**Applying calibration**. Suggested values from observations are displayed in the Calibration Status card as proposals only: they are not written to the mooring configuration automatically. When a suggestion differs from the currently stored value, an **Apply** button appears next to it; clicking Apply updates the mooring config, recomputes future access windows, and regenerates the iCal feed. Individual observations can be deleted, or all observations for a mooring can be cleared; either action also updates the suggestion, but never the stored config.

## PIN Protection

Each mooring is protected by a 6-digit PIN. Once a mooring has been claimed, any change to its configuration, observations, calibration, or iCal feed requires the PIN.

### What the PIN gates

PIN verification is **required** for:

- Saving changes to a claimed mooring's configuration
- Adding, deleting, or uploading observations
- Applying calibration suggestions (base drying height, wind offset)
- Pushing calculated windows into the mooring's iCal feed

PIN verification is **not required** for:

- Reading a mooring's configuration (the stored hash is never returned to clients)
- Calculating access windows — calculation is read-only, so any visitor can compute windows against any mooring without authentication
- Downloading a one-shot `.ics` export of a calculation result
- Subscribing to a mooring's public iCal feed at `/feeds/mooring_{id}.ics`

### Claim-on-first-save

A newly-created mooring has no PIN. The first **Save Configuration** on a new or unclaimed mooring is permitted without authentication, and the UI then prompts the user to set a 6-digit PIN immediately. If the claim prompt is cancelled, the mooring remains unclaimed; the next save triggers the claim prompt again.

**Claim promptly.** An unclaimed mooring can be claimed by anyone who reaches the `/api/moorings/{id}/pin` endpoint first. This is a deliberate first-write-wins design — simpler than any alternative — but it means that leaving a mooring in the unclaimed state creates a small window during which a third party who knows or guesses the mooring ID can claim it.

### Rate limiting

Failed PIN attempts are tracked per mooring:

- Up to **5 failed attempts within 10 minutes** are permitted
- The 5th consecutive failure **locks the mooring for 15 minutes**
- Successful verification resets the counter
- The lockout covers all PIN operations on the mooring — including Change PIN

While locked, all PIN-gated operations on the affected mooring return HTTP 429 with a `Retry-After` header.

### Changing a PIN

Use the **Change PIN** button in the Configuration panel. The current PIN is required to set a new one. Forgotten PINs cannot be recovered through the UI — see the admin reset procedure below.

### Admin PIN reset

PIN resets require direct database access. Two mechanisms are supported:

**Option A — Clear the PIN** (returns the mooring to unclaimed state; user then sets a new PIN on their next save via the claim flow):

```
sqlite3 data/tides.db "UPDATE moorings SET pin_hash = '' WHERE mooring_id = 42;"
```

**Option B — Set a known PIN** (e.g. `000000`, so the user can log in with the known value and then use Change PIN to rotate it):

```
# Compute hash against the configured salt:
python3 -c "import hashlib, os; s=os.environ['PIN_HASH_SALT']; p='000000'; print(hashlib.sha256((s+p).encode()).hexdigest())"

# Apply to the database:
sqlite3 data/tides.db "UPDATE moorings SET pin_hash = '<hex_digest_from_above>' WHERE mooring_id = 42;"
```

The hashing scheme is deterministic (SHA-256 of `salt + pin` with the site-wide `PIN_HASH_SALT`), so precomputed hashes for any known PIN value can be reused across moorings without per-row salts.

### Admin tools

A Windows PowerShell helper script is bundled at the repo root for operator convenience.

**`delete-mooring.ps1`** — Permanently removes a mooring from the database, bypassing the PIN-gated UI flow. Useful when a user has lost their PIN, when test moorings need cleaning up, or when the UI is unreachable. Cascades into observations, calendar events and PIN lockout state, removes the on-disk feed file, and inserts a `mooring_deleted` audit row into the activity log so the deletion is traceable.

Usage:

```
.\delete-mooring.ps1 -MooringId 42
.\delete-mooring.ps1 -MooringId 42 -Force          # skip the confirmation prompt
.\delete-mooring.ps1 -MooringId 42 -Container my-container-name
```

By default the script asks the operator to type the mooring ID back as confirmation before any database changes are made; deletions run inside a single SQL transaction so a partial failure leaves the database unchanged. The script requires Docker Desktop to be running and the `tidal-access` container to be up.

If this is the first PowerShell script run on the machine, Windows may need execution policy adjusting once with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` from an admin PowerShell, or invocations can be wrapped with `powershell -ExecutionPolicy Bypass -File .\delete-mooring.ps1 ...`.

### Threat model

The PIN system is designed to deter **casual misuse** — accidental edits, opportunistic changes by a third party who guesses or is told a mooring ID. It is **not** designed to resist:

- **An attacker with filesystem access to the database and `.env`.** A 6-digit PIN has only 1,000,000 possible values; any hash scheme, salted or not, is exhaustively reversible in under a second on commodity hardware given the salt. If someone can read the database file, the PIN system does not meaningfully slow them down.
- **Attackers observing PIN entry over an unencrypted connection.** Deploy the app behind HTTPS (Cloudflare Tunnel or a TLS-terminating reverse proxy) for any public-facing use.
- **Timing races during the claim window.** Described under Claim-on-first-save above.

For the intended use case — a handful of friends sharing a tool, protected from each other's keyboard mistakes and from an unknown third party stumbling onto the public URL — the threat model is appropriate. Anything stronger would require user accounts and TLS client auth, which is out of scope for this tool.

### The scheduler bypasses the PIN

The daily UKHO refresh (02:00 local) and the per-mooring wind observation jobs update stored events and regenerate feeds by calling internal functions directly, not through the HTTP API. They therefore bypass PIN gating by design. This is consistent with the threat model: the scheduler executes work that the mooring owner has already authorised at configuration time (by enabling calendar subscription and/or wind offset). An attacker who can reach the scheduler has already broken into the host.

## iCal Feeds

Each mooring with calendar subscription enabled gets a stable URL:
```
https://tsctide.uk/feeds/mooring_42.ics
```
On the local network, `http://localhost:8866/feeds/mooring_42.ics` also works.

The feed includes `REFRESH-INTERVAL` and `X-PUBLISHED-TTL` metadata for proper calendar app subscription behaviour. Events use cycle-based UIDs that are stable across data sources — upgrading from harmonic to UKHO data replaces events rather than duplicating them.

### How feeds are updated (v2)

Calculating access windows in the UI is a **read-only** operation. It returns windows for display or `.ics` download but does **not** alter the subscribed feed. The feed is updated in three distinct ways:

1. **Automatically, daily at 02:00 local time.** The scheduler fetches fresh UKHO data, recomputes windows for every calendar-enabled mooring, and regenerates their `.ics` files. This runs without a PIN (see *The scheduler bypasses the PIN* under **PIN Protection**).
2. **On Apply of a calibration suggestion.** Applying a drying-height or wind-offset suggestion from the Calibration Status card implicitly recomputes future windows and regenerates the feed. Apply actions are PIN-gated.
3. **Manually via the Update Feed button.** After calculating windows the user can click **Update Feed** to push those specific windows into the mooring's stored events and regenerate the `.ics` file. This is PIN-gated. The button is shown only when calendar subscription is enabled for the mooring — calendar-disabled moorings do not have a feed file to update.

The split between calculation (ungated) and feed update (PIN-gated) exists so that anyone — including the mooring owner experimenting with different parameters — can calculate windows without accidentally overwriting what the subscribed feed serves. One-shot `.ics` downloads from the results panel are similarly ungated.

## Standalone Langstone Tide Feeds

In addition to the per-mooring access-window feeds, two non-mooring feeds publish raw HW/LW tide events at Langstone Harbour. They are intended for users who simply want tide times in their calendar without configuring a mooring.

| Feed | URL | Range | Source |
|------|-----|-------|--------|
| Langstone UKHO 7d | `/feeds/Langstone_UKHO_7d.ics` | next 7 days | UKHO Admiralty data only |
| Langstone 180d | `/feeds/Langstone_Harmonic_180d.ics` | next 180 days | UKHO for days 0–7, harmonic model for days 8–180 |
| Langstone UKHO 7d (pressure-corrected) | `/feeds/Langstone_UKHO_7d_PressureCorrected.ics` | next 7 days | UKHO Admiralty data with the barometric correction applied to heights (v2.9) |

All three feeds are publicly accessible (no PIN required) and refresh daily at 02:00 alongside the per-mooring feeds. Each calendar event is rendered as a 1-hour slot centred on the tide event time, with a title like `⚓ HW 4.2m` for measured data or `⚓ est. LW ~1.0m` for harmonic estimates. The event description records the source (`UKHO Langstone`, `UKHO Portsmouth (Langstone offset applied)`, or `Harmonic model (Langstone)`).

The **pressure-corrected** feed is a height-only sibling of the UKHO 7-day feed: it carries the same tide events at the same times and the same UIDs, with the [barometric correction](#barometric-pressure-correction-v29) applied to the HW/LW heights using the forecast pressure at each event time. Days roughly 0–5 are corrected (where the pressure forecast reaches); days ~5–7 lie beyond the forecast horizon and are identical to the uncorrected feed. While the system master switch is off the feed is byte-equivalent to `Langstone_UKHO_7d.ics`. The two uncorrected feeds are deliberately left uncorrected so they stay in step with UKHO/EasyTide for cross-checking.

The 180-day feed merges UKHO refinements as they become available: when a tide that was previously a harmonic estimate falls within the next 7 days, the next daily refresh replaces it with the UKHO version under the same UID, so calendar apps update the existing event rather than showing a delete-and-add. Harmonic estimates carry a typical accuracy of ±15–20 minutes on time and ±0.15m on height; UKHO data is to the published Admiralty resolution.

Harmonic predictions are stored separately from UKHO data (in a dedicated `harmonic_predictions` table) so they cannot contaminate observation calibration. A 12-month rolling history of past predictions is retained to support periodic comparison between predicted and observed values for model refinement; this history is not surfaced in the UI.

### Calendar App Compatibility

| App | LAN (HTTP :8866) | Public (HTTPS via Cloudflare Tunnel) |
|-----|-------------------|--------------------------------------|
| Apple Calendar | ✓ | ✓ |
| Thunderbird | ✓ | ✓ |
| Outlook desktop | ✓ | ✓ |
| Google Calendar | ✗ (HTTPS only) | ✓ |
| Outlook.com | ✗ (HTTPS only) | ✓ |

## Scheduled Jobs

1. **Daily at 02:00** — Fetch UKHO data, store, update calendar feeds for all enabled moorings
2. **Dynamic (per-mooring, at each worst-case grounding)** — Wind observation at the moment each wind-enabled vessel could first ground (`drying + draught + shallow offset`); adjusts the *start* of that mooring's next access window. Re-enumerated on the daily fetch, on startup, on manual refresh, and after a config change; a 15-minute safety net rebuilds the jobs if a restart clears them.

## Model Accuracy

The harmonic model was calibrated in April 2026 against 388 Admiralty reference points (91 HW/LW events, 7 spring HW references, and 288 half-hourly height samples) spanning April to December 2026. It was then validated in April 2026 against a further 710 Admiralty Portsmouth reference points (355 HW + 355 LW) drawn from six months spanning July 2026 to December 2027.

The validation exercise identified a consistent systematic bias: the mathematical peak of the harmonic curve falls approximately 34 minutes later than the Admiralty's published HW time, and the mathematical trough about 28 minutes later than the published LW time. The most plausible explanation is a convention difference — the Admiralty's published HW/LW times are likely to correspond to the mid-point of the Solent's extended stand rather than the mathematical turning point — but this has not been independently verified. `predict_events()` applies a post-processing shift to align reported event timestamps with Admiralty convention. Heights at any given clock time are unaffected — the underlying tidal curve used for access-window calculations is not modified.

Accuracy after the Admiralty-convention offset is applied, measured against the 710-point validation set:

| Metric | HW | LW |
|--------|-------|-------|
| Timing bias (mean) | +0.2 min | −0.1 min |
| Timing standard deviation | 14.6 min | 19.5 min |
| Height bias (mean) | +0.02m | +0.05m |
| Height standard deviation | 0.13m | 0.19m |

The Langstone secondary port offset (Portsmouth → Langstone: +9min, +0.05m HW) was validated against UKHO half-hourly data for both ports in April 2026. The earlier figure of +0.24m HW height offset was reduced to +0.05m based on observed data; LW times and heights are effectively identical between the two ports.

UKHO data remains the most accurate source (Langstone-native, 7-day range). The harmonic model provides unlimited-range estimates with the accuracy above — events from this source are prefixed "est." in calendar titles.


## UKHO API Licensing

This application uses the **Discovery tier** (free) of the Admiralty Tidal API for development and proof-of-concept purposes. The Discovery tier terms do not permit caching of data. For production use with persistent data storage, upgrade to the **Foundation tier**. See [UKHO developer portal](https://admiraltyapi.portal.azure-api.net/).

## Tidal Curve (v2.8)

Beneath the Current Conditions dashboard, a collapsible **Tidal Curve** panel renders today's predicted Langstone tidal height on a 24-hour, 0–5.5m chart. The panel is always-visible (outside the tab views) so it remains useful regardless of which tab is active.

### What's drawn

- The full-day height curve, sampled at 5-minute resolution using the same asymmetric Langstone tidal model that the access-window calculator uses internally (so the curve and the windows can never disagree).
- A shaded area under the curve, a dashed Chart Datum reference line at 0m, and gridlines every 1m up to 5.5m.
- HW and LW markers with small labels showing the predicted height and local clock time (e.g. `H 4.49m 17:05`, `L 1.36m 09:32`).
- A red **vertical "now" line** at the current local time (only on today's date), with a small label reading the present-moment tide height. Updates every 5 minutes; the day rolls over automatically at local midnight.
- **Shaded access bands** for the loaded mooring (see *Access threshold lines and shaded bands* below).
- **Sunrise and sunset markers** and faint night-period shading (see *Sunrise and sunset* below).

### Hover crosshair

Moving the mouse (or dragging a finger on touch devices) over the chart snaps a navy crosshair to the nearest 5-minute sample. Dashed lines extend from the snap point to both axes, and a small tooltip near the cursor shows `HH:MM — H.HH m`.

### Access threshold lines and shaded bands

When a Mooring ID is entered (or loaded from a stored config), two horizontal dashed green lines appear:

- **Access** — at `drying_height + draught + safety_margin`, the tide height the boat needs to clear to be safely afloat with the configured margin.
- **Tender access** — at `drying_height + tender_min_depth_m`, drawn only when Tender Access is enabled in the configuration.

Each threshold also drives a coloured **band** highlighting the time spans when access is available:

- **Solid green wedge** between the Access line and the curve, marking the windows when the boat itself has water.
- **Diagonally hatched ribbon** between the Tender line and either the curve or the Access line (whichever is lower), marking the tender-only windows that bracket the boat windows.

The two bands never overlap — the hatched ribbon caps at the Access line — so the two-tier hierarchy reads cleanly: solid green = "boat can move", hatch = "tender only".

The lines and bands update live as the user edits any of the relevant inputs in the Configuration panel — no need to save or recalculate for the chart to reflect new values. If a threshold exceeds the 5.5m chart ceiling (e.g. an unusually deep-draught boat on a high mooring), the line is clipped at the top and labelled `↑ Access > 5.5 m` to make the off-scale value visible without misrepresenting it.

The Access line shows the **baseline** threshold; it does not represent the wind-offset adjustment (which is dynamic per-tide and per-cycle).

### Spring/Neap/Mid classification

A small chip on the Current Conditions tide column and an inline label in the Tidal Curve header indicate whether the displayed day's tides are running spring, neap, or mid-range. Classification is computed in `app/tide_state.py` by comparing the day's highest predicted High Water against percentile thresholds (p30 / p70) derived from a rolling 90-day window of UKHO HW heights ending at the displayed date:

- HW ≥ p70 → **Spring tide**
- HW ≤ p30 → **Neap tide**
- otherwise → **Mid tide**

Percentiles are computed from local data rather than hard-coded so the classifier auto-tunes as the dataset grows and across seasonal drift. When fewer than 14 stored HW samples are available, classification is suppressed (no chip shown) rather than guessed.

### Sunrise and sunset

Two gold dashed vertical markers, with compact `Sunrise HH:MM` / `Sunset HH:MM` labels above the x-axis, indicate the displayed day's astronomical sunrise and sunset. The pre-dawn (00:00 → sunrise) and post-dusk (sunset → 24:00) regions of the chart are faintly tinted navy so daylight hours are visible at a glance — useful for deciding whether a planned departure or return falls in the dark.

Sun times are computed astronomically from the configured `LOCATION_LAT` / `LOCATION_LON` via the [`astral`](https://astral.readthedocs.io/) Python package. No external API call is made; the calculation is deterministic and works for any past or future date.

### Date selector

Chevron buttons in the panel header let the user pan to other days; a separate **Today** button (visible only when off today) jumps back to the present. The selectable range is bounded by `[earliest stored UKHO date, today + 7]` — the seven-day forward horizon matches the UKHO API window. Chevrons grey out at the bounds.

The lazy UKHO fetch (one-shot retrieval when today's data is missing) is **only triggered for the today's date**. Panning to past or future days never triggers a UKHO call — if data isn't stored for the selected date, the panel shows "UKHO data unavailable for this date" rather than spamming the API. The daily 02:00 scheduler keeps the next-7-days window populated.

When viewing a date that isn't today, the red "now" line is hidden (it would be meaningless). The access threshold lines, shaded bands, Spring/Neap classification, sunrise/sunset markers, and hover crosshair all work as on the today view.

### Data source and recalibration

The curve uses a **date-based source switch** (v2.8.1):

- **Today and the next 6 days** are drawn from UKHO data. If today's data is missing from the database, the panel triggers a one-shot UKHO fetch (subject to a 30-second cooldown). The placeholder switches from "Loading tide curve…" to "Fetching today's UKHO data…" if the fetch takes more than a second. Lazy fetch is **only** triggered for today's date — panning to other days never hits UKHO. If UKHO data is unavailable for a date in the 0..6 range, the panel shows "UKHO data unavailable for this date" with a Retry link.
- **Day 7 onwards** uses stored harmonic predictions. The source badge changes to `Harmonic · est.` and a small italic disclaimer appears under the chart noting typical accuracy (±15 min on event times, ±0.15 m on heights). No lazy generation is offered; the data is server-side authoritative.

The cutoff is fixed by date, not by data availability, so curves are never mixed-source on a single day.

Harmonic predictions are regenerated automatically every night at 02:00 alongside the UKHO refresh; this keeps a rolling 180-day forward horizon populated. On a fresh deployment, a one-shot **startup warm-up** runs in the FastAPI lifespan if the `harmonic_predictions` table is empty, so harmonic-source days are reachable immediately rather than waiting for the first 02:00 job.

#### Manual regeneration after recalibration

The harmonic constants live in `app/model_config.json` and are baked into the Docker image at build time. After editing the JSON and running `docker compose up -d --build`, the next 02:00 scheduler job will pick up the new constants — but you can refresh stored predictions immediately:

```
docker exec tidal-access python -m scripts.regenerate_harmonic
```

The script regenerates the next 180 days of HW/LW predictions using the current constants, applies the Portsmouth → Langstone secondary-port offset, and inserts the rows with a fresh `generated_at` timestamp. `get_harmonic_predictions(latest_only=True)` always returns the freshest version per cycle, so older predictions retire silently. Old rows are retained for 365 days (per the existing `cleanup_old_harmonic_predictions` policy) so residual analysis against the previous model remains possible.

### Behaviour across tabs

The panel is expanded by default on first load and persists its collapsed/expanded state to `localStorage`. To reduce visual noise on admin views, the panel **auto-collapses when the Mooring Configs or System Activity tab is selected** and restores the user's persisted intent when switching back to Tides or Access Windows. Auto-collapsing does not overwrite the saved state — manually collapsing the panel on the Tides tab is still what's remembered between sessions.

### Page layout

In v2.8 the four tab buttons were moved out of the page body and into a static, full-width menu bar flush with the bottom of the navy header. A faint dashed separator below the Tidal Curve marks the boundary between always-visible page-level content (header, menu bar, Current Conditions, Tidal Curve) and the tab-specific views below it.

### Current Conditions enhancements

The Current Conditions dashboard gains two small but useful additions in v2.8:

- The Spring/Neap/Mid chip (described above) appears in the Tide column next to the state description.
- All three upcoming tide events now carry an "in Nh Mm" countdown (previously only the next event did), making it easier to think relatively when planning a short trip.

## Wind Offset

For swing moorings at the edge of a channel, the effective drying height depends on which way the boat lies as the tide drops. The wind offset feature models this:

1. Configure the direction of shallow water relative to the mooring (N/NE/E/SE/S/SW/W/NW)
2. Specify the additional drying height on the shallow side
3. A per-mooring wind check runs at the vessel's **worst-case grounding** — the moment the tide falls to `drying + draught + shallow offset`, i.e. the earliest the boat could touch if the wind pushed it into the shallows. If the wind is then blowing the boat toward the shallow side, the extra drying height is applied to the **start** of that mooring's next access window (the boat re-floats later). The next grounding triggers the next check, and so on down the chain.

The offset uses a three-sector trigger: if shallow water is to the W, the offset activates when wind is from E, NE, or SE.

If the offset is large enough that even the next high water no longer clears the safe threshold, that tide is shown as **"No access — wind-blown to shallows"** in the feed rather than as a window. Conversely, a tide that would otherwise be "always afloat" is re-evaluated and shown to ground if the wind pushes the boat shallow.

Trot (fore-and-aft) moorings that cannot swing, or deep-channel moorings with negligible depth variation, should leave this feature disabled.

### Calibrating the wind offset from observations

An aground observation (or a v2.10 depth sounding) only contributes to wind-offset calibration when the nearest wind sample within the same tidal cycle shows wind pushing toward the shallow side, **and** the observed direction of lay matches that wind direction within one compass sector. Aground observations recorded when the wind was not pushing toward shallow carry no information about the magnitude of the offset — they tell you the boat grounded, but not whether the shallow side was involved — and are treated as base drying observations instead.

The calibration also depends on the currently stored base drying height: the implied minimum offset is `observed_height − draught − drying_height`. If the base drying height is updated via the Apply button, the wind-offset suggestion should be re-checked, since its arithmetic baseline has shifted. The UI shows an inline note recording which base drying height was used for the current suggestion.

## Barometric Pressure Correction (v2.9)

UKHO predictions (and the harmonic model, which is calibrated to UKHO) assume **average** barometric pressure. Real sea level departs from that prediction with the barometer — the *inverse barometer* effect: low pressure raises observed water above the prediction, high pressure depresses it. The barometric correction adjusts predicted tide **heights** (and therefore the computed access windows) for the deviation of the forecast pressure from a reference, before the windows are computed:

```
correction_m = clamp( (P_ref − P_event) × k , ±max )
```

with `P_ref = 1013.25 hPa`, `k = 0.0100 m/hPa`, and a `±0.30 m` clamp (a synoptic pressure swing seldom moves the static level more than that; exceeding it implies bad data or a storm surge, which this correction does not model). Low pressure → positive correction → **more** water; high pressure → negative correction → **less** water. The coefficient `k` is a regional physical constant fitted offline against the Portsmouth tide gauge — see [docs/CALIBRATION_NOTES.md](docs/CALIBRATION_NOTES.md).

This is distinct from, and composes with, the wind offset: the wind offset shifts the **threshold** (effective drying height under the boat); the barometric correction shifts the **water height**. The two are orthogonal and both can apply to the same window.

### How it is gated

Three conditions must all hold for a window to be corrected:

1. **System master** — `barometric.enabled` in `model_config.json` (**currently enabled**; the code falls back to `false` only if the key is absent or malformed). A single rollout/kill switch for the whole feature.
2. **Per-mooring opt-in** — a toggle in each mooring's config panel (`barometric_enabled`, default off), beside the wind-offset settings.
3. **A fresh forecast** covering the event time. Pressure comes from the OpenWeatherMap 5-day / 3-hourly forecast, fetched and stored once per day by the 02:00 job. Beyond the forecast horizon (~5 days) **no** correction is applied — stale pressure is never extrapolated. An individual event whose forecast is missing or older than `forecast_staleness_hours` (default 36 h, tolerating one missed daily fetch) reverts to its uncorrected baseline; a single failed fetch never wholesale-reverts a feed.

The correction **value** is system-level (one pressure series, one `k` — every opted-in mooring gets the identical shift at a given tide); only the **opt-in decision** is per-mooring. The anonymous calculate path (no mooring selected) always shows the pure, uncorrected forecast.

**Two skippers can see different window times for the same tide** — one opted in, one not — exactly as wind-adjusted windows already differ between moorings. This is intended.

### Feeds, churn, and rounding

- A **standalone** pressure-corrected tide feed (`Langstone_UKHO_7d_PressureCorrected.ics`, above) is height-corrected with stable event times and UIDs; opting in is simply subscribing to that URL.
- For **per-mooring** access-window feeds, a height correction moves the threshold-crossing *times*. Stored windows keep full precision; on the daily regeneration an event is only rewritten when a raw window edge moved by at least `window_deadband_minutes` (default 5 min) versus the currently-stored edge — so small day-to-day forecast jitter does not churn the feed or flicker the displayed slot, while a genuine synoptic change propagates. A calibration **Apply** or a wind grounding job carries the correction too, so those write paths stay consistent with the daily one.
- Independently of barometric, **all** displayed access-window edges (vessel and tender, corrected or not) are rounded to the nearest 5 minutes, *conservative inward*: the start rounds up (later), the end rounds down (earlier), so a displayed window always sits inside the computed one and never overstates access. A window that collapses below one grid step is shown as negligible (no usable access) rather than as an inverted or zero-length event. The model's own timing uncertainty is ~15–20 min, so this rounding removes false precision without losing real information.

### Enabling

The system master ships **enabled** (`barometric.enabled = true`), so the standalone pressure-corrected tide feed is live and the current barometric effect at the live measured pressure is surfaced in the Current Conditions panel. Per-mooring access-window correction is still **opt-in and off by default**: it activates for a mooring only once that mooring's `barometric_enabled` toggle is set from its config panel. To turn the whole feature off, set `barometric.enabled` to `false` in `app/model_config.json` and rebuild the image (`docker compose up -d --build`, since the config is bundled, not mounted).
