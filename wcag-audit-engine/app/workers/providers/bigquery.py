"""BigQuery over its REST API -- the data provider behind the analysis worker.

Connected, not rebuilt: BigQuery does the querying. This adapter's only jobs
are (a) never letting a customer's question run up an unbounded bill, and
(b) reporting how many bytes it actually cost.

THE COST GATE, WHICH IS THE WHOLE POINT

BigQuery on-demand bills by bytes scanned, and a careless query against a
public dataset can scan terabytes. So every query is dry-run FIRST -- free,
and it returns the exact byte count -- and the real run is refused if that
exceeds the ceiling. The run also carries maximumBytesBilled, so even if the
estimate were wrong, BigQuery itself kills the job rather than billing us.
Two independent brakes, because this is the one worker that can lose real
money on a single malformed request.

Bytes are measured exactly. The price defaults to Google's published
on-demand rate, $6.25 per TiB (cloud.google.com/bigquery/pricing, read
2026-09-19; AI.FORECAST's TimesFM is billed on the same rate), charged at
list even inside the 1 TiB monthly free tier so the ledger never flatters a
margin. BQ_PRICE_PER_TIB overrides it.
"""

import hashlib
import logging
import os
import re
import time
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

log = logging.getLogger("hubvibe.workers.bigquery")

_TIMEOUT = float(os.environ.get("WORKER_BQ_TIMEOUT_SECONDS", "120"))
_MAX_GIB = float(os.environ.get("WORKER_BQ_MAX_SCAN_GIB", "20"))
_MAX_ROWS = int(os.environ.get("WORKER_BQ_MAX_ROWS", "200"))
# The model jobs (AI.FORECAST, AI.DETECT_ANOMALIES) run on Google's TimesFM,
# whose time is Google's queue, not our code: on 2026-10-02 the same
# uncached 56-series query took 28.7 s, then 72.8 s. The caller is never held
# for that: past 22 s the router hands the job back to collect (deliver later)
# while it runs on here, paid once on delivery. Only a job still running
# after this long answers "still computing" (unbilled); the identical request
# then resumes the same BigQuery job.
_RESUMABLE_WAIT = float(os.environ.get("WORKER_BQ_RESUMABLE_WAIT_SECONDS", "150"))
# A job is resumed only by the identical request within this long of its
# start, so a resumed answer is never older than this.
_JOB_REUSE_SECONDS = 600
_BYTES_PER_GIB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4

# Read-only by construction. BigQuery permissions should enforce this too, but
# a worker that accepts arbitrary customer SQL must not rely solely on IAM
# being configured correctly on every deployment.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"EXPORT|LOAD|CALL|BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)


def _price_per_tib() -> Optional[float]:
    raw = os.environ.get("BQ_PRICE_PER_TIB", "6.25")
    try:
        return float(raw)
    except ValueError:
        return None


def _cost_micros(bytes_processed: int):
    rate = _price_per_tib()
    if rate is None:
        return None, False
    return int(round((bytes_processed / _BYTES_PER_TIB) * rate * 1_000_000)), True


def _job_id(sql: str, ceiling_bytes: int, window: int) -> str:
    """The job id the identical request computes again: same SQL, same byte
    ceiling, same 10-minute window."""
    digest = hashlib.sha256(f"{sql}\n{ceiling_bytes}".encode()).hexdigest()[:40]
    return f"hubvibe_{digest}_{window}"


def validate_sql(sql: str) -> str:
    """Reject anything that is not a single read. Raises InvalidRequest, which
    the router answers BEFORE payment is read."""
    if not sql or not sql.strip():
        raise runtime.InvalidRequest("`sql` is required.")
    cleaned = sql.strip().rstrip(";")
    if ";" in cleaned:
        raise runtime.InvalidRequest("Only a single statement is allowed.")
    if _FORBIDDEN.search(cleaned):
        raise runtime.InvalidRequest(
            "This worker runs read-only SELECT queries; the statement contains a "
            "write or DDL keyword.")
    if not re.match(r"^\s*(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise runtime.InvalidRequest("Query must begin with SELECT or WITH.")
    return cleaned


class _BigQuery:
    id = "bigquery"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    def _url(self) -> str:
        return (f"https://bigquery.googleapis.com/bigquery/v2/projects/"
                f"{google_auth.project()}/queries")

    async def _post(self, body: dict) -> dict:
        return await self._request("POST", self._url(), json=body)

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"BigQuery timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"BigQuery unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"BigQuery returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = (response.json().get("error") or {}).get("message", "")
            except Exception:
                detail = response.text[:200]
            raise runtime.PermanentProviderError(f"BigQuery rejected the query: {detail}")
        return response.json()

    async def estimate(self, sql: str) -> int:
        """Bytes this query would scan. Free -- BigQuery does not bill dry runs."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        data = await self._post({"query": sql, "useLegacySql": False, "dryRun": True})
        return int(data.get("totalBytesProcessed") or 0)

    async def query_clustered(self, sql: str, max_billed_gib: float = 25.0) -> runtime.ProviderResult:
        """For a geography-clustered public table (Overture Maps): the dry-run
        estimate is the pre-pruning upper bound (several GiB) while a nearby
        query bills about 10 MB, so there is no estimate refusal here;
        maximumBytesBilled stays the hard ceiling BigQuery enforces."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        return await self._run(sql, int(max_billed_gib * _BYTES_PER_GIB), 0)

    async def query(self, sql: str, max_gib: Optional[float] = None) -> runtime.ProviderResult:
        """Estimate, refuse if too large, then run under a hard byte ceiling."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        ceiling_gib = max_gib if max_gib is not None else _MAX_GIB
        ceiling_bytes = int(ceiling_gib * _BYTES_PER_GIB)

        estimated = await self.estimate(sql)
        if estimated > ceiling_bytes:
            # InvalidRequest, not a provider failure: the query is the problem
            # and no retry or fallback can help. The caller is told the real
            # number so it can narrow the query itself.
            raise runtime.InvalidRequest(
                f"Query would scan {estimated / _BYTES_PER_GIB:.2f} GiB, over this "
                f"worker's {ceiling_gib:.0f} GiB limit. Narrow it (fewer columns, "
                f"a partition filter, or a LIMIT on a subquery).")

        return await self._run(sql, ceiling_bytes, estimated)

    async def _run(self, sql: str, ceiling_bytes: int, estimated: int) -> runtime.ProviderResult:
        data = await self._post({
            "query": sql,
            "useLegacySql": False,
            "maximumBytesBilled": str(ceiling_bytes),
            "maxResults": _MAX_ROWS,
            "timeoutMs": int(_TIMEOUT * 1000),
        })

        # jobs.query answers jobComplete:false -- with no rows and no error --
        # when the job outlives timeoutMs. Unchecked, that reads as a successful
        # empty result and the caller is billed for "no data" when the truth is
        # "we stopped waiting". Raising means the gate never settles.
        if data.get("jobComplete") is False:
            raise runtime.TransientProviderError(
                f"BigQuery did not finish within {_TIMEOUT:.0f}s; no rows came back.",
                reason="provider_timeout")

        schema = [f.get("name") for f in (data.get("schema") or {}).get("fields", [])]
        rows = []
        for row in (data.get("rows") or [])[:_MAX_ROWS]:
            values = [cell.get("v") for cell in row.get("f", [])]
            rows.append(dict(zip(schema, values)) if schema else values)

        billed = int(data.get("totalBytesProcessed") or estimated or 0)
        cost, measured = _cost_micros(billed)

        return runtime.ProviderResult(
            value={
                "columns": schema,
                "rows": rows,
                "row_count": len(rows),
                "total_rows": int(data.get("totalRows") or len(rows)),
                "truncated": int(data.get("totalRows") or 0) > len(rows),
                "bytes_processed": billed,
                "gib_processed": round(billed / _BYTES_PER_GIB, 4),
                "cache_hit": bool(data.get("cacheHit")),
            },
            cost_micros=cost, cost_measured=measured,
            usage=f"bytes={billed}")

    async def query_resumable(self, sql: str, max_gib: Optional[float] = None,
                              wait_seconds: Optional[float] = None) -> runtime.ProviderResult:
        """For the long model jobs: a BigQuery job whose id is derived from
        the query, waited on for at most `wait_seconds`.

        Still running then -> StillComputing (the router answers 503 with
        Retry-After, nothing billed). The identical request sent again finds
        that same job -- started in this 10-minute window or the previous
        one, at most _JOB_REUSE_SECONDS ago -- and collects its result
        instead of paying for a second run.
        """
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        ceiling_gib = max_gib if max_gib is not None else _MAX_GIB
        ceiling_bytes = int(ceiling_gib * _BYTES_PER_GIB)
        dry = await self._post({"query": sql, "useLegacySql": False, "dryRun": True})
        estimated = int(dry.get("totalBytesProcessed") or 0)
        if estimated > ceiling_bytes:
            raise runtime.InvalidRequest(
                f"Query would scan {estimated / _BYTES_PER_GIB:.2f} GiB, over this "
                f"worker's {ceiling_gib:.0f} GiB limit. Narrow it (fewer columns, "
                f"a partition filter, or a LIMIT on a subquery).")
        location = (dry.get("jobReference") or {}).get("location") or dry.get("location")

        project = google_auth.project()
        base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}"
        located = {"location": location} if location else {}
        now = time.time()
        window = int(now // _JOB_REUSE_SECONDS)

        job_id = None
        previous = _job_id(sql, ceiling_bytes, window - 1)
        try:
            job = await self._request("GET", f"{base}/jobs/{previous}", params=located)
            started = int((job.get("statistics") or {}).get("creationTime") or 0) / 1000
            if now - started < _JOB_REUSE_SECONDS:
                job_id = previous
        except runtime.PermanentProviderError:
            pass  # no such job: the usual case
        if job_id is None:
            job_id = _job_id(sql, ceiling_bytes, window)
            reference = {"projectId": project, "jobId": job_id}
            if location:
                reference["location"] = location
            try:
                await self._request("POST", f"{base}/jobs", json={
                    "jobReference": reference,
                    "configuration": {"query": {
                        "query": sql, "useLegacySql": False,
                        "maximumBytesBilled": str(ceiling_bytes)}}})
            except runtime.PermanentProviderError as exc:
                # The identical request already started it this window.
                if "already exists" not in str(exc).lower():
                    raise

        wait = _RESUMABLE_WAIT if wait_seconds is None else float(wait_seconds)
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            data = await self._request("GET", f"{base}/queries/{job_id}", params={
                **located, "maxResults": _MAX_ROWS,
                "timeoutMs": max(1, int(remaining * 1000))})
            if data.get("jobComplete") is not False:
                break
            # BigQuery may answer before timeoutMs; keep waiting until ours.
            if deadline - time.monotonic() <= 0.5:
                raise runtime.StillComputing(
                    f"BigQuery is still running this model job (job {job_id}). Nothing "
                    f"was charged. Send the identical request again in about 20 seconds: "
                    f"it resumes this same job and answers as soon as it finishes.",
                    retry_after=20)

        schema = [f.get("name") for f in (data.get("schema") or {}).get("fields", [])]
        rows = []
        for row in (data.get("rows") or [])[:_MAX_ROWS]:
            values = [cell.get("v") for cell in row.get("f", [])]
            rows.append(dict(zip(schema, values)) if schema else values)
        billed = int(data.get("totalBytesProcessed") or estimated or 0)
        cost, measured = _cost_micros(billed)
        return runtime.ProviderResult(
            value={
                "columns": schema,
                "rows": rows,
                "row_count": len(rows),
                "total_rows": int(data.get("totalRows") or len(rows)),
                "truncated": int(data.get("totalRows") or 0) > len(rows),
                "bytes_processed": billed,
                "gib_processed": round(billed / _BYTES_PER_GIB, 4),
                "cache_hit": bool(data.get("cacheHit")),
            },
            cost_micros=cost, cost_measured=measured,
            usage=f"bytes={billed}")

    async def rows(self, sql: str, max_rows: int) -> runtime.ProviderResult:
        """Like query(), but returns up to `max_rows` rows as plain value
        lists, following pageToken across result pages. For the workers that
        compute over a column rather than show a handful of rows."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        ceiling_bytes = int(_MAX_GIB * _BYTES_PER_GIB)
        estimated = await self.estimate(sql)
        if estimated > ceiling_bytes:
            raise runtime.InvalidRequest(
                f"Query would scan {estimated / _BYTES_PER_GIB:.2f} GiB, over this "
                f"worker's {_MAX_GIB:.0f} GiB limit. Use a smaller table or view.")

        page_size = min(int(max_rows), 50_000)
        data = await self._post({
            "query": sql,
            "useLegacySql": False,
            "maximumBytesBilled": str(ceiling_bytes),
            "maxResults": page_size,
            "timeoutMs": int(_TIMEOUT * 1000),
        })
        if data.get("jobComplete") is False:
            raise runtime.TransientProviderError(
                f"BigQuery did not finish within {_TIMEOUT:.0f}s; no rows came back.",
                reason="provider_timeout")

        def cells(page: dict) -> list:
            return [[cell.get("v") for cell in row.get("f", [])]
                    for row in (page.get("rows") or [])]

        rows = cells(data)
        job = data.get("jobReference") or {}
        token = data.get("pageToken")
        while token and len(rows) < max_rows and job.get("jobId"):
            params = {"pageToken": token, "maxResults": page_size,
                      "timeoutMs": int(_TIMEOUT * 1000)}
            if job.get("location"):
                params["location"] = job["location"]
            page = await self._request(
                "GET",
                f"https://bigquery.googleapis.com/bigquery/v2/projects/"
                f"{job.get('projectId') or google_auth.project()}/queries/{job['jobId']}",
                params=params)
            rows.extend(cells(page))
            token = page.get("pageToken")
        rows = rows[:max_rows]

        billed = int(data.get("totalBytesProcessed") or estimated or 0)
        cost, measured = _cost_micros(billed)
        return runtime.ProviderResult(
            value={
                "rows": rows,
                "row_count": len(rows),
                "total_rows": int(data.get("totalRows") or len(rows)),
                "bytes_processed": billed,
                "gib_processed": round(billed / _BYTES_PER_GIB, 4),
                "cache_hit": bool(data.get("cacheHit")),
            },
            cost_micros=cost, cost_measured=measured,
            usage=f"bytes={billed}")


PROVIDERS = [_BigQuery()]
PROVIDER = PROVIDERS[0]
