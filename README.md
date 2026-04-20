# Tidal Access Window Predictor

A Docker-containerised application for predicting when a boat on a swing mooring in Langstone Harbour has sufficient water depth to depart and arrive.

## Overview

The tool computes access windows — the periods around each high water when the tide height exceeds the sum of the mooring's drying height, the boat's draught, and a safety margin. It uses three data sources in priority order:

| Source | Range | Origin | Persisted | Offset Applied |
|--------|-------|--------|-----------|----------------|
| **UKHO** | 7 days | Admiralty Tidal API (Langstone native) | Yes | No (native data) |
| **KHM** | ~1 month | Manual paste from Royal Navy Portsmouth tables | Yes (flagged, overwritten by UKHO) | Yes (Portsmouth → Langstone: +9min, +0.05m HW) |
| **Harmonic** | Unlimited | Built-in harmonic model (19 constituents, calibrated April 2026) | **No** (display only) | Yes (Portsmouth → Langstone: +9min, +0.05m HW) |

### Key Features

- **Per-mooring configuration** with persistent storage keyed by mooring number (1–100)
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

Individual observations can be deleted, or all observations for a mooring can be cleared.

## iCal Feeds

Each mooring with calendar subscription enabled gets a stable URL:
```
https://tsctide.uk/feeds/mooring_42.ics
```
On the local network, `http://localhost:8866/feeds/mooring_42.ics` also works.

The feed includes `REFRESH-INTERVAL` and `X-PUBLISHED-TTL` metadata for proper calendar app subscription behaviour. Events use cycle-based UIDs that are stable across data sources — upgrading from harmonic to UKHO data replaces events rather than duplicating them.

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

The harmonic model was calibrated in April 2026 against 388 Admiralty reference points (91 HW/LW events, 7 spring HW references, and 288 half-hourly height samples) spanning April to December 2026. Fit quality:

| Metric | Value |
|--------|-------|
| Height RMS (half-hourly points) | ~0.10m |
| Height RMS (HW/LW peaks) | ~0.39m (dominated by Solent stand effect) |
| HW timing standard deviation | ~17 min |
| LW timing standard deviation | ~19 min |

The larger HW/LW peak RMS reflects the Solent's extended HW stand: the mathematical peak of the harmonic model falls earlier than the published HW time, which represents the mid-point of the stand. For access window calculations this is not significant since the curve shape (and therefore the threshold crossing times) is accurate.

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
