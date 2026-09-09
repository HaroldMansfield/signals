#!/usr/bin/env python3
"""
signals-search.py — Samaritan Signals Agent search engine
Geocodes a location then queries Shodan, Wigle, and OpenCelliD in parallel.

Usage:
  python3 signals-search.py --location "Temple Bar, Dublin" --radius 500 --types all
  python3 signals-search.py --location "40.7580,-73.9855" --radius 1000 --types cameras,wifi
  python3 signals-search.py --location "Las Vegas Strip" --radius 5000 --types cameras --dry-run
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import math

try:
    from skyfield.api import load, wgs84, EarthSatellite
    from skyfield.timelib import Time
    SKYFIELD_AVAILABLE = True
except ImportError:
    SKYFIELD_AVAILABLE = False

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    print("ERROR: requests not installed. Run: pip3 install requests --break-system-packages")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SHODAN_API_KEY  = os.environ.get("SHODAN_API_KEY", "")
WIGLE_API_NAME  = os.environ.get("WIGLE_API_NAME", "")
WIGLE_API_TOKEN = os.environ.get("WIGLE_API_TOKEN", "")
OPENCELLID_KEY  = os.environ.get("OPENCELLID_KEY", "")

NOMINATIM_URL   = "https://nominatim.openstreetmap.org/search"
SHODAN_URL      = "https://api.shodan.io"
WIGLE_URL       = "https://api.wigle.net/api/v2"
OPENCELLID_URL  = "https://opencellid.org/cell/getInArea"

HEADERS = {"User-Agent": "signals/1.0"}

OPENSKY_URL     = "https://opensky-network.org/api"
CELESTRAK_URL   = "https://celestrak.org/SATCAT/elements.php"   # unreachable from some networks
TLE_API_URL     = "https://tle.ivanstanojevic.me/api/tle"        # primary TLE source
N2YO_URL        = "https://api.n2yo.com/rest/v1/satellite"
AISHUB_USER     = os.environ.get("AISHUB_USER", "")
AISSTREAM_KEY   = os.environ.get("AISSTREAM_KEY", "")
AISSTREAM_URL   = "wss://stream.aisstream.io/v0/stream"
N2YO_API_KEY    = os.environ.get("N2YO_API_KEY", "")
READSB_URL      = "http://localhost:8080/aircraft.json"

# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(location: str) -> tuple[float, float]:
    """Convert address/landmark to (lat, lon). Returns tuple or raises."""
    # Already coordinates?
    parts = location.split(",")
    if len(parts) == 2:
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass

    # Normalise intersection formats: "5th Ave and 42nd St" -> "5th Ave & 42nd St"
    import re as _re
    normalised = _re.sub(r'\s+and\s+', ' & ', location, flags=_re.IGNORECASE)

    # Try multiple query variants to maximise geocoding success
    attempts = list(dict.fromkeys([normalised, location]))  # deduped, normalised first

    for attempt in attempts:
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"q": attempt, "format": "json", "limit": 1},
                headers=HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                display = results[0].get("display_name", attempt)
                print(f"[geocode] {location} -> {lat:.6f}, {lon:.6f}", file=sys.stderr)
                print(f"[geocode] Resolved: {display}", file=sys.stderr)
                return lat, lon
        except Exception:
            continue

    raise ValueError(f"Nominatim could not geocode: {location}")

# ── Shodan — Cameras ──────────────────────────────────────────────────────────



def _fetch_camera_title(ip: str, port: int) -> str:
    """Fetch the HTML title tag from a camera web interface."""
    import re
    try:
        url = f"http://{ip}:{port}/"
        resp = requests.get(url, headers=HEADERS, timeout=5, verify=False)
        html = resp.text
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            title = m.group(1).strip()
            if title and title.lower() not in ("", "camera", "webcam", "ip camera", "untitled", "index page", "index", "home", "live view", "video", "network camera", "ip webcam"):
                return title
    except Exception:
        pass
    return ""


def _scrape_insecam_page(cam_id: str) -> tuple:
    """Scrape an Insecam camera detail page for title, location, manufacturer, lat, lon."""
    import re
    title, location, manufacturer, lat, lon = "", "", "", None, None
    try:
        url = f"https://www.insecam.org/en/view/{cam_id}/"
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        html = resp.text

        # Title
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1).strip()
            # Strip "Insecam - " prefix
            title = re.sub(r"^insecam\s*[-—]\s*", "", raw, flags=re.IGNORECASE).strip()

        # Location — look for city/country in page
        m = re.search(r'class="[^"]*location[^"]*"[^>]*>(.*?)<', html, re.IGNORECASE | re.DOTALL)
        if m:
            location = re.sub(r"<[^>]+>", "", m.group(1)).strip()

        # Manufacturer — look for brand/model fields
        m = re.search(r'(?:manufacturer|brand|model)[^:]*:[\s]*([^<]{2,40})', html, re.IGNORECASE)
        if m:
            manufacturer = m.group(1).strip()

        # Coordinates — Insecam embeds them in a map init or data attributes
        m = re.search(r'lat[itude]*["\s:=]+([+-]?\d+\.\d+)', html, re.IGNORECASE)
        if m:
            lat = float(m.group(1))
        m = re.search(r'l(?:on|ng)[gitude]*["\s:=]+([+-]?\d+\.\d+)', html, re.IGNORECASE)
        if m:
            lon = float(m.group(1))

        # Also try to get camera description from page heading
        m = re.search(r'<h[12][^>]*>(.*?)</h[12]>', html, re.IGNORECASE | re.DOTALL)
        if m and not title:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    except Exception as e:
        print(f"[insecam] Page scrape failed for {cam_id}: {e}", file=sys.stderr)

    return title, location, manufacturer, lat, lon

def _insecam_fallback(city: str, result: dict, country: str = "") -> dict:
    """Fallback camera search via Insecam.org when Shodan is unavailable."""
    print(f"[insecam] Searching for cameras in: {city}, {country}", file=sys.stderr)
    result["source"] = "insecam"
    result["error"] = None

    # Map common country names to ISO codes
    country_map = {
        "united states": "US", "united kingdom": "GB", "germany": "DE",
        "france": "FR", "canada": "CA", "australia": "AU", "japan": "JP",
        "netherlands": "NL", "italy": "IT", "spain": "ES", "brazil": "BR",
        "mexico": "MX", "india": "IN", "china": "CN", "russia": "RU",
        "south korea": "KR", "sweden": "SE", "norway": "NO", "denmark": "DK",
        "finland": "FI", "poland": "PL", "ireland": "IE", "belgium": "BE",
        "switzerland": "CH", "austria": "AT", "portugal": "PT", "czech republic": "CZ",
        "romania": "RO", "hungary": "HU", "ukraine": "UA", "turkey": "TR",
        "israel": "IL", "south africa": "ZA", "argentina": "AR", "chile": "CL",
        "colombia": "CO", "thailand": "TH", "indonesia": "ID", "philippines": "PH",
        "vietnam": "VN", "malaysia": "MY", "singapore": "SG", "new zealand": "NZ",
    }
    country_code = country_map.get(country.lower().strip(), "US")

    try:
        # Search Insecam by city
        url = f"https://www.insecam.org/en/bycountry/{country_code}/" if country_code else "https://www.insecam.org/en/bycountry/US/"
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if resp.status_code == 404:
            # Try country-level fallback
            url = "https://www.insecam.org/en/"
            resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()

        import re
        html = resp.text

        # Parse camera blocks — each block has cam_id, img_url, location title, manufacturer
        # Pattern: <a href="/en/view/ID/" title="Live camera in LOCATION"><img src="URL" title="MANUFACTURER"
        block_pattern = r'href="/en/view/(\d+)/"\s+title="([^"]+)"[^>]*>[^<]*<[^>]*>[^<]*<img[^>]+src="(http://[\d\.]+:\d+[^"]*)"\s+title="([^"]*)"'''
        blocks = re.findall(block_pattern, html, re.DOTALL)

        seen_ips = set()
        for cam_id, location_title, img_url, manufacturer in blocks:
            m = re.match(r'http://([\d\.]+):(\d+)', img_url)
            if not m:
                continue
            ip, port = m.group(1), int(m.group(2))
            if ip in seen_ips:
                continue
            seen_ips.add(ip)

            # Clean location title — strip "Live camera in " and country prefix
            location_name = re.sub(r'^live camera\s*(in\s*)?',"", location_title, flags=re.IGNORECASE).strip()
            # Strip leading "Country, " prefix to get just city/area
            location_parts = location_name.split(", ", 1)
            cam_city = location_parts[-1] if len(location_parts) > 1 else location_name
            manufacturer = manufacturer.strip()

            result["results"].append({
                "id": f"CAM-{len(result['results'])+1:03d}",
                "ip": ip,
                "port": port,
                "product": manufacturer or "IP Camera",
                "title": location_name,
                "location_name": location_name,
                "org": "",
                "country": country,
                "city": cam_city,
                "lat": result.get("lat"),
                "lon": result.get("lon"),
                "links": [
                    f"https://www.insecam.org/en/view/{cam_id}/",
                    f"http://{ip}:{port}/",
                ],
                "screenshot_url": img_url,
                "shodan_url": f"https://www.shodan.io/host/{ip}",
                "auth_required": False,
                "last_seen": "",
                "tags": ["insecam"],
                "source": "insecam",
                "insecam_id": cam_id,
            })

        # Deduplicate
        seen_ids = set()
        deduped = []
        for r in result["results"]:
            key = r["ip"]
            if key not in seen_ids:
                seen_ids.add(key)
                deduped.append(r)
        result["results"] = deduped

        print(f"[insecam] Found {len(result['results'])} cameras", file=sys.stderr)

    except requests.RequestException as e:
        result["error"] = f"Insecam fallback failed: {e}"

    return result

def search_cameras(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "cameras", "source": "shodan", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no Shodan query sent"}]
        return result

    if not SHODAN_API_KEY:
        result["error"] = "SHODAN_API_KEY not set"
        return result

    # Shodan geo filter uses km
    radius_km = max(1, radius_m // 1000)

    # Reverse geocode to city for free-tier compatible queries
    city = "Unknown"
    country = "Unknown"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers=HEADERS, timeout=10
        )
        addr = r.json().get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("village") or "Unknown"
        country = addr.get("country") or "Unknown"
        print(f"[shodan] City: {city}, Country: {country}", file=sys.stderr)
    except Exception as e:
        print(f"[shodan] Reverse geocode failed: {e}", file=sys.stderr)

    # Camera-focused queries — city-based (free tier compatible)
    queries = [
        f'city:"{city}" has_screenshot:true webcam',
        f'city:"{city}" product:webcam',
        f'city:"{city}" title:camera',
        f'city:"{city}" port:554',
        f'city:"{city}" title:"network camera"',
        f'city:"{city}" title:"IP Camera"',
    ]

    seen_ips = set()
    for query in queries:
        try:
            resp = requests.get(
                f"{SHODAN_URL}/shodan/host/search",
                params={"key": SHODAN_API_KEY, "query": query, "minify": False},
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code == 401:
                result["error"] = "Shodan API key invalid or expired"
                print("[shodan] Falling back to Insecam...", file=sys.stderr)
                return _insecam_fallback(city, result, country)
            if resp.status_code == 402:
                result["error"] = "Shodan query requires paid plan"
                print("[shodan] Falling back to Insecam...", file=sys.stderr)
                return _insecam_fallback(city, result, country)
            if resp.status_code == 403:
                result["error"] = "Shodan plan does not support search API"
                print("[shodan] Falling back to Insecam...", file=sys.stderr)
                return _insecam_fallback(city, result, country)
            resp.raise_for_status()
            data = resp.json()

            for host in data.get("matches", []):
                ip = host.get("ip_str", "")
                if ip in seen_ips:
                    continue
                seen_ips.add(ip)

                ports = host.get("ports", [host.get("port", 0)])
                port = ports[0] if ports else 80
                product = host.get("product", "")
                org = host.get("org", "")
                transport = host.get("transport", "tcp")

                # Build direct view links
                links = []
                if port in (80, 8080, 443, 8443):
                    scheme = "https" if port in (443, 8443) else "http"
                    links.append(f"{scheme}://{ip}:{port}/")
                if port == 554 or "rtsp" in str(host.get("data", "")).lower():
                    links.append(f"rtsp://{ip}:{port}/")

                # Screenshot URL if available
                screenshot_url = None
                if host.get("opts", {}).get("screenshot"):
                    screenshot_url = f"https://www.shodan.io/host/{ip}#screenshots"

                result["results"].append({
                    "id": f"CAM-{len(result['results'])+1:03d}",
                    "ip": ip,
                    "port": port,
                    "product": product,
                    "org": org,
                    "country": host.get("location", {}).get("country_name", ""),
                    "city": host.get("location", {}).get("city", ""),
                    "lat": host.get("location", {}).get("latitude"),
                    "lon": host.get("location", {}).get("longitude"),
                    "links": links,
                    "screenshot_url": screenshot_url,
                    "shodan_url": f"https://www.shodan.io/host/{ip}",
                    "auth_required": _detect_auth(host),
                    "last_seen": host.get("last_update", ""),
                    "tags": host.get("tags", []),
                })

            time.sleep(1)  # Shodan rate limit
        except requests.RequestException as e:
            result["error"] = str(e)
            break

    return result


def _detect_auth(host: dict) -> bool:
    """Heuristic: detect if camera likely requires authentication."""
    data = str(host.get("data", "")).lower()
    auth_strings = ["401 unauthorized", "www-authenticate", "login", "password", "auth"]
    return any(s in data for s in auth_strings)

# ── Wigle — WiFi ──────────────────────────────────────────────────────────────


def search_lpr(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    """Search OpenStreetMap via Overpass API for surveillance cameras and LPRs."""
    result = {"type": "lpr", "source": "openstreetmap", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no Overpass query sent"}]
        return result

    # Convert radius to bounding box
    deg = radius_m / 111000
    bbox = f"{lat-deg},{lon-deg},{lat+deg},{lon+deg}"

    query = f"""
[out:json][timeout:30];
(
  node["man_made"="surveillance"]({bbox});
  node["camera:type"="ALPR"]({bbox});
  node["surveillance:type"="ALPR"]({bbox});
);
out body;
"""

    overpass_endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]
    resp = None
    last_error = None
    for endpoint in overpass_endpoints:
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                headers=HEADERS,
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
            elements = data.get("elements", [])

            for node in elements:
                tags = node.get("tags", {})
                nlat = node.get("lat")
                nlon = node.get("lon")

                # Classify device type
                surv_type = tags.get("surveillance:type", "").upper()
                cam_type   = tags.get("camera:type", "")
                is_alpr    = surv_type == "ALPR" or cam_type == "ALPR"
                is_flock   = (
                    "flock" in tags.get("manufacturer", "").lower() or
                    "flock" in tags.get("operator", "").lower() or
                    "flock" in tags.get("note", "").lower()
                )

                device_type = "ALPR/LPR" if is_alpr else "Surveillance Camera"
                if is_flock:
                    device_type = "Flock LPR"

                operator  = tags.get("operator", tags.get("operator:short", "Unknown"))
                manufacturer = tags.get("manufacturer", "")
                ref       = tags.get("ref", "")
                mount     = tags.get("camera:mount", "")
                direction = tags.get("direction", "")
                zone      = tags.get("surveillance:zone", "")

                # Build label
                label_parts = [p for p in [operator, manufacturer, ref] if p]
                label = " | ".join(label_parts) if label_parts else device_type

                osm_url = f"https://www.openstreetmap.org/node/{node['id']}"
                deflock_url = f"https://deflock.org/map#map=17/{nlat}/{nlon}" if is_alpr else ""

                # Extract direct camera feed URLs from OSM tags
                feed_url = (
                    tags.get("contact:webcam") or
                    tags.get("webcam") or
                    tags.get("url") or
                    tags.get("website") or
                    tags.get("contact:website") or
                    ""
                )
                name = tags.get("name", "")
                description = tags.get("description", "")

                result["results"].append({
                    "id": f"LPR-{len(result['results'])+1:03d}",
                    "osm_id": node["id"],
                    "device_type": device_type,
                    "is_alpr": is_alpr,
                    "is_flock": is_flock,
                    "label": label,
                    "name": name,
                    "description": description,
                    "operator": operator,
                    "manufacturer": manufacturer,
                    "ref": ref,
                    "mount": mount,
                    "direction": direction,
                    "zone": zone,
                    "surv_type": surv_type or tags.get("surveillance:type", ""),
                    "cam_type": cam_type,
                    "lat": nlat,
                    "lon": nlon,
                    "feed_url": feed_url,
                    "osm_url": osm_url,
                    "deflock_url": deflock_url,
                    "tags": tags,
                })

            # Sort: Flock first, then ALPRs, then general cameras
            result["results"].sort(key=lambda r: (
                0 if r["is_flock"] else (1 if r["is_alpr"] else 2),
                r.get("label", "")
            ))

            print(f"[overpass] Found {len(result['results'])} surveillance nodes", file=sys.stderr)
            result["error"] = None
            return result
        except requests.RequestException as e:
            last_error = e
            result["error"] = f"Overpass API error at {endpoint}: {e}"
            continue

    if last_error is not None:
        result["error"] = f"All Overpass endpoints failed; last error: {last_error}"

    return result

# ── OpenSky Network — Flights ─────────────────────────────────────────────────


def search_flights(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "flights", "source": "opensky", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no OpenSky query sent"}]
        return result

    deg = radius_m / 111000
    lamin, lamax = lat - deg, lat + deg
    lomin, lomax = lon - deg, lon + deg

    def _parse_states(states, source):
        for s in states:
            icao24      = (s[0] or "").strip()
            callsign    = (s[1] or "").strip()
            origin      = s[2] or ""
            s_lon       = s[5]
            s_lat       = s[6]
            altitude_m  = s[7]
            on_ground   = bool(s[8])
            velocity_ms = s[9]
            heading     = s[10]
            last_contact= s[4]
            if s_lat is None or s_lon is None:
                continue
            result["results"].append({
                "id":             f"FLT-{len(result['results'])+1:03d}",
                "icao24":         icao24,
                "callsign":       callsign or icao24.upper(),
                "origin_country": origin,
                "lat":            s_lat,
                "lon":            s_lon,
                "altitude_m":     round(altitude_m, 1) if altitude_m is not None else None,
                "velocity_ms":    round(velocity_ms, 1) if velocity_ms is not None else None,
                "heading":        round(heading, 1) if heading is not None else None,
                "on_ground":      on_ground,
                "last_contact":   last_contact,
                "source":         source,
                "flightaware_url":  f"https://flightaware.com/live/flight/{callsign}" if callsign else "",
                "flightradar_url":  f"https://www.flightradar24.com/{icao24}",
                "opensky_url":      f"https://opensky-network.org/aircraft-profile?icao24={icao24}",
            })

    # Primary: OpenSky Network
    try:
        resp = requests.get(
            f"{OPENSKY_URL}/states/all",
            params={"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        states = resp.json().get("states") or []
        _parse_states(states, "opensky")
        result["results"].sort(key=lambda r: (1 if r["on_ground"] else 0, -(r["altitude_m"] or 0)))
        result["source"] = "opensky"
        print(f"[opensky] Found {len(result['results'])} aircraft", file=sys.stderr)
        return result
    except Exception as e:
        print(f"[opensky] Failed ({e}), trying local readsb...", file=sys.stderr)
        result["error"] = str(e)

    # Fallback: local readsb JSON
    try:
        resp = requests.get(READSB_URL, headers=HEADERS, timeout=5)
        resp.raise_for_status()
        for ac in resp.json().get("aircraft", []):
            s_lat = ac.get("lat")
            s_lon = ac.get("lon")
            if s_lat is None or s_lon is None:
                continue
            icao24   = ac.get("hex", "")
            callsign = ac.get("flight", "").strip()
            alt      = ac.get("alt_baro")
            result["results"].append({
                "id":             f"FLT-{len(result['results'])+1:03d}",
                "icao24":         icao24,
                "callsign":       callsign or icao24.upper(),
                "origin_country": "",
                "lat":            s_lat,
                "lon":            s_lon,
                "altitude_m":     alt,
                "velocity_ms":    ac.get("gs"),
                "heading":        ac.get("track"),
                "on_ground":      alt == 0 if alt is not None else False,
                "last_contact":   ac.get("seen", ""),
                "source":         "readsb",
                "flightaware_url":  f"https://flightaware.com/live/flight/{callsign}" if callsign else "",
                "flightradar_url":  f"https://www.flightradar24.com/{icao24}",
                "opensky_url":      f"https://opensky-network.org/aircraft-profile?icao24={icao24}",
            })
        result["source"] = "readsb"
        result["error"] = None
        print(f"[readsb] Found {len(result['results'])} aircraft", file=sys.stderr)
    except Exception as e2:
        result["error"] = f"OpenSky: {result['error']}; readsb: {e2}"

    return result


def search_wifi(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "wifi", "source": "wigle", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no Wigle query sent"}]
        return result

    if not WIGLE_API_NAME or not WIGLE_API_TOKEN:
        result["error"] = "WIGLE_API_NAME or WIGLE_API_TOKEN not set"
        return result

    # Convert radius to lat/lon bounding box (rough)
    deg = radius_m / 111000
    params = {
        "latrange1": lat - deg,
        "latrange2": lat + deg,
        "longrange1": lon - deg,
        "longrange2": lon + deg,
        "resultsPerPage": 100,
        "onlymine": "false",
    }

    try:
        resp = requests.get(
            f"{WIGLE_URL}/network/search",
            params=params,
            auth=(WIGLE_API_NAME, WIGLE_API_TOKEN),
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 401:
            result["error"] = "Wigle credentials invalid"
            return result
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            result["error"] = data.get("message", "Wigle API returned success=false")
            return result

        encryption_rank = {"": 0, "None": 0, "WEP": 1, "WPA": 2, "WPA2": 3, "WPA3": 4}

        networks = data.get("results", [])
        # Sort: open first, then by last seen
        networks.sort(key=lambda n: (
            encryption_rank.get(n.get("encryption", "WPA2"), 3),
            n.get("lasttime", "")
        ))

        for net in networks:
            enc = net.get("encryption", "Unknown")
            result["results"].append({
                "id": f"WIFI-{len(result['results'])+1:03d}",
                "ssid": net.get("ssid", "<hidden>"),
                "bssid": net.get("netid", ""),
                "encryption": enc,
                "open": enc in ("", "None"),
                "weak": enc == "WEP",
                "channel": net.get("channel", ""),
                "signal": net.get("rssi", ""),
                "lat": net.get("trilat"),
                "lon": net.get("trilong"),
                "last_seen": net.get("lasttime", ""),
                "first_seen": net.get("firsttime", ""),
                "wigle_url": f"https://wigle.net/map?maplat={net.get('trilat')}&maplon={net.get('trilong')}&zoom=16",
            })

    except requests.RequestException as e:
        result["error"] = str(e)

    return result

# ── OpenCelliD — Cell Towers ──────────────────────────────────────────────────


# MCC/MNC carrier lookup table (US-focused + major international)
MNC_TABLE = {
    # United States
    "310010": "Verizon", "310012": "Verizon", "310013": "MobileTel",
    "310020": "Union Telephone", "310030": "AT&T",
    "310040": "Dobson Cellular", "310060": "Consolidated Telcom",
    "310070": "Highland Cellular", "310080": "T-Mobile",
    "310090": "Cricket", "310100": "Plateau Wireless",
    "310110": "PTI Pacifica", "310120": "Sprint",
    "310150": "AT&T", "310160": "T-Mobile",
    "310170": "T-Mobile", "310180": "West Central Wireless",
    "310190": "Dutch Harbor Cellular", "310200": "T-Mobile",
    "310210": "T-Mobile", "310220": "Disciotelecom",
    "310230": "T-Mobile", "310240": "T-Mobile",
    "310250": "T-Mobile", "310260": "T-Mobile",
    "310270": "T-Mobile", "310280": "AT&T",
    "310290": "T-Mobile", "310300": "T-Mobile",
    "310310": "T-Mobile", "310320": "Cellular One",
    "310330": "T-Mobile", "310340": "Westlink Comm",
    "310350": "T-Mobile", "310360": "Cellular One",
    "310370": "T-Mobile", "310380": "T-Mobile",
    "310390": "T-Mobile", "310400": "T-Mobile",
    "310410": "AT&T", "310420": "T-Mobile",
    "310430": "T-Mobile", "310440": "Cellular One",
    "310450": "Viaero Wireless", "310460": "T-Mobile",
    "310470": "T-Mobile", "310480": "T-Mobile",
    "310490": "T-Mobile", "310500": "Alltel",
    "310510": "Airtel", "310520": "T-Mobile",
    "310530": "West Virginia Wireless", "310540": "T-Mobile",
    "310550": "T-Mobile", "310560": "T-Mobile",
    "310570": "MTPCS Cellular One", "310580": "T-Mobile",
    "310590": "T-Mobile", "310600": "T-Mobile",
    "310610": "Elkhart TelCo", "310620": "T-Mobile",
    "310630": "T-Mobile", "310640": "T-Mobile",
    "310650": "Jasper Wireless", "310660": "T-Mobile",
    "310670": "T-Mobile", "310680": "AT&T",
    "310690": "T-Mobile", "310700": "T-Mobile",
    "310710": "T-Mobile", "310720": "T-Mobile",
    "310730": "T-Mobile", "310740": "T-Mobile",
    "310750": "T-Mobile", "310760": "T-Mobile",
    "310770": "T-Mobile", "310780": "T-Mobile",
    "310790": "T-Mobile", "310800": "T-Mobile",
    "310810": "T-Mobile", "310820": "T-Mobile",
    "310830": "Caprock Cellular", "310840": "T-Mobile",
    "310850": "Aeris Comm", "310860": "T-Mobile",
    "310870": "T-Mobile", "310880": "T-Mobile",
    "310890": "T-Mobile", "310900": "T-Mobile",
    "310910": "T-Mobile", "310920": "T-Mobile",
    "310930": "T-Mobile", "310940": "T-Mobile",
    "310950": "T-Mobile", "310960": "T-Mobile",
    "310970": "T-Mobile", "310980": "T-Mobile",
    "310990": "T-Mobile",
    "311010": "Chariton Valley", "311020": "Viaero Wireless",
    "311030": "Indigo Wireless", "311040": "Commnet Wireless",
    "311050": "Thumb Cellular", "311060": "Farmers Cellular",
    "311070": "Easterbrooke Cellular", "311080": "Pine Cellular",
    "311090": "Long Lines Wireless", "311100": "High Plains Wireless",
    "311110": "Northstar", "311120": "Choice Phone",
    "311130": "Cell One Amarillo", "311140": "MBO Wireless",
    "311150": "Wilkes Cellular", "311160": "Endless Mountains Wireless",
    "311170": "Peoples Wireless", "311180": "Rural Cellular Corp",
    "311190": "Cellular One", "311200": "Trans-Pacific Wireless",
    "311210": "Farmers Cellular", "311220": "Advantage Cellular",
    "311230": "C Spire Wireless", "311240": "CDMA South",
    "311250": "Cellcom", "311260": "Cellular One",
    "311270": "Surewest Wireless", "311280": "Verizon",
    "311290": "NTELOS", "311300": "Surewest Wireless",
    "311310": "Indigo Wireless", "311320": "Cellular One",
    "311330": "Bug Tussel Wireless", "311340": "Commnet Wireless",
    "311350": "Sagebrush Cellular", "311360": "Surewest Wireless",
    "311370": "GCI", "311380": "New Cell",
    "311390": "Cellular One", "311400": "Midcontinent Comm",
    "311410": "Cellcom", "311420": "Verizon",
    "311430": "Cellular One", "311440": "Numerex",
    "311450": "Verizon", "311460": "TeleCove",
    "311470": "Verizon", "311480": "Verizon",
    "311490": "Sprint", "311500": "Mosaic Telecom",
    "311510": "Verizon", "311520": "Southern Communications",
    "311530": "NewCore Wireless", "311540": "SIMMETRY",
    "311550": "Verizon", "311560": "OTZ Telephone",
    "311570": "Verizon", "311580": "UTSTARCOM",
    "311590": "Verizon", "311600": "New Cell",
    "311610": "Verizon", "311620": "Verizon",
    "311630": "Verizon", "311640": "Verizon",
    "311650": "Verizon", "311660": "Verizon",
    "311670": "Verizon", "311680": "Verizon",
    "311690": "Verizon", "311700": "Verizon",
    "311710": "Verizon", "311720": "Verizon",
    "311730": "Verizon", "311740": "Verizon",
    "311750": "Verizon", "311760": "Verizon",
    "311770": "Verizon", "311780": "Verizon",
    "311790": "Verizon", "311800": "Verizon",
    "311810": "Verizon", "311820": "Verizon",
    "311830": "Verizon", "311840": "Verizon",
    "311850": "Verizon", "311860": "Verizon",
    "311870": "Verizon", "311870": "US Cellular",
    "311880": "Verizon", "311890": "Verizon",
    "311900": "Verizon", "311910": "Verizon",
    "311920": "Verizon", "311930": "Verizon",
    "311940": "Verizon", "311950": "Verizon",
    "311960": "Verizon", "311970": "Verizon",
    "311980": "Verizon", "311990": "Verizon",
    "312010": "Verizon", "312020": "Verizon",
    "312030": "Verizon", "312040": "Verizon",
    "312050": "Verizon", "312060": "Verizon",
    "312070": "Verizon", "312080": "Verizon",
    "312090": "Verizon", "312100": "Verizon",
    "312110": "Verizon", "312120": "Verizon",
    "312130": "Verizon", "312140": "Verizon",
    "312150": "Verizon", "312160": "Verizon",
    "312170": "Verizon", "312180": "Verizon",
    "312190": "Verizon", "312200": "Verizon",
    "312210": "Verizon", "312220": "Verizon",
    "312230": "Verizon", "312240": "Verizon",
    "312250": "Verizon", "312260": "Verizon",
    "312270": "Verizon", "312280": "Verizon",
    "312290": "Verizon", "312300": "Verizon",
    "312310": "Verizon", "312320": "Verizon",
    "312330": "Verizon", "312340": "Verizon",
    "312350": "Verizon", "312360": "Verizon",
    "312370": "Verizon", "312380": "Verizon",
    "312390": "Verizon", "312400": "Verizon",
    "312410": "Verizon", "312420": "Verizon",
    "312430": "Verizon", "312440": "Verizon",
    "312450": "Verizon", "312460": "Verizon",
    "312470": "Verizon", "312480": "Verizon",
    "312490": "Verizon", "312500": "Verizon",
    "312510": "Verizon", "312520": "Verizon",
    "312530": "Dish Wireless",
    # UK
    "23410": "O2 UK", "23415": "Vodafone UK", "23420": "Three UK",
    "23430": "EE UK", "23450": "EE UK",
    # Canada
    "302220": "Telus", "302370": "Fido", "302610": "Bell",
    "302720": "Rogers",
    # Australia
    "50501": "Telstra", "50502": "Optus", "50503": "Vodafone AU",
    # Germany
    "26201": "Telekom DE", "26202": "Vodafone DE", "26203": "O2 DE",
    # France
    "20801": "Orange FR", "20810": "SFR", "20820": "Bouygues",
    # Ireland
    "27201": "Vodafone IE", "27202": "Three IE", "27203": "Eir",
}

def lookup_carrier(mcc: str, mnc: str) -> str:
    """Return carrier name for MCC/MNC pair, or empty string if unknown."""
    key = f"{mcc}{mnc.zfill(3)}"
    return MNC_TABLE.get(key, "")

# ── AISHub / VesselFinder — Maritime ─────────────────────────────────────────


def search_ships(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "ships", "source": "aisstream", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no AIS query sent"}]
        return result

    # Primary: AISstream.io WebSocket (free, requires API key)
    if AISSTREAM_KEY:
        result = _aisstream_search(lat, lon, radius_m, result)
        if result["results"] or not result["error"]:
            return result
        print(f"[aisstream] Failed ({result['error']}), trying AISHub...", file=sys.stderr)

    # Fallback: AISHub REST API (requires contributor username)
    if AISHUB_USER:
        result["source"] = "aishub"
        result = _aishub_search(lat, lon, radius_m, result)
        return result

    result["error"] = "No ship tracking credentials. Get a free key at aisstream.io"
    return result


def _aisstream_search(lat: float, lon: float, radius_m: int, result: dict) -> dict:
    """Query aisstream.io WebSocket for vessels in bounding box (~15s collect window)."""
    import asyncio
    import json as _json
    import time

    try:
        import websockets
    except ImportError:
        result["error"] = "websockets package not installed — run: pip3 install websockets"
        return result

    deg = radius_m / 111000
    bounding_box = [[lat - deg, lon - deg], [lat + deg, lon + deg]]
    subscribe_msg = _json.dumps({
        "APIKey": AISSTREAM_KEY,
        "BoundingBoxes": [bounding_box],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    vessels: dict = {}

    async def _collect():
        try:
            async with websockets.connect(AISSTREAM_URL, open_timeout=10, close_timeout=5) as ws:
                await ws.send(subscribe_msg)
                deadline = time.time() + 15
                while time.time() < deadline and len(vessels) < 200:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        msg = _json.loads(raw)
                        # Check for auth error
                        if msg.get("Error"):
                            result["error"] = f"AISstream: {msg['Error']}"
                            return
                        msg_type = msg.get("MessageType", "")
                        meta     = msg.get("MetaData", {})
                        mmsi     = str(meta.get("MMSI", ""))
                        if not mmsi:
                            continue
                        if mmsi not in vessels:
                            vessels[mmsi] = {
                                "mmsi":        mmsi,
                                "vessel_name": meta.get("ShipName", "").strip(),
                                "lat":         meta.get("latitude"),
                                "lon":         meta.get("longitude"),
                                "speed":       None,
                                "course":      None,
                                "heading":     None,
                                "ship_type":   "",
                                "destination": "",
                                "flag":        meta.get("MMSI_CountryCode", ""),
                                "status":      "",
                                "last_seen":   meta.get("time_utc", ""),
                            }
                        if msg_type == "PositionReport":
                            pr = msg.get("Message", {}).get("PositionReport", {})
                            vessels[mmsi].update({
                                "speed":   pr.get("Sog"),
                                "course":  pr.get("Cog"),
                                "heading": pr.get("TrueHeading"),
                                "status":  pr.get("NavigationalStatus", ""),
                                "lat":     pr.get("Latitude") or vessels[mmsi]["lat"],
                                "lon":     pr.get("Longitude") or vessels[mmsi]["lon"],
                            })
                        elif msg_type == "ShipStaticData":
                            sd = msg.get("Message", {}).get("ShipStaticData", {})
                            vessels[mmsi].update({
                                "vessel_name": (sd.get("Name") or vessels[mmsi]["vessel_name"]).strip(),
                                "ship_type":   str(sd.get("Type", "")),
                                "destination": (sd.get("Destination") or "").strip(),
                                "flag":        meta.get("MMSI_CountryCode", "") or vessels[mmsi]["flag"],
                            })
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        continue
        except Exception as e:
            result["error"] = f"AISstream connection failed: {e}"

    try:
        asyncio.run(_collect())
    except Exception as e:
        result["error"] = f"AISstream error: {e}"
        return result

    if result.get("error") and not vessels:
        return result

    for i, v in enumerate(vessels.values()):
        mmsi = v["mmsi"]
        result["results"].append({
            "id":                f"SHIP-{i+1:03d}",
            "mmsi":              mmsi,
            "vessel_name":       v["vessel_name"] or f"MMSI:{mmsi}",
            "ship_type":         v["ship_type"] or "Unknown",
            "lat":               v["lat"],
            "lon":               v["lon"],
            "speed":             v["speed"],
            "course":            v["course"],
            "heading":           v["heading"],
            "destination":       v["destination"],
            "flag":              v["flag"],
            "status":            v["status"],
            "last_seen":         v["last_seen"],
            "source":            "aisstream",
            "marinetraffic_url": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
            "vesselfinder_url":  f"https://www.vesselfinder.com/?mmsi={mmsi}",
        })

    result["error"] = None
    print(f"[aisstream] Found {len(result['results'])} vessels", file=sys.stderr)
    return result


def _aishub_search(lat: float, lon: float, radius_m: int, result: dict) -> dict:
    """AISHub contributor REST API fallback."""
    deg = radius_m / 111000
    try:
        resp = requests.get(
            "https://data.aishub.net/vessels-v2.php",
            params={
                "USERNAME": AISHUB_USER,
                "format":   1,
                "output":   "json",
                "compress": 0,
                "latmin":   lat - deg, "latmax": lat + deg,
                "lonmin":   lon - deg, "lonmax": lon + deg,
            },
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        vessels = data[1] if isinstance(data, list) and len(data) > 1 else []
        for i, v in enumerate(vessels):
            mmsi = str(v.get("MMSI", ""))
            result["results"].append({
                "id":                f"SHIP-{i+1:03d}",
                "mmsi":              mmsi,
                "vessel_name":       (v.get("NAME") or f"MMSI:{mmsi}").strip(),
                "ship_type":         str(v.get("TYPE", "Unknown")),
                "lat":               v.get("LATITUDE"),
                "lon":               v.get("LONGITUDE"),
                "speed":             v.get("SOG"),
                "course":            v.get("COG"),
                "heading":           v.get("HEADING"),
                "destination":       (v.get("DEST") or "").strip(),
                "flag":              v.get("COUNTRY", ""),
                "status":            str(v.get("NAVSTAT", "")),
                "last_seen":         v.get("TIME", ""),
                "source":            "aishub",
                "marinetraffic_url": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
                "vesselfinder_url":  f"https://www.vesselfinder.com/?mmsi={mmsi}",
            })
        result["error"] = None
        print(f"[aishub] Found {len(result['results'])} vessels", file=sys.stderr)
    except Exception as e:
        result["error"] = f"AISHub error: {e}"
    return result


def search_cells(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "cells", "source": "opencellid", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no OpenCelliD query sent"}]
        return result

    if not OPENCELLID_KEY:
        result["error"] = "OPENCELLID_KEY not set"
        return result

    deg = radius_m / 111000
    params = {
        "key": OPENCELLID_KEY,
        "BBOX": f"{lat-deg},{lon-deg},{lat+deg},{lon+deg}",
        "format": "json",
        "limit": 200,
    }

    try:
        resp = requests.get(OPENCELLID_URL, params=params, headers=HEADERS, timeout=15)
        if resp.status_code == 403:
            result["error"] = "OpenCelliD API key invalid"
            return result
        resp.raise_for_status()
        data = resp.json()

        radio_order = {"NR": 0, "LTE": 1, "UMTS": 2, "GSM": 3}
        towers = data.get("cells", [])
        towers.sort(key=lambda t: radio_order.get(t.get("radio", "GSM"), 9))

        for tower in towers:
            mcc = str(tower.get("mcc", ""))
            mnc = str(tower.get("mnc", ""))
            carrier = lookup_carrier(mcc, mnc)
            result["results"].append({
                "id": f"CELL-{len(result['results'])+1:03d}",
                "radio": tower.get("radio", ""),
                "mcc": mcc,
                "mnc": mnc,
                "carrier": carrier,
                "lac": tower.get("lac", tower.get("area", "")),
                "cell_id": tower.get("cell", tower.get("cellid", "")),
                "lat": tower.get("lat"),
                "lon": tower.get("lon"),
                "range_m": tower.get("range", ""),
                "samples": tower.get("samples", ""),
                "created": tower.get("created", ""),
                "updated": tower.get("updated", ""),
                "opencellid_url": f"https://opencellid.org/#zoom=16&lat={tower.get('lat')}&lon={tower.get('lon')}",
            })

    except requests.RequestException as e:
        result["error"] = str(e)

    return result

# ── CelesTrak / N2YO — Satellites ────────────────────────────────────────────


def search_satellites(lat: float, lon: float, radius_m: int, dry_run: bool) -> dict:
    result = {"type": "satellites", "source": "tle-api", "lat": lat, "lon": lon,
              "radius_m": radius_m, "results": [], "error": None}

    if dry_run:
        result["results"] = [{"_dry_run": True, "message": "Dry run — no TLE query sent"}]
        return result

    # Number of TLE pages to fetch — more pages = more satellites checked.
    # Page size 100, sorted by popularity. Default 3 pages = 300 satellites.
    # Radius hint: larger area → fetch more pages.
    pages_to_fetch = min(10, max(3, radius_m // 200000))

    if SKYFIELD_AVAILABLE:
        try:
            ts = load.timescale()
            now = ts.now()
            observer = wgs84.latlon(lat, lon)

            # Fetch TLEs from tle.ivanstanojevic.me (JSON API, sorted by popularity)
            # Falls back to CelesTrak if the primary source fails.
            members = []
            for page in range(1, pages_to_fetch + 1):
                try:
                    resp = requests.get(
                        TLE_API_URL,
                        params={"page": page, "page-size": 100},
                        headers=HEADERS,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    batch = data.get("member", [])
                    members.extend(batch)
                    if len(batch) < 100:
                        break  # last page
                except Exception as e:
                    print(f"[tle-api] page {page} failed: {e}", file=sys.stderr)
                    break

            if not members:
                raise ValueError("No TLE data received from tle.ivanstanojevic.me")

            print(f"[tle-api] Fetched {len(members)} TLEs ({pages_to_fetch} pages)", file=sys.stderr)

            parsed = 0
            for m in members:
                name  = m.get("name", "")
                line1 = m.get("line1", "")
                line2 = m.get("line2", "")
                if not line1 or not line2:
                    continue

                try:
                    sat = EarthSatellite(line1, line2, name, ts)
                    norad_id = str(m.get("satelliteId", line1[2:7].strip()))
                    parsed += 1

                    # Current position
                    geocentric = sat.at(now)
                    subpoint   = wgs84.subpoint(geocentric)
                    sat_lat    = subpoint.latitude.degrees
                    sat_lon    = subpoint.longitude.degrees
                    alt_km     = subpoint.elevation.km

                    # Current elevation from observer
                    diff  = sat - observer
                    topo  = diff.at(now)
                    alt_obj, az_obj, _ = topo.altaz()
                    current_elev = alt_obj.degrees

                    # Velocity magnitude
                    vel = geocentric.velocity.km_per_s
                    speed_kms = round(math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2), 2)

                    # Next pass over observer (24h window, min 10° elevation)
                    next_pass_utc = None
                    max_elev_deg  = None
                    az_at_max     = None
                    try:
                        t1 = ts.tt_jd(now.tt + 1.0)
                        times, events = sat.find_events(observer, now, t1, altitude_degrees=10.0)
                        for t_ev, ev in zip(times, events):
                            if ev == 0 and next_pass_utc is None:
                                next_pass_utc = t_ev.utc_iso()
                            if ev == 1:  # culmination
                                d2 = sat - observer
                                a2, az2, _ = d2.at(t_ev).altaz()
                                max_elev_deg = round(a2.degrees, 1)
                                az_at_max    = round(az2.degrees, 1)
                                break
                    except Exception:
                        pass

                    # Include if currently visible OR has a pass within 24h
                    if current_elev < 0 and next_pass_utc is None:
                        continue

                    # Ground-track distance (for sorting)
                    dlat = (sat_lat - lat) * 111.0
                    dlon = (sat_lon - lon) * 111.0 * math.cos(math.radians(lat))
                    dist_km = round(math.sqrt(dlat**2 + dlon**2), 1)

                    result["results"].append({
                        "id":            f"SAT-{len(result['results'])+1:03d}",
                        "name":          name,
                        "norad_id":      norad_id,
                        "lat":           float(round(sat_lat, 4)),
                        "lon":           float(round(sat_lon, 4)),
                        "altitude_km":   float(round(alt_km, 1)),
                        "velocity_kms":  float(speed_kms),
                        "current_elev":  float(round(current_elev, 1)),
                        "currently_visible": bool(current_elev >= 0),
                        "next_pass_utc": next_pass_utc,
                        "elevation_deg": float(max_elev_deg) if max_elev_deg is not None else None,
                        "azimuth_deg":   float(az_at_max) if az_at_max is not None else None,
                        "dist_km":       float(dist_km),
                        "n2yo_url":          f"https://www.n2yo.com/satellite/?s={norad_id}",
                        "heavens_above_url": f"https://heavens-above.com/PassSummary.aspx?satid={norad_id}&lat={lat}&lng={lon}&alt=0&tz=UTC",
                    })

                except Exception:
                    continue

            # Sort: currently visible first, then by next pass time
            result["results"].sort(key=lambda r: (
                0 if r.get("currently_visible") else 1,
                r.get("next_pass_utc") or "z"
            ))
            result["source"] = "tle-api"
            print(f"[tle-api] Parsed {parsed}, {len(result['results'])} visible/passing over location", file=sys.stderr)

        except Exception as e:
            result["error"] = str(e)
            print(f"[tle-api/skyfield] Error: {e}", file=sys.stderr)

    # N2YO fallback (when skyfield unavailable or CelesTrak failed)
    if not result["results"] and N2YO_API_KEY:
        try:
            resp = requests.get(
                f"{N2YO_URL}/above/{lat}/{lon}/0/{max(1, int(radius_km))}/0/",
                params={"apiKey": N2YO_API_KEY},
                headers=HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for sat in data.get("above", []):
                norad_id = str(sat.get("satid", ""))
                result["results"].append({
                    "id":            f"SAT-{len(result['results'])+1:03d}",
                    "name":          sat.get("satname", ""),
                    "norad_id":      norad_id,
                    "lat":           sat.get("satlat"),
                    "lon":           sat.get("satlng"),
                    "altitude_km":   sat.get("satalt"),
                    "velocity_kms":  None,
                    "next_pass_utc": None,
                    "elevation_deg": sat.get("elevation"),
                    "azimuth_deg":   sat.get("azimuth"),
                    "dist_km":       None,
                    "n2yo_url":           f"https://www.n2yo.com/satellite/?s={norad_id}",
                    "heavens_above_url":  f"https://heavens-above.com/PassSummary.aspx?satid={norad_id}&lat={lat}&lng={lon}&alt=0&tz=UTC",
                })
            result["source"] = "n2yo"
            result["error"] = None
            print(f"[n2yo] Found {len(result['results'])} satellites", file=sys.stderr)
        except Exception as e:
            result["error"] = (result.get("error") or "") + f" N2YO: {e}"

    elif not SKYFIELD_AVAILABLE and not N2YO_API_KEY:
        result["error"] = "skyfield not available and N2YO_API_KEY not set"

    return result


# ── Output Formatters ─────────────────────────────────────────────────────────

def format_text(results: dict) -> str:
    lines = []
    ts = results["meta"]["timestamp"]
    loc = results["meta"]["resolved_location"]
    lines.append(f"SIGNALS REPORT — {ts}")
    lines.append(f"Location : {results['meta']['query_location']}")
    lines.append(f"Resolved : {loc}")
    lines.append(f"Radius   : {results['meta']['radius_m']}m")
    lines.append("")

    for section in results["data"]:
        t = section["type"].upper()
        count = len(section["results"])
        err = section.get("error")
        lines.append(f"── {t} ({count} results) {'[ERROR: '+err+']' if err else ''}")

        if section["type"] == "cameras":
            open_cams = [r for r in section["results"] if not r.get("auth_required") and not r.get("_dry_run")]
            if open_cams:
                lines.append(f"   ⚠ OPEN/UNAUTHENTICATED: {len(open_cams)}")
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                auth = "OPEN" if not r.get("auth_required") else "AUTH"
                title = r.get("title") or r.get("location_name") or ""
                product = r.get("product", "Unknown")
                label = f"{title}" if title else product
                lines.append(f"   [{r['id']}] {label} | {r['ip']}:{r['port']} | {auth}")
                if r.get("location_name") and r.get("location_name") != title:
                    lines.append(f"           Location: {r['location_name']}")
                if r.get("city"):
                    lines.append(f"           City: {r['city']}")
                for link in r.get("links", []):
                    lines.append(f"           LINK: {link}")
                if r.get("shodan_url") and "insecam" not in r.get("ip",""):
                    lines.append(f"           Shodan: {r['shodan_url']}")

        elif section["type"] == "wifi":
            open_nets = [r for r in section["results"] if r.get("open") and not r.get("_dry_run")]
            if open_nets:
                lines.append(f"   ⚠ OPEN NETWORKS: {len(open_nets)}")
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                flag = "OPEN" if r.get("open") else ("WEP-WEAK" if r.get("weak") else r.get("encryption",""))
                lines.append(f"   [{r['id']}] {r.get('ssid','<hidden>')} | {r.get('bssid','')} | {flag} | Last: {r.get('last_seen','')[:10]}")
                if r.get("wigle_url"):
                    lines.append(f"           Map: {r['wigle_url']}")

        elif section["type"] == "cells":
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                carrier = r.get("carrier", "")
                carrier_str = f" ({carrier})" if carrier else ""
                lines.append(f"   [{r['id']}] {r.get('radio','')} | MCC:{r.get('mcc','')} MNC:{r.get('mnc','')}{carrier_str} | LAC:{r.get('lac','')} CID:{r.get('cell_id','')} | Range:{r.get('range_m','')}m")
                if r.get("opencellid_url"):
                    lines.append(f"           Map: {r['opencellid_url']}")

        elif section["type"] == "lpr":
            alpr_count = len([r for r in section["results"] if r.get("is_alpr") and not r.get("_dry_run")])
            flock_count = len([r for r in section["results"] if r.get("is_flock") and not r.get("_dry_run")])
            if flock_count:
                lines.append(f"   Flock LPRs: {flock_count}")
            if alpr_count:
                lines.append(f"   Total ALPRs/LPRs: {alpr_count}")
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                dtype = r.get("device_type", "Camera")
                label = r.get("label", "")
                name = r.get("name", "")
                direction = f" Dir:{r['direction']}" if r.get("direction") else ""
                display = name if name else label
                lines.append(f"   [{r['id']}] {dtype} | {display}{direction}")
                if r.get("description"):
                    lines.append(f"           {r['description']}")
                if r.get("feed_url"):
                    lines.append(f"           LIVE FEED: {r['feed_url']}")
                if r.get("osm_url"):
                    lines.append(f"           OSM: {r['osm_url']}")
                if r.get("deflock_url"):
                    lines.append(f"           DeFlock: {r['deflock_url']}")

        elif section["type"] == "flights":
            airborne = [r for r in section["results"] if not r.get("on_ground") and not r.get("_dry_run")]
            if airborne:
                lines.append(f"   Airborne: {len(airborne)}")
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                status  = "GROUND" if r.get("on_ground") else "AIR"
                alt     = f"{r['altitude_m']}m" if r.get("altitude_m") is not None else "?"
                spd     = f"{r['velocity_ms']}m/s" if r.get("velocity_ms") is not None else "?"
                hdg     = f"HDG:{r['heading']}°" if r.get("heading") is not None else ""
                country = f" [{r['origin_country']}]" if r.get("origin_country") else ""
                lines.append(f"   [{r['id']}] {r.get('callsign','')} | {status} | Alt:{alt} Spd:{spd} {hdg}{country}")
                if r.get("flightaware_url"):
                    lines.append(f"           FlightAware: {r['flightaware_url']}")
                if r.get("flightradar_url"):
                    lines.append(f"           FlightRadar: {r['flightradar_url']}")

        elif section["type"] == "ships":
            underway = [r for r in section["results"] if (r.get("speed") or 0) > 0.5 and not r.get("_dry_run")]
            if underway:
                lines.append(f"   Underway: {len(underway)}")
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                spd  = f"{r['speed']}kn" if r.get("speed") is not None else "?"
                dest = f" -> {r['destination']}" if r.get("destination") else ""
                flag = f" [{r['flag']}]" if r.get("flag") else ""
                lines.append(f"   [{r['id']}] {r.get('vessel_name','')} | {r.get('ship_type','')} | Spd:{spd}{dest}{flag}")
                if r.get("marinetraffic_url"):
                    lines.append(f"           MarineTraffic: {r['marinetraffic_url']}")
                if r.get("vesselfinder_url"):
                    lines.append(f"           VesselFinder:  {r['vesselfinder_url']}")

        elif section["type"] == "satellites":
            for r in section["results"]:
                if r.get("_dry_run"):
                    lines.append(f"   {r['message']}")
                    continue
                alt  = f"{r['altitude_km']}km" if r.get("altitude_km") is not None else "?"
                nxt  = f" Next:{r['next_pass_utc']}" if r.get("next_pass_utc") else ""
                elev = f" MaxEl:{r['elevation_deg']}°" if r.get("elevation_deg") is not None else ""
                dist = f" [{r['dist_km']}km away]" if r.get("dist_km") is not None else ""
                lines.append(f"   [{r['id']}] {r.get('name','')} | Alt:{alt} | NORAD:{r.get('norad_id','')}{dist}{nxt}{elev}")
                if r.get("n2yo_url"):
                    lines.append(f"           N2YO: {r['n2yo_url']}")
                if r.get("heavens_above_url"):
                    lines.append(f"           HeavensAbove: {r['heavens_above_url']}")

        lines.append("")

    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Samaritan Signals search engine")
    parser.add_argument("--location", required=True, help="Address, landmark, or lat,lon")
    parser.add_argument("--radius",   required=True, type=int, help="Search radius in metres")
    parser.add_argument("--types",    default="all", help="cameras, wifi, cells, or all (comma-separated)")
    parser.add_argument("--output",   default="json", choices=["json","text"], help="Output format")
    parser.add_argument("--dry-run",  action="store_true", help="Geocode only, no API calls")
    args = parser.parse_args()

    requested = set()
    if "all" in args.types:
        requested = {"cameras", "wifi", "cells", "lpr", "flights", "ships", "satellites"}
    else:
        for t in args.types.split(","):
            requested.add(t.strip())

    # Geocode
    try:
        lat, lon = geocode(args.location)
    except Exception as e:
        print(json.dumps({"error": f"Geocoding failed: {e}"}))
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Geocoded: {lat:.6f}, {lon:.6f}", file=sys.stderr)
        print(f"[dry-run] Would query: {requested} within {args.radius}m", file=sys.stderr)

    # Run queries in parallel
    futures = {}
    with ThreadPoolExecutor(max_workers=7) as pool:
        if "cameras" in requested:
            futures["cameras"] = pool.submit(search_cameras, lat, lon, args.radius, args.dry_run)
        if "wifi" in requested:
            futures["wifi"] = pool.submit(search_wifi, lat, lon, args.radius, args.dry_run)
        if "cells" in requested:
            futures["cells"] = pool.submit(search_cells, lat, lon, args.radius, args.dry_run)
        if "lpr" in requested:
            futures["lpr"] = pool.submit(search_lpr, lat, lon, args.radius, args.dry_run)
        if "flights" in requested:
            futures["flights"] = pool.submit(search_flights, lat, lon, args.radius, args.dry_run)
        if "ships" in requested:
            futures["ships"] = pool.submit(search_ships, lat, lon, args.radius, args.dry_run)
        if "satellites" in requested:
            futures["satellites"] = pool.submit(search_satellites, lat, lon, args.radius, args.dry_run)

    data = []
    for key, future in futures.items():
        try:
            data.append(future.result())
        except Exception as e:
            data.append({"type": key, "error": str(e), "results": []})

    output = {
        "meta": {
            "query_location": args.location,
            "resolved_location": f"{lat:.6f},{lon:.6f}",
            "radius_m": args.radius,
            "types": list(requested),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "dry_run": args.dry_run,
        },
        "data": data,
    }

    if args.output == "text":
        print(format_text(output))
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
