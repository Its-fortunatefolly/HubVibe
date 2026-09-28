"""Build the two tract tables property.context ships with (public domain).

ACS 5-year (Census Bureau, table-based summary files, no key needed) and the
FHFA annual tract house price index. Both publish once a year; rerun this
when they do and commit the outputs.

    python3 scripts/build_property_tables.py [ACS_YEAR]
"""

import csv
import gzip
import io
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "workers" / "providers" / "data"
ACS_YEAR = sys.argv[1] if len(sys.argv) > 1 else "2024"
ACS_URL = ("https://www2.census.gov/programs-surveys/acs/summary_file/{y}/table-based-SF/data/5YRData/"
           "acsdt5y{y}-{t}.dat")
FHFA_URL = "https://www.fhfa.gov/hpi/download/annual/hpi_at_tract.csv"
UA = {"User-Agent": "HubVibe build (+https://hubvibe-io.com)"}
# table -> columns kept
TABLES = {"b25077": ["E001", "M001"], "b25064": ["E001"], "b19013": ["E001"], "b01003": ["E001"],
          "b25003": ["E001", "E002"], "b25035": ["E001"], "b25002": ["E001", "E003"]}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=600) as r:
        return r.read()


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else f  # ACS annotates suppressed cells with negative codes


def acs() -> None:
    tracts = {}
    for table, cols in TABLES.items():
        text = fetch(ACS_URL.format(y=ACS_YEAR, t=table)).decode("utf-8")
        reader = csv.reader(io.StringIO(text), delimiter="|")
        header = next(reader)
        idx = {c: header.index(f"{table.upper()}_{c}") for c in cols}
        for row in reader:
            geo = row[0]
            if not geo.startswith("1400000US"):
                continue
            t = tracts.setdefault(geo[9:], {})
            for c, i in idx.items():
                t[f"{table}_{c}"] = num(row[i])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["tract", "median_home_value", "median_home_value_moe", "median_gross_rent", "median_household_income",
                "population", "owner_occupied_pct", "median_year_built", "vacancy_pct"])
    for tract in sorted(tracts):
        t = tracts[tract]
        occ, own = t.get("b25003_E001"), t.get("b25003_E002")
        units, vac = t.get("b25002_E001"), t.get("b25002_E003")
        def i(v):
            return "" if v is None else str(int(round(v)))
        w.writerow([tract, i(t.get("b25077_E001")), i(t.get("b25077_M001")), i(t.get("b25064_E001")),
                    i(t.get("b19013_E001")), i(t.get("b01003_E001")),
                    "" if not occ or own is None else f"{own / occ * 100:.1f}", i(t.get("b25035_E001")),
                    "" if not units or vac is None else f"{vac / units * 100:.1f}"])
    path = OUT / f"acs5_{int(ACS_YEAR) - 4}_{ACS_YEAR}_tracts.csv.gz"
    path.write_bytes(gzip.compress(out.getvalue().encode(), mtime=0))
    print(path.name, len(tracts), "tracts", path.stat().st_size, "bytes")


def fhfa() -> None:
    rows = {}
    reader = csv.DictReader(io.StringIO(fetch(FHFA_URL).decode("utf-8")))
    for r in reader:
        hpi, chg = num(r.get("hpi")), r.get("annual_change")
        rows.setdefault(r["tract"], {})[int(r["year"])] = (hpi, None if chg in ("", ".", None) else num(chg) if not chg.startswith("-") else float(chg))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["tract", "latest_year", "annual_change_pct", "five_year_change_pct"])
    latest_all = 0
    for tract in sorted(rows):
        years = {y: v for y, v in rows[tract].items() if v[0] is not None}
        if not years:
            continue
        y = max(years)
        latest_all = max(latest_all, y)
        chg = years[y][1]
        five = None
        if y - 5 in years and years[y - 5][0]:
            five = (years[y][0] / years[y - 5][0] - 1) * 100
        w.writerow([tract, y, "" if chg is None else f"{chg:.2f}", "" if five is None else f"{five:.2f}"])
    path = OUT / "fhfa_tract_hpi.csv.gz"
    path.write_bytes(gzip.compress(out.getvalue().encode(), mtime=0))
    print(path.name, len(rows), "tracts, latest year", latest_all, path.stat().st_size, "bytes")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    acs()
    fhfa()
