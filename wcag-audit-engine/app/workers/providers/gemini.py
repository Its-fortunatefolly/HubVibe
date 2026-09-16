"""Gemini on Vertex AI -- the reasoning provider behind every analysis worker.

Connected, not rebuilt: this speaks the same REST surface the Vertex SDKs
call (`:generateContent`), with ADC for auth.

COST HONESTY

Token USAGE is measured exactly, from the response's own usageMetadata. Token
PRICE is not asserted: a published rate this code has not verified would turn
into a fabricated margin the moment it changed. So the rate comes from
configuration (GEMINI_PRICE_PER_MTOK_IN / _OUT), and when it is unset the
attempt is recorded with real usage and cost_measured=False -- the ledger then
reports that worker's margin as unknown rather than as pure profit.
"""

import json
import logging
import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

log = logging.getLogger("hubvibe.workers.gemini")

DEFAULT_REGION = os.environ.get("WORKER_VERTEX_REGION", "us-central1")

# Verified callable on this project 2026-09-15 by a free :countTokens probe
# (HTTP 200). Order is the fallback order: flash first because it is the
# cheaper and faster of the two, pro second for when flash fails.
_TEXT_MODELS = os.environ.get(
    "WORKER_GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-pro"
).split(",")

_TIMEOUT = float(os.environ.get("WORKER_GEMINI_TIMEOUT_SECONDS", "120"))


def _rate(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("%s is not a number; treating Gemini cost as unmeasured", name)
        return None


def _cost_micros(prompt_tokens: int, output_tokens: int):
    """(micros, measured). Measured only when BOTH rates are configured."""
    rate_in = _rate("GEMINI_PRICE_PER_MTOK_IN")
    rate_out = _rate("GEMINI_PRICE_PER_MTOK_OUT")
    if rate_in is None or rate_out is None:
        return None, False
    usd = (prompt_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return int(round(usd * 1_000_000)), True


class _GeminiModel:
    """One model, as one provider entry, so fallback between models is the
    same machinery as fallback between vendors."""

    def __init__(self, model: str):
        self.model = model.strip()
        self.id = f"vertex:{self.model}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, system: Optional[str] = None,
                       json_output: bool = False,
                       temperature: float = 0.2) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        project = google_auth.project()
        url = (f"https://{DEFAULT_REGION}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{DEFAULT_REGION}/publishers/google/models/{self.model}"
               ":generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_output:
            body["generationConfig"]["responseMimeType"] = "application/json"

        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
                f"Vertex rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # A safety block lands here: a real answer with no content. Not
            # retryable, and emphatically not something to bill for.
            raise runtime.InvalidProviderResponse(
                f"Vertex returned no candidate ({json.dumps(data)[:200]})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise runtime.InvalidProviderResponse("Vertex returned an empty answer.")

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        cost, measured = _cost_micros(prompt_tokens, output_tokens)

        return runtime.ProviderResult(
            value={"text": text, "model": self.model,
                   "prompt_tokens": prompt_tokens, "output_tokens": output_tokens},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens}")

    async def generate_json(self, prompt: str, system: Optional[str] = None,
                            temperature: float = 0.2) -> runtime.ProviderResult:
        """Same call, but the answer must parse as JSON.

        Worth its own method because a worker that promises structured output
        and returns prose is selling a shape the buyer's parser will reject --
        the failure surfaces at the customer, not here. Parsing it on this
        side turns that into a retry against a provider we already have.
        """
        result = await self.generate(prompt, system=system, json_output=True,
                                     temperature=temperature)
        text = result.value["text"]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise runtime.InvalidProviderResponse(
                    "Model did not return JSON.") from None
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise runtime.InvalidProviderResponse(
                    f"Model returned malformed JSON: {exc}") from exc
        result.value = {**result.value, "json": parsed}
        return result


PROVIDERS = [_GeminiModel(m) for m in _TEXT_MODELS if m.strip()]


def primary():
    return PROVIDERS[0] if PROVIDERS else None
