"""Dataset files -> rows, for opendata.table. Keyless, dependency-free.

A government dataset is usually a CSV in the country's legacy encoding
(CP949 in Korea, CP932/Shift_JIS in Japan, Big5 in Taiwan, GB18030 in
China), an XLSX, or a JSON array. This module fetches one through the same
target guard every fetch on the node uses, detects the encoding by trying
the candidates and scoring the script each yields, and parses the table
with the standard library only (csv, zipfile + XML for XLSX, json).
"""

import codecs
import csv
import io
import json
import os
import re
import zipfile
from typing import Optional
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from .. import runtime
from . import web

MAX_BYTES = int(os.environ.get("WORKER_TABLE_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_ROWS = 2000
MAX_COLUMNS = 200
MAX_CELL_CHARS = 2000

# Multi-byte candidates after UTF-8, with the Unicode ranges that prove each
# one decoded the script it is for. A wrong multi-byte codec usually fails to
# decode; when it does not, the script check catches it (Korean bytes read as
# Shift_JIS come out as half-width katakana and kanji, never full-width kana).
_MULTIBYTE = (
    ("cp949", ((0xAC00, 0xD7A3), (0x3131, 0x318E))),            # Korean: Hangul
    ("cp932", ((0x3040, 0x30FF), (0x4E00, 0x9FFF))),            # Japanese: kana + kanji
    ("euc_jp", ((0x3040, 0x30FF), (0x4E00, 0x9FFF))),
    ("gb18030", ((0x4E00, 0x9FFF),)),                           # Simplified Chinese
    ("big5", ((0x4E00, 0x9FFF),)),                              # Traditional Chinese (hint tw/hk)
)
MIN_KANA_RATIO = 0.02  # a Japanese codec chosen without a country hint must show some kana
# Single-byte code pages never fail to decode, so they are tried last and
# only win when the source's country says so or nothing else fits.
_SINGLEBYTE = ("cp1252", "cp1251", "cp874", "iso-8859-2", "iso-8859-7", "iso-8859-9")
# Where a file comes from is the strongest hint: ordered candidates by country code.
_HOST_HINTS = {"kr": ("cp949",), "jp": ("cp932", "euc_jp"), "tw": ("big5",), "hk": ("big5",), "cn": ("gb18030",),
               "th": ("cp874",), "ru": ("cp1251",), "ua": ("cp1251",), "gr": ("iso-8859-7",), "tr": ("iso-8859-9",)}
MIN_SCRIPT_RATIO = 0.5
_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_NUMBER = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


def _script_ratio(text: str, ranges, codec: str) -> float:
    """Share of non-ASCII characters that belong to the codec's own script.
    Two traps are closed: Korean or Chinese bytes read as Shift_JIS come out
    as half-width katakana (counted against cp932), and Japanese bytes read
    as CP949 come out as rare extended Hangul that KS X 1001 (euc_kr) cannot
    encode (counted against cp949)."""
    sample = text[:20000]
    non_ascii = [ch for ch in sample if ord(ch) > 0x7F]
    if not non_ascii:
        return 1.0
    hits = 0
    for ch in non_ascii:
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in ranges):
            if codec == "cp949" and 0xAC00 <= code <= 0xD7A3:
                try:
                    ch.encode("euc_kr")
                except UnicodeEncodeError:
                    continue
            hits += 1
    penalty = 0
    if codec in ("cp932", "euc_jp"):
        penalty = sum(1 for ch in non_ascii if 0xFF61 <= ord(ch) <= 0xFF9F)
    return max(hits - penalty, 0) / len(non_ascii)


def host_hint(url: Optional[str]) -> Optional[str]:
    """Country code from a hostname's public suffix (data.go.kr -> kr)."""
    if not url:
        return None
    host = urlparse(url).netloc.lower().split(":")[0]
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    return tld if tld in _HOST_HINTS else None


def decode_bytes(data: bytes, declared: Optional[str] = None, hint: Optional[str] = None) -> tuple:
    """(text, encoding, note). BOMs win; a declared charset is trusted only
    if it decodes AND is not a bare 'UTF-8' claim on non-UTF-8 bytes (the
    Korean portal declares UTF-8 on CP949 files); then UTF-8; then the
    multi-byte codecs, the source country's first; then single-byte pages."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig", None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16", None
    try:
        return data.decode("utf-8"), "utf-8", None
    except UnicodeDecodeError:
        pass
    if declared and declared.lower().replace("_", "-") not in ("utf-8", "utf8"):
        try:
            return data.decode(declared), declared.lower(), None
        except (UnicodeDecodeError, LookupError):
            pass
    preferred = list(_HOST_HINTS.get(hint or "", ()))
    ordered = [c for c in _MULTIBYTE if c[0] in preferred] + [c for c in _MULTIBYTE if c[0] not in preferred]
    best = None
    for name, ranges in ordered:
        try:
            text = data.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
        ratio = _script_ratio(text, ranges, name)
        if ratio < MIN_SCRIPT_RATIO:
            continue
        if name in preferred:
            return text, name, None
        if name in ("cp932", "euc_jp"):
            non_ascii = [ch for ch in text[:20000] if ord(ch) > 0x7F]
            kana = sum(1 for ch in non_ascii if 0x3040 <= ord(ch) <= 0x30FF)
            if non_ascii and kana / len(non_ascii) < MIN_KANA_RATIO:
                continue  # kanji-only text is more likely Chinese unless the source says Japan
        if best is None or ratio > best[0]:
            best = (ratio, name, text)
    if best:
        return best[2], best[1], None
    for name in preferred + [c for c in _SINGLEBYTE if c not in preferred]:
        try:
            text = data.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
        note = None if name in preferred else "Encoding could not be determined; decoded as %s, non-ASCII text may be wrong." % name
        return text, name, note
    return data.decode("latin-1"), "latin-1", "Encoding could not be determined; decoded as Latin-1, non-ASCII text may be wrong."


def _cell(value):
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    if text == "":
        return None
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS]
    if _NUMBER.match(text):
        plain = text.replace(",", "")
        try:
            return int(plain) if "." not in plain else float(plain)
        except ValueError:
            return text
    return text


def _header(cells: list) -> list:
    names, seen = [], {}
    for i, c in enumerate(cells[:MAX_COLUMNS]):
        name = str(c).strip() if c not in (None, "") else f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        names.append(name)
    return names


def rows_from_table(table: list, max_rows: int) -> dict:
    """[[header], [row], ...] -> columns + list-of-objects rows."""
    table = [r for r in table if any(c not in (None, "") for c in r)]
    if not table:
        return {"columns": [], "rows": [], "total_rows": 0, "truncated": False}
    columns = _header(table[0])
    body = table[1:]
    rows = []
    for r in body[:max_rows]:
        rows.append({columns[i]: _cell(r[i]) if i < len(r) else None for i in range(len(columns))})
    return {"columns": columns, "rows": rows, "total_rows": len(body), "truncated": len(body) > max_rows}


def parse_csv(text: str, max_rows: int, delimiter: Optional[str] = None) -> dict:
    sample = text[:20000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    table = []
    for i, row in enumerate(reader):
        table.append(row)
        if i > max_rows + 5000:
            break
    out = rows_from_table(table, max_rows)
    out["delimiter"] = delimiter
    return out


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(n - 1, 0)


def xlsx_sheets(data: bytes) -> list:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise runtime.InvalidRequest(f"The file is not a readable XLSX workbook: {exc}") from None
    sheets = wb.find("m:sheets", _XLSX_NS)
    return [s.get("name") for s in (sheets if sheets is not None else [])]


def parse_xlsx(data: bytes, max_rows: int, sheet=None) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise runtime.InvalidRequest(f"The file is not a readable XLSX workbook: {exc}") from None
    targets = {r.get("Id"): r.get("Target") for r in rels}
    sheets = []
    found = wb.find("m:sheets", _XLSX_NS)
    for s in (found if found is not None else []):
        rid = s.get("{%s}id" % _XLSX_NS["r"])
        target = targets.get(rid, "")
        path = target if target.startswith("/") else "xl/" + target
        sheets.append((s.get("name"), path.lstrip("/")))
    if not sheets:
        raise runtime.InvalidRequest("The workbook has no sheets.")
    names = [n for n, _ in sheets]
    if sheet is None:
        chosen = sheets[0]
    elif isinstance(sheet, int):
        if not 0 <= sheet < len(sheets):
            raise runtime.InvalidRequest(f"`sheet` index {sheet} is out of range; the workbook has {len(sheets)} sheet(s): {names}.")
        chosen = sheets[sheet]
    else:
        match = [s for s in sheets if s[0] == sheet]
        if not match:
            raise runtime.InvalidRequest(f"`sheet` {sheet!r} is not in the workbook; sheets: {names}.")
        chosen = match[0]
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _XLSX_NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _XLSX_NS["m"])))
    try:
        root = ET.fromstring(z.read(chosen[1]))
    except (KeyError, ET.ParseError) as exc:
        raise runtime.InvalidProviderResponse(f"Sheet {chosen[0]!r} could not be read: {exc}") from None
    table = []
    for row in root.iter("{%s}row" % _XLSX_NS["m"]):
        cells = {}
        for c in row.findall("m:c", _XLSX_NS):
            idx = _col_index(c.get("r") or "")
            kind = c.get("t")
            v = c.find("m:v", _XLSX_NS)
            if kind == "s" and v is not None:
                value = shared[int(v.text)] if v.text and v.text.isdigit() and int(v.text) < len(shared) else None
            elif kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter("{%s}t" % _XLSX_NS["m"]))
            elif kind == "b" and v is not None:
                value = v.text == "1"
            elif v is not None and v.text is not None:
                try:
                    value = int(v.text) if re.match(r"^-?\d+$", v.text) else float(v.text)
                except ValueError:
                    value = v.text
            else:
                value = None
            cells[idx] = value
        if cells:
            width = max(cells) + 1
            table.append([cells.get(i) for i in range(min(width, MAX_COLUMNS))])
        if len(table) > max_rows + 5000:
            break
    out = rows_from_table(table, max_rows)
    out["sheet"] = chosen[0]
    out["sheets"] = names
    return out


def parse_json(text: str, max_rows: int) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise runtime.InvalidRequest(f"The file is not valid JSON: {exc}") from None
    records = None
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
        if len(lists) == 1:
            records = lists[0]
        elif "data" in data and isinstance(data["data"], list):
            records = data["data"]
    if records is None or not all(isinstance(r, dict) for r in records):
        raise runtime.InvalidRequest("The JSON is not a list of records (an array of objects, or an object holding one).")
    columns = []
    for r in records[:1000]:
        for k in r:
            if k not in columns and len(columns) < MAX_COLUMNS:
                columns.append(str(k))
    rows = [{c: _cell(r.get(c)) if not isinstance(r.get(c), (dict, list)) else r.get(c) for c in columns} for r in records[:max_rows]]
    return {"columns": columns, "rows": rows, "total_rows": len(records), "truncated": len(records) > max_rows}


def filename_from(headers: dict, url: str) -> Optional[str]:
    cd = headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1)).strip()
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return m.group(1).strip()
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) or None


def sniff_format(data: bytes, content_type: str, filename: Optional[str]) -> str:
    name = (filename or "").lower()
    if data[:2] == b"PK":
        return "xlsx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    head = data[:2000].lstrip()
    if name.endswith(".json") or "json" in content_type or head[:1] in (b"[", b"{"):
        return "json"
    if name.endswith(".tsv") or "tab-separated" in content_type:
        return "tsv"
    if head[:1] == b"<" and (b"<html" in head.lower() or b"<!doctype" in head.lower()):
        return "html"
    return "csv"


class _TableFetcher:
    id = "http-fetch-table"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def fetch(self, url: str) -> runtime.ProviderResult:
        response, final_url = await web._get_guarded(url)
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(f"The file URL answered HTTP {response.status_code}.")
        data = response.content or b""
        if len(data) > MAX_BYTES:
            raise runtime.InvalidRequest(f"The file is {len(data) // (1024 * 1024)} MB, over the {MAX_BYTES // (1024 * 1024)} MB limit.")
        content_type = (response.headers.get("content-type") or "").lower()
        charset = None
        m = re.search(r"charset=([\w\-]+)", content_type)
        if m:
            charset = m.group(1)
        headers = {k.lower(): v for k, v in response.headers.items()}
        return runtime.ProviderResult(
            value={"data": data, "final_url": final_url, "content_type": content_type.split(";")[0].strip() or None,
                   "charset": charset, "filename": filename_from(headers, final_url), "bytes": len(data)},
            cost_micros=0, cost_measured=True, usage=f"bytes={len(data)} status={response.status_code}")


PROVIDERS = [_TableFetcher()]
