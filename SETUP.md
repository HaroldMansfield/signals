# Signals — Setup Guide

## Step 1: Python dependencies

```bash
pip3 install requests websockets skyfield --break-system-packages
```

`skyfield` is optional. Without it, satellite tracking uses N2YO API if
`N2YO_API_KEY` is configured, otherwise satellites are skipped.

---

## Step 2: API keys

Only `SHODAN_API_KEY` is required. All others enable additional data types.

### Shodan (required — cameras)

1. Register at https://account.shodan.io/register
2. After login go to https://account.shodan.io
3. Copy your API key
4. Set: `export SHODAN_API_KEY="your_key"`

Free tier supports city-based camera and device queries.

---

### Wigle (optional — WiFi networks)

1. Register at https://wigle.net
2. Go to Account > API Token
3. Copy both the API Name and API Token
4. Set:
export WIGLE_API_NAME="AID_xxxxxxxxxx"

export WIGLE_API_TOKEN="your_token"

---

### OpenCelliD (optional — cell towers)

1. Register at https://opencellid.org
2. Go to your profile page and copy the API key
3. Set: `export OPENCELLID_KEY="your_key"`

Free tier gives full access to the cell tower database.

---

### AISstream (optional — maritime vessel tracking)

1. Register at https://aisstream.io
2. Go to dashboard and create an API key
3. Set: `export AISSTREAM_KEY="your_key"`

Free tier covers real-time vessel positions globally via WebSocket.

---

### AISHub (optional — maritime fallback)

1. Register at https://www.aishub.net/register
2. Must contribute AIS data to get API access
3. Set: `export AISHUB_USER="your_username"`

Used as fallback if AISstream is unavailable or unconfigured.

---

### N2YO (optional — satellite fallback)

1. Register at https://www.n2yo.com/api
2. Copy your API key
3. Set: `export N2YO_API_KEY="your_key"`

Only needed when `skyfield` is not installed.

---

## Step 3: Verify

Dry run — geocode only, no API calls:

```bash
python3 signals-search.py --location "your city" --radius 1000 --types cameras --dry-run
```

Live test:

```bash
python3 signals-search.py --location "your city" --radius 1000 --types cameras --output text
```
