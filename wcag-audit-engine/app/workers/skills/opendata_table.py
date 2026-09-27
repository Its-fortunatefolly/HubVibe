"""opendata.table -- one government dataset file, returned as rows.

The search bee finds the dataset; this bee makes it usable: fetch the file
(a direct CSV/TSV/XLSX/JSON link, an e-Stat file-download link, or a
data.go.kr dataset page, which is resolved to its keyless file link first),
detect the encoding (Korean CP949, Japanese CP932, Chinese Big5/GB18030,
Western code pages, UTF-8/16), parse the table with the standard library,
and return columns and rows as JSON with numbers typed. The header row is
the first non-empty row; XLSX sheets are selectable; large files are capped
and disclosed. Nothing is cached.
"""

import codecs
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import opendata, tabular
from .extract import validate_url

MAX_ROWS = tabular.MAX_ROWS


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    url = validate_url(payload.get("url"))
    max_rows = payload.get("max_rows", 200)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise runtime.InvalidRequest(f"`max_rows` must be a whole number from 1 to {MAX_ROWS}.")
    sheet = payload.get("sheet")
    if sheet is not None and (isinstance(sheet, bool) or not isinstance(sheet, (int, str)) or (isinstance(sheet, str) and not sheet.strip())):
        raise runtime.InvalidRequest("`sheet`, when given, is a sheet name or a 0-based sheet index (XLSX only).")
    encoding = payload.get("encoding")
    if encoding is not None:
        if not isinstance(encoding, str) or not encoding.strip():
            raise runtime.InvalidRequest("`encoding`, when given, must be a codec name such as cp949 or shift_jis.")
        try:
            codecs.lookup(encoding.strip())
        except LookupError:
            raise runtime.InvalidRequest(f"`encoding` {encoding!r} is not a known codec.") from None
        encoding = encoding.strip()
    delimiter = payload.get("delimiter")
    if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1):
        raise runtime.InvalidRequest("`delimiter`, when given, must be a single character.")
    return {"url": url, "max_rows": max_rows, "sheet": sheet.strip() if isinstance(sheet, str) else sheet,
            "encoding": encoding, "delimiter": delimiter}


def precheck(payload: dict) -> None:
    parse(payload)


async def table(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes = []
    url = req["url"]
    resolved_from = None
    dataset_id = opendata.is_datagokr_dataset_url(url)
    if dataset_id:
        kr = opendata.ADAPTERS["data.go.kr"]

        async def resolve(provider):
            return await provider.resolve_download(dataset_id)
        found = await ctx.run("resolve:data.go.kr", [kr], resolve, per_attempt_seconds=25, max_attempts=2)
        resolved_from = url
        url = found["url"]
        if found.get("title"):
            notes.append(f"data.go.kr dataset: {found['title']}")

    async def fetch(provider):
        return await provider.fetch(url)
    got = await ctx.run("fetch", tabular.PROVIDERS, fetch, per_attempt_seconds=60, max_attempts=2)
    data = got["data"]
    if not data:
        raise runtime.PermanentProviderError("The file is empty.")
    fmt = tabular.sniff_format(data, got["content_type"] or "", got["filename"])
    encoding: Optional[str] = None
    sheet = sheets = None
    if fmt == "xls":
        raise runtime.InvalidRequest("Legacy .xls workbooks are not supported; ask for a CSV or XLSX resource of the dataset.")
    if fmt == "html":
        raise runtime.InvalidRequest("The URL returned a web page, not a data file. Give the file's own link (or a data.go.kr dataset page).")
    if fmt == "xlsx":
        parsed = tabular.parse_xlsx(data, req["max_rows"], req["sheet"])
        sheet, sheets = parsed.pop("sheet"), parsed.pop("sheets")
    else:
        text, encoding, note = tabular.decode_bytes(data, req["encoding"] or got.get("charset"),
                                                    hint=tabular.host_hint(url) or tabular.host_hint(resolved_from))
        if note:
            notes.append(note)
        if fmt == "json":
            parsed = tabular.parse_json(text, req["max_rows"])
        else:
            parsed = tabular.parse_csv(text, req["max_rows"], req["delimiter"] or ("\t" if fmt == "tsv" else None))
            fmt = "tsv" if parsed.get("delimiter") == "\t" else "csv"
    parsed.pop("delimiter", None)
    if not parsed["columns"]:
        raise runtime.PermanentProviderError("The file parsed but holds no table (no header row found).")
    if parsed["truncated"]:
        notes.append(f"{parsed['total_rows']} data rows in the file; the first {req['max_rows']} are returned (max_rows).")
    return {
        "source_url": req["url"],
        "resolved_from": resolved_from,
        "file_url": url,
        "final_url": got["final_url"],
        "filename": got["filename"],
        "content_type": got["content_type"],
        "bytes": got["bytes"],
        "format": fmt,
        "encoding": encoding,
        "sheet": sheet,
        "sheets": sheets or [],
        "columns": parsed["columns"],
        "column_count": len(parsed["columns"]),
        "rows": parsed["rows"],
        "row_count": len(parsed["rows"]),
        "total_rows": parsed["total_rows"],
        "truncated": parsed["truncated"],
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"opendata.table": table}
PRECHECKS = {"opendata.table": precheck}
