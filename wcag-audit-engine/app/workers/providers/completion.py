"""Raw text completion, model chosen by the caller when given.

ONE WORKER, SEVERAL VENDORS. `llm.analyze`/`llm.extract` are a fixed
Gemini-backed shape (answer from THIS material, only from it). `llm.generate`
is the raw completion primitive underneath that, exposed directly for a
caller who wants to write their own prompt and pick a vendor.

Wave 1 ships one entry (Gemini, already live on this box). A second vendor
(Claude on Vertex, once Model Garden access is enabled) is added to
PROVIDERS the same way every other fallback provider here is added -- append
to the list, nothing else changes.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth
from .gemini import DEFAULT_REGION, _cost_micros


class _VertexGeminiCompletion:
    """Gemini, called as a raw completion rather than through the
    answer-from-material shape `llm.analyze` uses."""

    provider_name = "gemini"
    model = os.environ.get("WORKER_LLM_GENERATE_GEMINI_MODEL", "gemini-2.5-flash")
    id = f"vertex:{model}"
    _timeout = float(os.environ.get("WORKER_LLM_GENERATE_TIMEOUT_SECONDS", "60"))

    def matches(self, requested_provider: Optional[str]) -> bool:
        return requested_provider in (None, self.provider_name)

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, system: Optional[str], max_tokens: int,
                       temperature: float) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        project = google_auth.project()
        url = (f"https://{DEFAULT_REGION}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{DEFAULT_REGION}/publishers/google/models/{self.model}"
               ":generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Vertex timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Vertex unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Vertex returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Vertex rejected the request ({response.status_code}): {response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise runtime.InvalidProviderResponse("Vertex returned no candidate.")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise runtime.InvalidProviderResponse("Vertex returned an empty completion.")

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        cost, measured = _cost_micros(prompt_tokens, output_tokens)

        return runtime.ProviderResult(
            value={"text": text, "model": self.model, "provider": self.provider_name,
                   "finish_reason": (candidates[0].get("finishReason") or "").lower() or None,
                   "prompt_tokens": prompt_tokens, "output_tokens": output_tokens},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens}")


PROVIDERS = [_VertexGeminiCompletion()]
