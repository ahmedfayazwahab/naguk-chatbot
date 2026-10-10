"""One-time geocoding: adds latitude/longitude to every open restaurant record.

Uses OpenStreetMap's free Nominatim search (no API key, no cost). Nominatim's
usage policy allows at most 1 request per second and requires a descriptive
User-Agent, so this sleeps between calls. Run it again only when a restaurant
is added or moves; the coordinates live in the JSON files like any other field.
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "restaurants"
HEADERS = {"User-Agent": "naguk-chatbot-geocoder/1.0"}


def search(query: str):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "ca"}
    )
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=10) as resp:
        results = json.loads(resp.read())
    time.sleep(1.1)
    return (float(results[0]["lat"]), float(results[0]["lon"])) if results else None


def main() -> None:
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue
        r = json.loads(path.read_text())
        if r.get("status") == "upcoming" or not r.get("address"):
            continue

        street = f"{r['address']}, {r.get('city', '')}, {r.get('province', '')}, {r.get('postal_code', '')}, Canada"
        coords = search(street)
        source = "full address"
        if not coords and r.get("postal_code"):
            coords = search(f"{r['postal_code']}, Canada")
            source = "postal code only"
        if not coords:
            print(f"FAILED   {path.stem}: {street}")
            continue

        r["latitude"], r["longitude"] = coords
        path.write_text(json.dumps(r, indent=2))
        print(f"ok ({source:16s}) {path.stem}: {coords}")


if __name__ == "__main__":
    main()
