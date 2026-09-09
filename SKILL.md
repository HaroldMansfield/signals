---
name: signals
description: >
  Public infrastructure intelligence skill. Locates publicly accessible
  cameras, WiFi networks, cell towers, LPR/ALPR devices, live flights,
  maritime vessels, and satellites for any location. Use this skill when
  asked to scan infrastructure around a location, find cameras or surveillance
  devices, track aircraft or vessels, or map wireless networks. Triggers on
  requests containing: cameras, surveillance, WiFi, cell towers, LPR, ALPR,
  flights overhead, ships nearby, satellites, signals scan, or "what's around
  [location]". Two scripts are bundled: signals-search.py for data collection
  and signals-map.py for HTML map generation.
metadata:
  openclaw:
    requires:
      env:
        - SHODAN_API_KEY
      bins:
        - python3
    primaryEnv: SHODAN_API_KEY
    envVars:
      - name: SHODAN_API_KEY
        required: true
        description: Shodan API key. Free tier at shodan.io supports city-based camera queries.
      - name: WIGLE_API_NAME
        required: false
        description: Wigle API name (username). Free account at wigle.net under Account > API Token.
      - name: WIGLE_API_TOKEN
        required: false
        description: Wigle API token. Pair with WIGLE_API_NAME.
      - name: OPENCELLID_KEY
        required: false
        description: OpenCelliD API key. Free registration at opencellid.org.
      - name: AISSTREAM_KEY
        required: false
        description: AISstream WebSocket API key. Free tier at aisstream.io.
      - name: AISHUB_USER
        required: false
        description: AISHub contributor username. Alternative maritime source at aishub.net.
      - name: N2YO_API_KEY
        required: false
        description: N2YO satellite API key. Free tier at n2yo.com/api. Fallback when skyfield unavailable.
    emoji: "📡"
---

# Signals

Private public-infrastructure and signals-mapping skill. Locates and
maps publicly accessible cameras, WiFi networks, cell towers, LPR/ALPR devices,
live aircraft, maritime vessels, and satellites for any location worldwide.

All sources are public. All streams are intentionally broadcast or registered in
open databases. No private systems are accessed. No authentication is bypassed.

---

## Scripts

Both scripts are bundled in this skill folder.

**signals-search.py** — data collection engine
**signals-map.py** — Leaflet HTML map generator

Run from the skill folder:

```bash
python3 signals-search.py --location "..." --radius 1000 --types all
```

---

## Required Setup

### Python dependencies

```bash
pip3 install requests websockets skyfield --break-system-packages
```

`skyfield` is optional. Without it, satellite tracking falls back to N2YO API
if `N2YO_API_KEY` is set, otherwise satellites are skipped.

See SETUP.md for API key registration links.

---

## signals-search.py
python3 signals-search.py --location LOCATION --radius METRES [--types TYPE] [--output FORMAT] [--dry-run]

| Argument | Values | Default | Notes |
|---|---|---|---|
| `--location` | address, landmark, or lat,lon | required | Geocoded via Nominatim |
| `--radius` | integer (metres) | required | See radius guidelines below |
| `--types` | cameras, wifi, cells, lpr, flights, ships, satellites, all | all | Comma-separated |
| `--output` | json, text | json | json for piping to map; text for terminal |
| `--dry-run` | flag | off | Geocode only, no API calls |

### Radius guidelines

| Radius | Use case |
|---|---|
| 250m | Single building or venue |
| 500m | City block |
| 1000m | Neighbourhood |
| 2000m | Wide area |
| 5000m | District |
| 50000m+ | City-wide (flights, ships, satellites) |

### Examples

```bash
# All types — 1km radius
python3 signals-search.py --location "Temple Bar, Dublin" --radius 1000 --types all --output text

# Cameras and WiFi only
python3 signals-search.py --location "Times Square, New York" --radius 500 --types cameras,wifi --output text

# Coordinates input
python3 signals-search.py --location "40.7580,-73.9855" --radius 1000 --types all

# Dry run — geocode only
python3 signals-search.py --location "Las Vegas Strip" --radius 5000 --types cameras --dry-run
```

---

## signals-map.py

Generates a self-contained dark-theme Leaflet HTML map from signals-search.py JSON output.

```bash
# Pipe directly
python3 signals-search.py --location "Fort Lauderdale" --radius 5000 --types all | \
python3 signals-map.py --output /tmp/signals-map.html

# From saved JSON
python3 signals-map.py --input results.json --output map.html

# Generate and open in browser
python3 signals-map.py --input results.json --output map.html --open
```

---

## Data sources by type

| Type | Primary source | Fallback |
|---|---|---|
| Cameras | Shodan API | Insecam.org (automatic) |
| WiFi | Wigle API | None |
| Cell towers | OpenCelliD API | None |
| LPR/ALPR | OpenStreetMap Overpass | None |
| Flights | OpenSky Network | Local readsb JSON |
| Ships | AISstream WebSocket | AISHub REST API |
| Satellites | tle.ivanstanojevic.me + skyfield | N2YO API |

---

## Output format

### Text output
SIGNALS REPORT — 2026-06-17T15:30:00Z

Location : Temple Bar, Dublin

Resolved : 53.345618,6.266970

Radius   : 1000m
── CAMERAS (4 results)

⚠ OPEN/UNAUTHENTICATED: 2

[CAM-001] Temple Bar Webcam | 185.x.x.x:80 | OPEN

LINK: http://185.x.x.x:80/

### JSON output

Structured output with `meta` block and `data` array, one object per type.
Pipe directly to signals-map.py for HTML map generation.

---

## Agent usage pattern
Run a signals scan for [location] within [radius]m covering [types].

Use signals-search.py from the skill folder.

Output as text and generate an HTML map at [output path].
