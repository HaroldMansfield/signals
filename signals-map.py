#!/usr/bin/env python3
"""
signals-map.py — Generate a self-contained Leaflet HTML map from signals-search.py output.

Usage:
  python3 signals-search.py --location "Fort Lauderdale" --radius 50000 --types all | \\
    python3 signals-map.py --output /tmp/signals-map.html

  python3 signals-map.py --input results.json --output map.html
  python3 signals-map.py --input results.json --output map.html --open
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone


# ── Marker config per type ─────────────────────────────────────────────────────

TYPE_CONFIG = {
    "cameras":    {"color": "#e74c3c", "label": "Cameras",    "icon": "📷"},
    "lpr":        {"color": "#e67e22", "label": "LPR/ALPR",   "icon": "🚗"},
    "wifi":       {"color": "#3498db", "label": "WiFi",        "icon": "📶"},
    "cells":      {"color": "#27ae60", "label": "Cell Towers", "icon": "📡"},
    "flights":    {"color": "#f1c40f", "label": "Flights",     "icon": "✈"},
    "ships":      {"color": "#1abc9c", "label": "Ships",       "icon": "🚢"},
    "satellites": {"color": "#9b59b6", "label": "Satellites",  "icon": "🛰"},
}


def _esc(s) -> str:
    """Escape a value for safe embedding in JavaScript strings."""
    return str(s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", "")


def _popup_rows(r: dict, rtype: str) -> str:
    """Build HTML table rows for a marker popup."""
    rows = []

    def row(label, val):
        if val is not None and val != "" and val != []:
            rows.append(f"<tr><td style='color:#aaa;padding:2px 6px 2px 0'>{label}</td>"
                        f"<td style='padding:2px 0'>{val}</td></tr>")

    def link(url, text):
        if url:
            return f"<a href='{url}' target='_blank' style='color:#5dade2'>{text}</a>"
        return ""

    if rtype == "cameras":
        row("ID", r.get("id"))
        row("IP:Port", f"{r.get('ip')}:{r.get('port')}")
        row("Product", r.get("product"))
        row("Org", r.get("org"))
        row("City", r.get("city"))
        row("Country", r.get("country"))
        row("Auth", "OPEN ⚠" if not r.get("auth_required") else "AUTH")
        row("Last seen", r.get("last_seen", "")[:10])
        for lnk in r.get("links", []):
            rows.append(f"<tr><td colspan='2'>{link(lnk, lnk)}</td></tr>")
        row("Shodan", link(r.get("shodan_url"), "View on Shodan"))

    elif rtype == "lpr":
        row("ID", r.get("id"))
        row("Type", r.get("device_type"))
        row("Label", r.get("label"))
        row("Operator", r.get("operator"))
        row("Manufacturer", r.get("manufacturer"))
        row("Direction", r.get("direction"))
        row("Zone", r.get("zone"))
        row("OSM", link(r.get("osm_url"), "View on OSM"))
        row("DeFlock", link(r.get("deflock_url"), "View on DeFlock"))

    elif rtype == "wifi":
        row("ID", r.get("id"))
        row("SSID", r.get("ssid"))
        row("BSSID", r.get("bssid"))
        enc = r.get("encryption", "")
        flag = "OPEN ⚠" if r.get("open") else ("WEP ⚠" if r.get("weak") else enc)
        row("Encryption", flag)
        row("Channel", r.get("channel"))
        row("Last seen", r.get("last_seen", "")[:10])
        row("Map", link(r.get("wigle_url"), "View on Wigle"))

    elif rtype == "cells":
        row("ID", r.get("id"))
        row("Radio", r.get("radio"))
        row("Carrier", r.get("carrier"))
        row("MCC/MNC", f"{r.get('mcc')}/{r.get('mnc')}")
        row("LAC/CID", f"{r.get('lac')}/{r.get('cell_id')}")
        row("Range", f"{r.get('range_m')}m" if r.get("range_m") else None)
        row("Samples", r.get("samples"))
        row("Map", link(r.get("opencellid_url"), "View on OpenCelliD"))

    elif rtype == "flights":
        row("ID", r.get("id"))
        row("Callsign", r.get("callsign"))
        row("ICAO24", r.get("icao24"))
        row("Country", r.get("origin_country"))
        row("Status", "On Ground" if r.get("on_ground") else "Airborne")
        row("Altitude", f"{r.get('altitude_m')}m" if r.get("altitude_m") is not None else None)
        row("Speed", f"{r.get('velocity_ms')} m/s" if r.get("velocity_ms") is not None else None)
        row("Heading", f"{r.get('heading')}°" if r.get("heading") is not None else None)
        row("FlightAware", link(r.get("flightaware_url"), "FlightAware"))
        row("FlightRadar24", link(r.get("flightradar_url"), "FlightRadar24"))
        row("OpenSky", link(r.get("opensky_url"), "OpenSky"))

    elif rtype == "ships":
        row("ID", r.get("id"))
        row("Vessel", r.get("vessel_name"))
        row("MMSI", r.get("mmsi"))
        row("Type", r.get("ship_type"))
        row("Flag", r.get("flag"))
        row("Speed", f"{r.get('speed')} kn" if r.get("speed") is not None else None)
        row("Course", f"{r.get('course')}°" if r.get("course") is not None else None)
        row("Destination", r.get("destination"))
        row("Last seen", r.get("last_seen", "")[:19])
        row("MarineTraffic", link(r.get("marinetraffic_url"), "MarineTraffic"))
        row("VesselFinder", link(r.get("vesselfinder_url"), "VesselFinder"))

    elif rtype == "satellites":
        row("ID", r.get("id"))
        row("Name", r.get("name"))
        row("NORAD ID", r.get("norad_id"))
        row("Altitude", f"{r.get('altitude_km')} km" if r.get("altitude_km") is not None else None)
        row("Velocity", f"{r.get('velocity_kms')} km/s" if r.get("velocity_kms") is not None else None)
        row("Ground dist", f"{r.get('dist_km')} km" if r.get("dist_km") is not None else None)
        row("Next pass", r.get("next_pass_utc", "")[:19] if r.get("next_pass_utc") else None)
        row("Max elevation", f"{r.get('elevation_deg')}°" if r.get("elevation_deg") is not None else None)
        row("Azimuth", f"{r.get('azimuth_deg')}°" if r.get("azimuth_deg") is not None else None)
        row("N2YO", link(r.get("n2yo_url"), "N2YO"))
        row("Heavens-Above", link(r.get("heavens_above_url"), "Heavens-Above"))

    inner = "\n".join(rows)
    return f"<table style='font-size:12px;font-family:monospace;border-collapse:collapse'>{inner}</table>"


def generate_map(data: dict) -> str:
    meta    = data.get("meta", {})
    loc_str = meta.get("resolved_location", "0,0")
    try:
        center_lat, center_lon = [float(x) for x in loc_str.split(",")]
    except Exception:
        center_lat, center_lon = 0.0, 0.0

    radius_m  = meta.get("radius_m", 5000)
    query_loc = meta.get("query_location", "")
    timestamp = meta.get("timestamp", "")[:19].replace("T", " ")

    sections = data.get("data", [])

    # Count results per type
    counts = {}
    for sec in sections:
        rtype = sec.get("type", "")
        counts[rtype] = len([r for r in sec.get("results", []) if not r.get("_dry_run")])

    # Build header stats
    header_stats = "  ".join(
        f'<span style="color:{TYPE_CONFIG.get(t, {}).get("color","#fff")}">'
        f'{TYPE_CONFIG.get(t, {}).get("icon","■")} {t.upper()}: {n}</span>'
        for t, n in counts.items()
    )

    # Build JS marker blocks per layer
    layer_js_blocks = []
    for sec in sections:
        rtype = sec.get("type", "")
        cfg   = TYPE_CONFIG.get(rtype, {"color": "#ffffff", "label": rtype, "icon": "●"})
        color = cfg["color"]
        label = cfg["label"]
        markers_js = []

        for r in sec.get("results", []):
            if r.get("_dry_run"):
                continue
            r_lat = r.get("lat")
            r_lon = r.get("lon")
            if r_lat is None or r_lon is None:
                continue

            popup_html = _esc(_popup_rows(r, rtype))
            title = _esc(
                r.get("callsign") or r.get("vessel_name") or r.get("name") or
                r.get("ssid") or r.get("id") or rtype
            )

            # Rotation for directional types
            rotation = ""
            if rtype == "flights" and r.get("heading") is not None:
                rotation = f", {{'rotationAngle': {r['heading']}}}"
            elif rtype == "ships" and r.get("course") is not None:
                rotation = f", {{'rotationAngle': {r['course']}}}"

            markers_js.append(
                f"  L.marker([{r_lat}, {r_lon}], {{icon: makeIcon('{color}', '{_esc(cfg['icon'])}')}}"
                f"{rotation})"
                f".bindPopup('<b style=\"color:{color}\">{title}</b><br>{popup_html}', "
                f"{{maxWidth:360, maxHeight:400}})"
            )

        markers_str = ",\n".join(markers_js)
        layer_js_blocks.append(
            f"  var layer_{rtype} = L.layerGroup([\n{markers_str}\n  ]);\n"
            f"  overlays['{label} ({counts.get(rtype, 0)})'] = layer_{rtype};\n"
            f"  layer_{rtype}.addTo(map);"
        )

    all_layers_js = "\n".join(layer_js_blocks)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signals — {_esc(query_loc)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; color: #e0e0e0; font-family: 'Courier New', monospace; display: flex; flex-direction: column; height: 100vh; }}
  #header {{ background: #111; border-bottom: 1px solid #333; padding: 10px 16px; flex-shrink: 0; }}
  #header h1 {{ font-size: 14px; color: #5dade2; letter-spacing: 1px; text-transform: uppercase; }}
  #header .meta {{ font-size: 11px; color: #666; margin-top: 3px; }}
  #header .stats {{ font-size: 12px; margin-top: 6px; }}
  #map {{ flex: 1; }}
  .leaflet-popup-content-wrapper {{ background: #1a1a2e; border: 1px solid #333; color: #e0e0e0; border-radius: 4px; }}
  .leaflet-popup-tip {{ background: #1a1a2e; }}
  .leaflet-popup-content {{ margin: 10px 14px; }}
  .leaflet-control-layers {{ background: #111 !important; color: #e0e0e0; border: 1px solid #333 !important; }}
  .leaflet-control-layers-toggle {{ background-color: #111; }}
  .leaflet-control-layers label {{ color: #e0e0e0 !important; }}
  .leaflet-bar a {{ background-color: #111 !important; color: #e0e0e0 !important; border-bottom-color: #333 !important; }}
  .leaflet-bar a:hover {{ background-color: #222 !important; }}
</style>
</head>
<body>
<div id="header">
  <h1>▸ SIGNALS — {_esc(query_loc)}</h1>
  <div class="meta">
    Resolved: {_esc(loc_str)} &nbsp;|&nbsp; Radius: {radius_m:,}m &nbsp;|&nbsp; {_esc(timestamp)} UTC
  </div>
  <div class="stats">{header_stats}</div>
</div>
<div id="map"></div>
<script>
// ── Map init ──────────────────────────────────────────────────────────────────
var map = L.map('map', {{
  center: [{center_lat}, {center_lon}],
  zoom: {max(10, min(15, 14 - int(radius_m / 20000)))},
  preferCanvas: true
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd', maxZoom: 20
}}).addTo(map);

// Centre marker
L.circle([{center_lat}, {center_lon}], {{
  radius: {radius_m},
  color: '#5dade2', fillColor: '#5dade2', fillOpacity: 0.04,
  weight: 1, dashArray: '4 4'
}}).addTo(map);

L.circleMarker([{center_lat}, {center_lon}], {{
  radius: 6, color: '#5dade2', fillColor: '#5dade2', fillOpacity: 0.9, weight: 2
}}).bindPopup('<b style="color:#5dade2">Query Centre</b><br>{_esc(query_loc)}<br>{_esc(loc_str)}').addTo(map);

// ── Icon factory ─────────────────────────────────────────────────────────────
function makeIcon(color, emoji) {{
  return L.divIcon({{
    html: '<div style="font-size:18px;line-height:1;filter:drop-shadow(0 0 3px ' + color + ')">' + emoji + '</div>',
    iconSize: [24, 24], iconAnchor: [12, 12], popupAnchor: [0, -14],
    className: ''
  }});
}}

// ── Layers ───────────────────────────────────────────────────────────────────
var overlays = {{}};

{all_layers_js}

L.control.layers(null, overlays, {{collapsed: false, position: 'topright'}}).addTo(map);
</script>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Generate Leaflet HTML map from signals-search.py output")
    parser.add_argument("--input",  default="-",  help="Input JSON file (default: stdin)")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    parser.add_argument("--open",   action="store_true", help="Open map in browser after generating")
    args = parser.parse_args()

    # Read JSON
    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input) as f:
            raw = f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    html = generate_map(data)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"[map] Written: {args.output} ({size_kb:.1f} KB)", file=sys.stderr)

    if args.open:
        webbrowser.open(f"file://{os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
