"""Image generation via Imagen 4 on Vertex.

Connected, not rebuilt: this speaks the same `:predict` REST surface the
Vertex SDKs call, with the same ADC this package already resolves for
Gemini and BigQuery.

COST: unlike token-priced inference, Imagen bills a FLAT rate per image at a
given tier -- there is no usage number to measure per call, the rate itself
is the fact. Default matches Google's published Imagen 4 standard-tier price
(verified 2026-09-16); overridable because that price is Google's to change.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

DEFAULT_REGION = os.environ.get("WORKER_VERTEX_REGION", "us-central1")
_MODEL = os.environ.get("WORKER_IMAGEN_MODEL", "imagen-4.0-generate-001")
_TIMEOUT = float(os.environ.get("WORKER_IMAGEN_TIMEOUT_SECONDS", "90"))
_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16"}


def _price_per_image() -> Optional[float]:
    raw = os.environ.get("WORKER_IMAGEN_PRICE_USD", "0.04")
    try:
        return float(raw)
    except ValueError:
        return None


class _Imagen:
    id = f"vertex:{_MODEL}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, aspect_ratio: str = "1:1") -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        if aspect_ratio not in _ASPECT_RATIOS:
            raise runtime.InvalidRequest(
                f"`aspect_ratio` must be one of {sorted(_ASPECT_RATIOS)}.")

        project = google_auth.project()
        url = (f"https://{DEFAULT_REGION}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{DEFAULT_REGION}/publishers/google/models/{_MODEL}:predict")
        body = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio,
                           "personGeneration": "allow_adult"},
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Imagen timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Imagen unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Imagen returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            # A prompt Imagen's own safety filter refuses is the CALLER's
            # input being wrong for this provider, not a bug worth retrying.
            raise runtime.PermanentProviderError(
                f"Imagen rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        predictions = data.get("predictions") or []
        if not predictions or not predictions[0].get("bytesBase64Encoded"):
            raise runtime.InvalidProviderResponse(
                "Imagen returned no image (commonly a safety filter with no error body).")
        image = predictions[0]

        rate = _price_per_image()
        cost = int(round(rate * 1_000_000)) if rate is not None else None
        return runtime.ProviderResult(
            value={"image_base64": image["bytesBase64Encoded"],
                   "mime_type": image.get("mimeType", "image/png"), "model": _MODEL},
            cost_micros=cost, cost_measured=rate is not None, usage="images=1")


PROVIDERS = [_Imagen()]
