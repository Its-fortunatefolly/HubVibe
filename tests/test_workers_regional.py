"""Regional data finished: Korea's portal keyless, the EU catalog, 16 portals,
and opendata.table turning a dataset file into typed rows.

Pinned: data.go.kr's search page (captured 2026-09-27) parsed into dataset
rows with organisation, date, formats and keywords; its dataset page
resolved to the keyless file link; an EU DCAT record parsed with language
pick and distributions; encoding detection on CP949/CP932/UTF-8/UTF-16 and
a wrong declared charset ignored; CSV sniffing and number typing; XLSX read
with the standard library (shared strings, inline strings, numbers, sheet
choice); JSON records; the table skill resolving a Korean dataset page
first; every bad input refused free; paid HTTP + MCP; manifests.
"""

import asyncio
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "opendata.table", "/work/opendata/table", "hubvibe_opendata_table"

KR_SEARCH_PAGE = '<html><body><div class="data-list-group"><div class="data-result-tit">\n\t\t<p class="tit">\n\t\t\t파일데이터\n\t\t\t<span>\n\t\t\t\t\n\t\t\t\t\t(3,229건)\n\t\t\t\t\n\t\t\t</span>\n\t\t</p>\n\t\t\n\t</div>\n\t<div class="apply-result">\n\t\t\n\t\t\t\n\t\t\t\n\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\t\n\n\n\n\n\n\n \n<div class="apply-result-item">\n    <div class="in-result-item">\n        <div class="apply-result-category">\n            \n                <span class="krds-badge small bg-light-secondary">공공행정</span>\n            \n            \n                <span class="krds-badge small bg-light-primary">자치행정기관</span>\n            \n            \n        </div>\n        <div class="apply-result-link">\n            \n                <span class="krds-badge file-ext-css" data-ext="CSV">\n                    CSV\n                </span>\n            \n\n            \n                <span class="krds-badge bg-light-primary">JSON + XML</span>\n            \n\n            <a href="/data/15005995/fileData.do">\n                \n                    \n                        울산광역시_<em>인구</em> 현황\n                    \n                    \n                    \n                    \n                \n            </a>\n        </div>\n        <span class="apply-result-summary">\n            \n                \n                \n                    본 데이터는 관내 <em>인구</em> 현황에 관한 통계 정보이다. 총인구, 성별 <em>인구</em>, 연령대별 <em>인구</em>, 세대수, 구·군별 <em>인구</em> 분포 등의 세부 내용을 포함한다.\n                \n                \n            \n        </span>\n        <ul role="none">\n            <li role="none">\n                <strong>제공기관</strong>\n                \n                    \n                    \n                    \n                        울산광역시\n                    \n                \n            </li>\n            <li role="none">\n                <strong>수정일</strong>\n                \n                2025-11-12\n            </li>\n            <li role="none">\n                <strong>조회수</strong>\n                \n                    \n                    \n                        \n                        36,832건\n                    \n                \n            </li>\n            <li role="none">\n            \t<strong>다운로드</strong>\n            \t\t\n            \t\t8,005건\n            \t</li>\n            <li role="none">\n                <strong>키워드</strong>\n                \n                    \n                    \n                        \n                        인구통계,인구수,인구분포,연령별인구,성별인구,세대수,인구동향,인구구조\n                    \n                \n            </li>\n        </ul>\n    </div>\n    <div class="apply-result-btn-group">\n        <button type="button" onclick="searchObj.fn_preview(\'15005995\', \'FILE\')"\n                class="krds-btn medium text pc-ver" data-target="modal_guide_03" title="울산광역시_인구 현황 미리보기">\n            <i class="svg-icon ico-pw-visible-on"></i>미리보기\n        </button>\n        \n            \n                <button type="button" onclick="fileDetailObj.fn_fileDataDown(\'15005995\',\n                        \'uddi:38f18153-efdc-4af1-b083-c766d6755990_201909241544\',\n                        \'\',\n                        \'1\',\n                        \'1\'\n                        )"\n                        class="krds-btn medium secondary" title="울산광역시_인구 현황 다운로드">다운로드 <i class="svg-icon ico-down"></i></button>\n\n            \n            \n        \n        \n    </div>\n</div>\n\n\t\t\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\t\n\n\n\n\n\n\n \n\n<div class="apply-result-item">\n    <div class="in-result-item">\n        <div class="apply-result-category">\n            \n                <span class="krds-badge small bg-light-secondary">공공행정</span>\n            \n            \n                <span class="krds-badge small bg-light-primary">자치행정기관</span>\n            \n            \n        </div>\n        <div class="apply-result-link">\n            \n                <span class="krds-badge file-ext-css" data-ext="CSV">\n                    CSV\n                </span>\n            \n\n            \n                <span class="krds-badge bg-light-primary">JSON + XML</span>\n            \n\n            <a href="/data/15118049/fileData.do">\n                \n                    \n                        경기도 화성시_<em>인구</em>\n                    \n                    \n                    \n                    \n                \n            </a>\n        </div>\n        <span class="apply-result-summary">\n            \n                \n                \n                    경기도 화성시_<em>인구</em> 공공데이터는 화성시 행정구역별 <em>인구</em> 현황을 제공하는 정형 데이터로, 총인구수뿐만 아니라 남자인구수, 여자인구수, 그리고 0세부터 110세 이상까지 연령대를 10세\n                \n                \n            \n        </span>\n        <ul role="none">\n            <li role="none">\n                <strong>제공기관</strong>\n                \n                    \n                    \n                    \n                        경기도 화성시\n                    \n                \n            </li>\n            <li role="none">\n                <strong>수정일</strong>\n                \n                2026-09-11\n            </li>\n            <li role="none">\n                <strong>조회수</strong>\n                \n                    \n                    \n                        \n                        29,184건\n                    \n                \n            </li>\n            <li role="none">\n            \t<strong>다운로드</strong>\n            \t\t\n            \t\t3,230건\n            \t</li>\n            <li role="none">\n                <strong>키워드</strong>\n                \n                    \n                    \n                        \n                        성별인구,인구수,연령별인구,인구통계,고령화,저출산,행정구역별인구\n                    \n                \n            </li>\n        </ul>\n    </div>\n    <div class="apply-result-btn-group">\n        <button type="button" onclick="searchObj.fn_preview(\'15118049\', \'FILE\')"\n                class="krds-btn medium text pc-ver" data-target="modal_guide_03" title="경기도 화성시_인구 미리보기">\n            <i class="svg-icon ico-pw-visible-on"></i>미리보기\n        </button>\n        \n            \n                <button type="button" onclick="fileDetailObj.fn_fileDataDown(\'15118049\',\n                        \'uddi:d0608c93-3f21-4d11-a1bb-99ee1a8d2d55\',\n                        \'\',\n                        \'1\',\n                        \'1\'\n                        )"\n                        class="krds-btn medium secondary" title="경기도 화성시_인구 다운로드">다운로드 <i class="svg-icon ico-down"></i></button>\n\n            \n            \n        \n        \n    </div>\n</div>\n\n\t\t\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\n\t\t\t\t\t\t\t\n\n\n\n\n\n\n \n</div></body></html>'
KR_DATASET_PAGE = '<html><head><title>울산광역시_인구 현황_20251112 | 공공데이터포털</title></head><body><a href="/cmm/cmm/fileDownload.do?atchFileId=FILE_000000003522968&fileDetailSn=1&insertDataPrcus=N">다운로드</a></body></html>'
EU_RECORD = json.loads(r"""{"id": "malta-population-2025", "title": {"en": "Total Population and Maltese population", "mt": "Popolazzjoni"}, "description": {"en": "Population by locality.", "mt": "..."}, "publisher": {"type": "Agent", "name": "Planning Authority"}, "modified": "2025-07-29T06:03:52", "country": {"label": "Malta", "id": "mt"}, "resource": "https://data.europa.eu/88u/dataset/malta-population-2025", "keywords": [{"label": {"en": "population"}}], "distributions": [{"access_url": ["https://example.org/pop.csv"], "download_url": null, "format": {"id": "CSV", "label": "CSV"}, "title": {"en": "Population CSV"}, "modified": "2025-01-29T16:21:25", "byte_size": null}, {"access_url": [], "download_url": "https://example.org/pop.xlsx", "format": "XLSX", "title": "Workbook", "byte_size": 1024}]}""")
KR_CSV_TEXT = "행정구역코드,행정구역명,성별,내국인,순이동\n31,울산광역시,남자,564888 ,-1655 \n31,울산광역시,여자,\"1,234\",12\n"
JP_CSV_TEXT = "都道府県;人口;備考\n東京都;14000000;首都\n大阪府;8800000;\n"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
OD, TB = W.providers.opendata, W.providers.tabular
SK = W.skills.opendata_table


@pytest.fixture(autouse=True)
def _target_guard(monkeypatch):
    """main.py injects the audit engine's SSRF guard into the web provider at
    startup; these unit tests run without main, so give the provider a guard
    with the same shape (refuse link-local/private, allow the public web)."""
    def guard(url):
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host.startswith(("169.254.", "10.", "127.", "localhost", "192.168.")):
            return "must not point at a private, loopback or link-local address"
        return None
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason", guard)


def _run(coro):
    return asyncio.run(coro)


def _xlsx(sheets: dict) -> bytes:
    """A minimal real XLSX: shared strings for text, inline for one cell, numbers as numbers."""
    shared, sheet_xml = [], {}
    for name, rows in sheets.items():
        body = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if value is None:
                    continue
                if isinstance(value, bool):
                    cells.append(f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>')
                elif isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                elif value.startswith("inline:"):
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value[7:]}</t></is></c>')
                else:
                    if value not in shared:
                        shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheet_xml[name] = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                           f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    names = list(sheets)
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)) + '</sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names)))
            + '</Relationships>')
    sst = ('<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        for i, n in enumerate(names):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", sheet_xml[n])
    return buf.getvalue()


# --- Korea: the portal's public pages ----------------------------------------

def test_the_korean_search_page_becomes_dataset_rows():
    parsed = OD.parse_datagokr_search(KR_SEARCH_PAGE)
    assert parsed["count"] == 3229 and len(parsed["results"]) == 2
    first = parsed["results"][0]
    assert first["id"] == "15005995" and first["title"] == "울산광역시_인구 현황"
    assert first["organization"] == "울산광역시" and first["updated_at"] == "2025-11-12"
    assert "CSV" in first["formats"] and first["tags"][0] == "인구통계" and "인구" in first["description"]


def test_the_korean_dataset_page_resolves_to_its_keyless_file_link():
    link = OD.parse_datagokr_download(KR_DATASET_PAGE)
    assert link.startswith("https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_") and "fileDetailSn=1" in link
    assert OD.parse_datagokr_download("<html>nothing</html>") is None
    assert OD.is_datagokr_dataset_url("https://www.data.go.kr/data/15005995/fileData.do") == "15005995"
    assert OD.is_datagokr_dataset_url("https://data.go.kr/data/15005995/fileData.do") == "15005995"
    assert OD.is_datagokr_dataset_url("https://www.data.go.kr/cmm/cmm/fileDownload.do?x=1") is None
    assert OD.is_datagokr_dataset_url("https://example.com/data/15005995/fileData.do") is None


def test_the_korean_adapter_is_registered_keyless_and_returns_the_common_record_shape():
    kr = OD.ADAPTERS["data.go.kr"]
    assert kr.id == "datagokr:data.go.kr" and kr.available()
    assert isinstance(OD.ADAPTERS["data.europa.eu"], OD._Europa) and isinstance(OD.ADAPTERS["data.gov.ie"], OD._Ckan)


# --- EU: DCAT records ---------------------------------------------------------

def test_an_eu_record_is_parsed_with_language_pick_and_distributions():
    rec = OD._europa_package(EU_RECORD, "data.europa.eu", ["en"])
    assert rec["title"] == "Total Population and Maltese population" and rec["description"] == "Population by locality."
    assert rec["organization"] == "Planning Authority" and rec["country"] == "MT" and rec["updated_at"] == "2025-07-29T06:03:52"
    assert rec["tags"] == ["population"] and rec["landing_url"].startswith("https://data.europa.eu/")
    assert rec["resources"][0] == {"url": "https://example.org/pop.csv", "format": "CSV", "name": "Population CSV",
                                   "last_modified": "2025-01-29T16:21:25", "size_bytes": None}
    assert rec["resources"][1]["url"] == "https://example.org/pop.xlsx" and rec["resources"][1]["format"] == "XLSX"
    assert rec["resource_count"] == 2
    assert OD._lang_pick({"mt": "x", "de": "y"}, ["en"]) in ("x", "y") and OD._lang_pick({"fr": "b", "en": "a"}, ["de"]) == "a"


# --- files -> rows ------------------------------------------------------------

def test_encodings_are_detected_from_the_script_they_decode_to():
    text, enc, note = TB.decode_bytes(KR_CSV_TEXT.encode("cp949"))
    assert enc == "cp949" and "울산광역시" in text and note is None
    text, enc, _ = TB.decode_bytes("都道府県,ひらがな\n東京都,とうきょう\n".encode("cp932"))
    assert enc == "cp932" and "東京都" in text, "Japanese with kana is recognised without a hint"
    text, enc, _ = TB.decode_bytes(JP_CSV_TEXT.encode("cp932"), hint="jp")
    assert enc == "cp932" and "大阪府" in text, "kanji-only Japanese needs the source country (e-stat.go.jp)"
    text, enc, _ = TB.decode_bytes("城市,人口\n北京市,21000000\n".encode("gb18030"))
    assert enc == "gb18030" and "北京市" in text
    text, enc, _ = TB.decode_bytes("縣市,人口\n臺北市,2500000\n".encode("big5"), hint="tw")
    assert enc == "big5" and "臺北市" in text
    text, enc, _ = TB.decode_bytes(KR_CSV_TEXT.encode("cp949"), hint="kr")
    assert enc == "cp949"
    assert TB.host_hint("https://www.data.go.kr/data/1/fileData.do") == "kr" and TB.host_hint("https://www.e-stat.go.jp/x") == "jp"
    assert TB.host_hint("https://example.com/a.csv") is None and TB.host_hint(None) is None
    text, enc, _ = TB.decode_bytes(b"\xef\xbb\xbf" + KR_CSV_TEXT.encode("utf-8"))
    assert enc == "utf-8-sig" and text.startswith("행정구역코드")
    text, enc, _ = TB.decode_bytes(KR_CSV_TEXT.encode("utf-16"))
    assert enc == "utf-16" and "울산" in text
    text, enc, _ = TB.decode_bytes(KR_CSV_TEXT.encode("cp949"), declared="utf-8")
    assert enc == "cp949", "a wrong declared charset must not be trusted"
    text, enc, _ = TB.decode_bytes("café,1\n".encode("cp1252"), declared="cp1252")
    assert enc == "cp1252" and "café" in text


def test_csv_is_sniffed_and_numbers_are_typed():
    out = TB.parse_csv(KR_CSV_TEXT, max_rows=10)
    assert out["columns"] == ["행정구역코드", "행정구역명", "성별", "내국인", "순이동"] and out["delimiter"] == ","
    assert out["rows"][0] == {"행정구역코드": 31, "행정구역명": "울산광역시", "성별": "남자", "내국인": 564888, "순이동": -1655}
    assert out["rows"][1]["내국인"] == 1234 and out["total_rows"] == 2 and out["truncated"] is False
    out = TB.parse_csv(JP_CSV_TEXT, max_rows=1)
    assert out["delimiter"] == ";" and out["columns"] == ["都道府県", "人口", "備考"] and out["truncated"] is True
    assert out["rows"] == [{"都道府県": "東京都", "人口": 14000000, "備考": "首都"}] and out["total_rows"] == 2
    dup = TB.rows_from_table([["a", "a", ""], [1, 2, 3]], 5)
    assert dup["columns"] == ["a", "a_2", "column_3"]


def test_xlsx_is_read_with_the_standard_library_and_sheets_are_selectable():
    data = _xlsx({"Data": [["name", "value", "ok"], ["Tokyo", 14000000, True], ["inline:Osaka", 8.8, False]],
                  "Notes": [["note"], ["second sheet"]]})
    out = TB.parse_xlsx(data, max_rows=10)
    assert out["sheet"] == "Data" and out["sheets"] == ["Data", "Notes"]
    assert out["columns"] == ["name", "value", "ok"]
    assert out["rows"] == [{"name": "Tokyo", "value": 14000000, "ok": True}, {"name": "Osaka", "value": 8.8, "ok": False}]
    assert TB.parse_xlsx(data, 10, sheet="Notes")["rows"] == [{"note": "second sheet"}]
    assert TB.parse_xlsx(data, 10, sheet=1)["sheet"] == "Notes"
    for bad in ("Missing", 5):
        with pytest.raises(W.runtime.InvalidRequest):
            TB.parse_xlsx(data, 10, sheet=bad)
    with pytest.raises(W.runtime.InvalidRequest):
        TB.parse_xlsx(b"PK\x03\x04junk", 10)


def test_json_records_and_format_sniffing():
    out = TB.parse_json('[{"a": 1, "b": "x"}, {"a": 2, "c": null}]', max_rows=10)
    assert out["columns"] == ["a", "b", "c"] and out["rows"][1] == {"a": 2, "b": None, "c": None} and out["total_rows"] == 2
    assert TB.parse_json('{"data": [{"k": "1,000"}]}', 10)["rows"] == [{"k": 1000}]
    for bad in ("{}", "[1, 2]", "not json"):
        with pytest.raises(W.runtime.InvalidRequest):
            TB.parse_json(bad, 10)
    assert TB.sniff_format(b"PK\x03\x04", "application/octet-stream", None) == "xlsx"
    assert TB.sniff_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "", "old.xls") == "xls"
    assert TB.sniff_format(b"[{}]", "application/json", None) == "json"
    assert TB.sniff_format(b"a\tb\n1\t2", "text/tab-separated-values", None) == "tsv"
    assert TB.sniff_format(b"<!DOCTYPE html><html>", "text/html", None) == "html"
    assert TB.sniff_format(b"a,b\n1,2", "text/csv", "x.csv") == "csv"
    assert TB.filename_from({"content-disposition": "attachment; filename*=UTF-8''%EC%9D%B8%EA%B5%AC.csv"}, "https://x/y.bin") == "인구.csv"
    assert TB.filename_from({"content-disposition": 'attachment; filename="a b.csv"'}, "https://x/y.bin") == "a b.csv"
    assert TB.filename_from({}, "https://x/path/file%20name.xlsx") == "file name.xlsx"


# --- the skill ----------------------------------------------------------------

class _Ctx:
    def __init__(self, fetched, resolved=None):
        self.fetched, self.resolved, self.steps = fetched, resolved, []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def resolve_download(self, dataset_id):
                outer.steps.append(("resolve", dataset_id))
                return W.runtime.ProviderResult(value=outer.resolved, cost_micros=0, cost_measured=True)

            async def fetch(self, url):
                outer.steps.append(("fetch", url))
                return W.runtime.ProviderResult(value=outer.fetched, cost_micros=0, cost_measured=True)

        return (await call(_P())).value


def _fetched(data, content_type="application/octet-stream; charset=UTF-8", filename="울산광역시_인구 현황(20251112).csv", url="https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_1&fileDetailSn=1&insertDataPrcus=N"):
    charset = "UTF-8" if "charset=UTF-8" in content_type else None
    return {"data": data, "final_url": url, "content_type": content_type.split(";")[0], "charset": charset, "filename": filename, "bytes": len(data)}


def test_a_korean_dataset_page_is_resolved_then_read_as_typed_rows_in_cp949():
    ctx = _Ctx(_fetched(KR_CSV_TEXT.encode("cp949")), resolved={"url": "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_1&fileDetailSn=1&insertDataPrcus=N", "title": "울산광역시_인구 현황_20251112"})
    result = _run(SK.table(ctx, {"url": "https://www.data.go.kr/data/15005995/fileData.do", "max_rows": 1}))
    assert ctx.steps[0] == "resolve:data.go.kr" and ctx.steps[1] == ("resolve", "15005995")
    assert result["resolved_from"] == "https://www.data.go.kr/data/15005995/fileData.do" and result["file_url"].startswith("https://www.data.go.kr/cmm/")
    assert result["format"] == "csv" and result["encoding"] == "cp949" and result["columns"][0] == "행정구역코드"
    assert result["rows"] == [{"행정구역코드": 31, "행정구역명": "울산광역시", "성별": "남자", "내국인": 564888, "순이동": -1655}]
    assert result["row_count"] == 1 and result["total_rows"] == 2 and result["truncated"] is True
    assert any("2 data rows" in n for n in result["notes"]) and result["notes"][0].startswith("data.go.kr dataset:")
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_direct_xlsx_link_reads_the_chosen_sheet_and_a_web_page_is_refused():
    data = _xlsx({"A": [["x"], [1]], "B": [["y"], ["two"]]})
    ctx = _Ctx(_fetched(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "17-12-01-1.xlsx",
                        "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031669224&fileKind=0"))
    result = _run(SK.table(ctx, {"url": "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031669224&fileKind=0", "sheet": "B"}))
    assert result["resolved_from"] is None and result["format"] == "xlsx" and result["encoding"] is None
    assert result["sheet"] == "B" and result["sheets"] == ["A", "B"] and result["rows"] == [{"y": "two"}]
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])
    with pytest.raises(W.runtime.InvalidRequest):
        _run(SK.table(_Ctx(_fetched(b"<!DOCTYPE html><html><body>portal</body></html>", "text/html", "index.html", "https://example.com/")),
                      {"url": "https://example.com/"}))
    with pytest.raises(W.runtime.InvalidRequest):
        _run(SK.table(_Ctx(_fetched(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16, "application/vnd.ms-excel", "old.xls", "https://example.com/old.xls")),
                      {"url": "https://example.com/old.xls"}))


def test_bad_table_input_is_refused_before_the_gate():
    assert SK.parse({"url": "https://example.com/a.csv"})["max_rows"] == 200
    for bad in ({}, {"url": "ftp://x/y"}, {"url": "http://169.254.169.254/x.csv"}, {"url": "https://example.com/a.csv", "max_rows": 0},
                {"url": "https://example.com/a.csv", "max_rows": 2001}, {"url": "https://example.com/a.csv", "sheet": ""},
                {"url": "https://example.com/a.csv", "sheet": True}, {"url": "https://example.com/a.csv", "encoding": "nope-8"},
                {"url": "https://example.com/a.csv", "delimiter": ",,"}):
        with pytest.raises(W.runtime.InvalidRequest):
            SK.parse(bad)


# --- the route, the tool, the contract ---------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_regional", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_the_row_is_priced_keyless_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.10 and w.tier == "utility" and w.available()
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None
    search = W.catalog.BY_NAME["opendata.search"]
    assert "kr" in search.input_schema["properties"]["region"]["enum"] and len(search.description) <= 500


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"url": "http://10.0.0.1/x.csv"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        ctx2 = _Ctx(_fetched(KR_CSV_TEXT.encode("cp949")), resolved={"url": "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_1&fileDetailSn=1&insertDataPrcus=N", "title": "t"})
        return await W.skills.opendata_table.table(ctx2, payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"url": "https://www.data.go.kr/data/15005995/fileData.do", "max_rows": 2}
    assert client.post(PATH, json=body).status_code == 402
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.10 and envelope["result"]["row_count"] == 2
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["encoding"] == "cp949"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.10}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    search = next(t for t in static["tools"] if t["name"] == "hubvibe_opendata_search")
    assert "kr" in search["inputSchema"]["properties"]["region"]["enum"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
