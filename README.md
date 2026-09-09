# Signals

Private public-infrastructure and signals-mapping skill for agentic platforms.

Locates and maps publicly accessible cameras, WiFi networks, cell towers, LPR/ALPR
devices, live aircraft, maritime vessels, and satellites for any location worldwide.

All sources are public. All streams are intentionally broadcast or registered in open
databases. No private systems are accessed. No authentication is bypassed.

---

## What it finds

| Type | Source | API Key Required |
|---|---|---|
| Cameras | Shodan + Insecam fallback | Yes (Shodan) |
| WiFi networks | Wigle | Yes |
| Cell towers | OpenCelliD | Yes |
| LPR / ALPR devices | OpenStreetMap Overpass | No |
| Live flights | OpenSky Network | No |
| Maritime vessels | AISstream / AISHub | Optional |
| Satellites | TLE API + skyfield / N2YO | Optional |

---

## Contents
signals/

├── SKILL.md           — skill definition and agent usage reference

├── SETUP.md           — API key registration and setup instructions

├── signals-search.py  — data collection engine

└── signals-map.py     — self-contained Leaflet HTML map generator

---

## Requirements

**Python 3.9+**

```bash
pip3 install requests websockets skyfield
```

`skyfield` is optional. Without it, satellite tracking falls back to N2YO API
if `N2YO_API_KEY` is set.

---

## Quick start

```bash
# Dry run — geocode only, no API calls
python3 signals-search.py --location "Times Square, New York" --radius 1000 --types cameras --dry-run

# Camera scan — text output
python3 signals-search.py --location "Times Square, New York" --radius 1000 --types cameras --output text

# Full scan — all types
python3 signals-search.py --location "Temple Bar, Dublin" --radius 1000 --types all --output text

# Generate HTML map
python3 signals-search.py --location "Fort Lauderdale" --radius 5000 --types all | \
python3 signals-map.py --output map.html --open
```

---

## API keys

Only `SHODAN_API_KEY` is required. All others are optional and enable additional
data types. Set as environment variables before running.

| Variable | Service | Notes |
|---|---|---|
| `SHODAN_API_KEY` | shodan.io | Required. Free tier works. |
| `WIGLE_API_NAME` | wigle.net | Optional. Enables WiFi mapping. |
| `WIGLE_API_TOKEN` | wigle.net | Optional. Pair with API name. |
| `OPENCELLID_KEY` | opencellid.org | Optional. Enables cell tower mapping. |
| `AISSTREAM_KEY` | aisstream.io | Optional. Enables live vessel tracking. |
| `AISHUB_USER` | aishub.net | Optional. Maritime fallback. |
| `N2YO_API_KEY` | n2yo.com | Optional. Satellite fallback without skyfield. |

See [SETUP.md](SETUP.md) for registration links and step-by-step instructions.

---

## Installation on agentic platforms

Clone the repo and reference the skill folder in your agent configuration:

```bash
git clone git@github.com-haroldmansfield:HaroldMansfield/signals.git
```

Compatible with OpenClaw, Claude Code, Claude Cowork, Cursor, Gemini CLI,
and any platform that supports the OpenClaw SKILL.md skill format.

---

## Usage — radius guidelines

| Radius | Use case |
|---|---|
| 250m | Single building or venue |
| 500m | City block |
| 1000m | Neighbourhood |
| 2000m | Wide area sweep |
| 5000m | District |
| 50000m+ | City-wide (flights, ships, satellites) |

---

## License

MIT
