# Tidal Access Window Predictor

A Docker-containerised application for predicting when a boat on a swing mooring in Langstone Harbour has sufficient water depth to depart and arrive.

**Version 2** — adds per-mooring 6-digit PIN protection, decouples iCal feed updates from calculation, and restructures the calibration system to split base drying height from shallow-side wind offset. The v1.0 release is preserved on the tag `v1.0`.

## Overview

The tool computes access windows — the periods around each high water when the tide height exceeds the sum of the mooring's drying height, the boat's draught, and a safety margin. It uses three data sources in priority order:

| Source | Range | Origin | Persisted | Offset Applied |
|--------|-------|--------|-----------|----------------|
| **UKHO** | 7 days | Admiralty Tidal API (Langstone native) | Yes | No (native data) |
| **KHM** | ~1 month | Manual paste from Royal Navy Portsmouth tables | Yes (flagged, overwritten by UKHO) | Yes (Portsmouth → Langstone: +9min, +0.05m HW) |
| **Harmonic** | Unlimited | Built-in harmonic model (19 constituents, calibrated April 2026) | **No** (display only) | Yes (Portsmouth → Langstone: +9min, +0.05m HW) |

### Key Features

- **Per-mooring configuration** with persistent storage keyed by mooring number (1–100)
- **6-digit PIN protection** — each mooring is protected against casual alteration by third parties; see **PIN Protection** below for the full model
- **Empirical calibration** — record observations (afloat/aground) to refine the drying height estimate, with confidence rating
- **Subscribable iCal feed** per mooring, auto-updated daily, with proper subscription metadata
- **Wind offset** — adjusts the next tide's access window based on observed wind direction and the mooring's shallow-water geometry
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
│   ├── khm_parser.py         # KHM table parser (13-column format)
│   ├── harmonic.py           # Harmonic prediction (19 constituents, Doodson args)
│   ├── secondary_port.py     # Portsmouth → Langstone offset
│   ├── wind.py               # OWM client + offset logic
│   ├── access_calc.py        # Window calculation engine
│   ├── ical_manager.py       # iCal feed + export generation
│   ├── scheduler.py          # APScheduler jobs
│   ├── model_config.json     # Model parameters reference
│   └── static/index.html     # Web UI
└── data/                     # Docker volume (persistent)
    ├── tides.db              # SQLite database
    ├── model_config.json     # Editable model parameters
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
| `WIND_SAMPLE_HW_OFFSET_HOURS` | Hours after HW to sample wind | `4` |
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

The daily UKHO refresh (02:00 local) and the HW+4h wind observation job update stored events and regenerate feeds by calling internal functions directly, not through the HTTP API. They therefore bypass PIN gating by design. This is consistent with the threat model: the scheduler executes work that the mooring owner has already authorised at configuration time (by enabling calendar subscription and/or wind offset). An attacker who can reach the scheduler has already broken into the host.

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
2. **Dynamic (HW+4h)** — Wind observation at configurable offset after each HW, recalculates next tide's window for wind-enabled moorings

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

UKHO data remains the most accurate source (Langstone-native, 7-day range). KHM data is second-most accurate within its ~30-day range. The harmonic model provides unlimited-range estimates with the accuracy above — events from this source are prefixed "est." in calendar titles.


## UKHO API Licensing

This application uses the **Discovery tier** (free) of the Admiralty Tidal API for development and proof-of-concept purposes. The Discovery tier terms do not permit caching of data. For production use with persistent data storage, upgrade to the **Foundation tier**. See [UKHO developer portal](https://admiraltyapi.portal.azure-api.net/).

## Wind Offset

For swing moorings at the edge of a channel, the effective drying height depends on which way the boat lies as the tide drops. The wind offset feature models this:

1. Configure the direction of shallow water relative to the mooring (N/NE/E/SE/S/SW/W/NW)
2. Specify the additional drying height on the shallow side
3. The system checks observed wind at HW+4h — if the wind was pushing the boat toward the shallow side, the extra drying height is added to calculations for the next flood tide

The offset uses a three-sector trigger: if shallow water is to the W, the offset activates when wind is from E, NE, or SE.

Trot (fore-and-aft) moorings that cannot swing, or deep-channel moorings with negligible depth variation, should leave this feature disabled.

### Calibrating the wind offset from observations

An aground observation only contributes to wind-offset calibration when the HW+4h wind sample of the preceding cycle shows wind pushing toward the shallow side, **and** the observed direction of lay matches that wind direction within one compass sector. Aground observations recorded when the wind was not pushing toward shallow carry no information about the magnitude of the offset — they tell you the boat grounded, but not whether the shallow side was involved — and are treated as base drying observations instead.

The calibration also depends on the currently stored base drying height: the implied minimum offset is `observed_height − draught − drying_height`. If the base drying height is updated via the Apply button, the wind-offset suggestion should be re-checked, since its arithmetic baseline has shifted. The UI shows an inline note recording which base drying height was used for the current suggestion.
