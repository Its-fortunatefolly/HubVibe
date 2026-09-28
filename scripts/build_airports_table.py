"""Build the airport lookup travel.flight_status ships with.

OurAirports (https://ourairports.com/data/): "All data is released to the
Public Domain". Kept: airports with an IATA code or a 4-letter ICAO ident,
the fields needed to turn a code into a place.

    python3 scripts/build_airports_table.py
"""

import csv
import gzip
import io
import urllib.request
from pathlib import Path

SRC = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OUT = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "workers" / "providers" / "data" / "airports.csv.gz"
KEEP = {"large_airport", "medium_airport", "small_airport"}


def main() -> None:
    raw = urllib.request.urlopen(urllib.request.Request(SRC, headers={"User-Agent": "HubVibe build"}), timeout=120).read()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["iata", "icao", "name", "city", "country", "lat", "lon", "type"])
    n = 0
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        icao = (r.get("icao_code") or r.get("gps_code") or r.get("ident") or "").strip().upper()
        iata = (r.get("iata_code") or "").strip().upper()
        if r.get("type") not in KEEP or not (iata or len(icao) == 4) or r.get("scheduled_service") != "yes" and not iata:
            continue
        w.writerow([iata, icao if len(icao) == 4 else "", r.get("name"), r.get("municipality"), r.get("iso_country"),
                    r.get("latitude_deg"), r.get("longitude_deg"), r.get("type")])
        n += 1
    OUT.write_bytes(gzip.compress(out.getvalue().encode(), mtime=0))
    print(OUT.name, n, "airports", OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
