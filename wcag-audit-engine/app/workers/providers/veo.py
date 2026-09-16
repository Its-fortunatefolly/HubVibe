"""Video generation via Veo on Vertex -- predictLongRunning, polled with
fetchPredictOperation until the operation completes.

FAILS CLOSED PAST CREDENTIALS, DELIBERATELY. Every other Google-backed
provider in this package treats `google_auth.configured()` as sufficient to
advertise (Vertex/BigQuery/Imagen/TTS/STT all share one Cloud Platform
enablement). Veo does not: the exact model resource name on Vertex has
moved between `-preview` and stable suffixes during this model family's
rollout, and getting that string wrong here would mean a caller pays,
waits through a multi-minute poll loop, and then reads an opaque 502 --
the worst outcome this package's whole design exists to prevent (see
catalog.py's Worker.available() docstring). So this worker also requires
WORKER_VEO_ENABLED=1, an explicit opt-in the operator sets only after
confirming (`GET .../publishers/google/models/{model}` -- a free metadata
read, no generation) that the configured model id actually resolves on
this project. Until then it reports exactly why it is off rather than
silently misbehaving on a real paid call.
"""

import asyncio
import os
import time
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

DEFAULT_REGION = os.environ.get("WORKER_VERTEX_REGION", "us-central1")
_MODEL = os.environ.get("WORKER_VEO_MODEL", "veo-3.1-generate-preview")
_ENABLED = os.environ.get("WORKER_VEO_ENABLED") == "1"
_TIMEOUT = float(os.environ.get("WORKER_VEO_TIMEOUT_SECONDS", "30"))
_POLL_INTERVAL = float(os.environ.get("WORKER_VEO_POLL_SECONDS", "8"))
_ASPECT_RATIOS = {"16:9", "9:16"}
_DURATIONS = {4, 6, 8}


def _price_per_second() -> Optional[float]:
    raw = os.environ.get("WORKER_VEO_PRICE_PER_SECOND_USD")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class _Veo:
    id = f"vertex:{_MODEL}"

    def available(self) -> bool:
        return _ENABLED and google_auth.configured()

    def unavailable_reason(self) -> str:
        if not google_auth.configured():
            return google_auth.unavailable_reason()
        return (f"video generation is disabled on this deployment pending verification "
               f"that {_MODEL!r} resolves on this project; set WORKER_VEO_ENABLED=1 "
               "once confirmed (see this module's docstring)")

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Veo timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Veo unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Veo returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Veo rejected the request ({response.status_code}): {response.text[:200]}")
        return response.json()

    async def generate(self, prompt: str, aspect_ratio: str = "16:9",
                       duration_seconds: int = 6, generate_audio: bool = False,
                       deadline_seconds: float = 200) -> runtime.ProviderResult:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        if aspect_ratio not in _ASPECT_RATIOS:
            raise runtime.InvalidRequest(f"`aspect_ratio` must be one of {sorted(_ASPECT_RATIOS)}.")
        if duration_seconds not in _DURATIONS:
            raise runtime.InvalidRequest(f"`duration_seconds` must be one of {sorted(_DURATIONS)}.")

        project = google_auth.project()
        base = (f"https://{DEFAULT_REGION}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{DEFAULT_REGION}/publishers/google/models/{_MODEL}")
        headers = await google_auth.headers()

        started = await self._post(f"{base}:predictLongRunning", headers, {
            "instances": [{"prompt": prompt}],
            "parameters": {"aspectRatio": aspect_ratio, "durationSeconds": duration_seconds,
                           "sampleCount": 1, "generateAudio": generate_audio},
        })
        operation_name = started.get("name")
        if not operation_name:
            raise runtime.InvalidProviderResponse(
                "Veo did not return an operation to poll for.")

        deadline = time.monotonic() + deadline_seconds
        operation = None
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            operation = await self._post(f"{base}:fetchPredictOperation", headers,
                                         {"operationName": operation_name})
            if operation.get("done"):
                break
        else:
            raise runtime.DeadlineExceeded(
                f"Veo had not finished generating after {deadline_seconds:.0f}s.")

        if operation is None or not operation.get("done"):
            raise runtime.DeadlineExceeded("Veo did not finish within this job's time budget.")
        if "error" in operation and operation["error"]:
            raise runtime.PermanentProviderError(
                f"Veo generation failed: {operation['error'].get('message', 'unknown error')}")

        payload = operation.get("response") or {}
        videos = payload.get("videos") or payload.get("predictions") or []
        if not videos or not isinstance(videos, list):
            raise runtime.InvalidProviderResponse(
                f"Veo finished but returned no video (response keys: {sorted(payload.keys())}).")
        video = videos[0]
        video_b64 = video.get("bytesBase64Encoded") or video.get("bytesBase64")
        gcs_uri = video.get("gcsUri")
        if not video_b64 and not gcs_uri:
            raise runtime.InvalidProviderResponse(
                f"Veo's video entry had neither inline bytes nor a GCS uri (keys: "
                f"{sorted(video.keys())}).")

        rate = _price_per_second()
        cost = int(round(rate * duration_seconds * 1_000_000)) if rate is not None else None
        return runtime.ProviderResult(
            value={"video_base64": video_b64, "gcs_uri": gcs_uri,
                   "mime_type": video.get("mimeType", "video/mp4"), "model": _MODEL,
                   "duration_seconds": duration_seconds},
            cost_micros=cost, cost_measured=rate is not None,
            usage=f"duration={duration_seconds}s")


PROVIDERS = [_Veo()]
