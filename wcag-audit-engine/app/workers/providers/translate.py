"""Google Cloud Translation v3 with the Translation LLM model: the primary
engine for the no-language-barrier layer (skills/localize.py).

A list of strings in, the same number of translated strings out, in order,
source language detected. The LLM model, not the standard NMT one, on
evidence (2026-09-27, same strings, ja/ko/pt): NMT turned "Title is 72
characters (recommended <= 60)" into "the title must be 72 characters" in
all three; the LLM model and Gemini kept the meaning. The LLM model
accepted all 49 target languages tested. Auth is the node's Google service
account (roles/cloudtranslate.user on resolver-time); fail-closed without a
Google credential; any refusal falls back to Gemini.
Pricing: $10 per million characters in plus $10 per million out.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

_TIMEOUT = float(os.environ.get("WORKER_TRANSLATE_TIMEOUT_SECONDS", "30"))
BASE = os.environ.get("WORKER_TRANSLATE_API_BASE", "https://translation.googleapis.com/v3")
USD_PER_CHAR = float(os.environ.get("WORKER_TRANSLATE_USD_PER_CHAR", "0.00001"))  # each of input and output
LOCATION = os.environ.get("WORKER_TRANSLATE_LOCATION", "us-central1")
MODEL = os.environ.get("WORKER_TRANSLATE_MODEL", "general/translation-llm")
MAX_STRINGS = 1024
MAX_CHARS = 30000

# BCP-47 script tags the general model spells by region instead.
_ALIASES = {"zh-hant": "zh-TW", "zh-hans": "zh-CN", "zh-hk": "zh-TW", "zh-mo": "zh-TW", "zh-sg": "zh-CN",
            "pt-br": "pt", "iw": "he", "jw": "jv", "tl": "fil", "no": "no", "nb": "no"}


def target_code(language: str) -> str:
    lang = language.strip()
    return _ALIASES.get(lang.lower(), lang)


class _CloudTranslate:
    id = "google-translate-llm"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return "" if self.available() else google_auth.unavailable_reason()

    async def translate(self, strings: list, language: str) -> runtime.ProviderResult:
        if not strings:
            return runtime.ProviderResult(value=[], cost_micros=0, cost_measured=True, usage="strings=0")
        if len(strings) > MAX_STRINGS or sum(len(s) for s in strings) > MAX_CHARS:
            raise runtime.PermanentProviderError("Batch over the Cloud Translation request limit.")
        project = google_auth.project()
        body = {"contents": strings, "targetLanguageCode": target_code(language), "mimeType": "text/plain",
                "model": f"projects/{project}/locations/{LOCATION}/models/{MODEL}"}
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(f"{BASE}/projects/{project}/locations/{LOCATION}:translateText",
                                             json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Cloud Translation timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Cloud Translation unreachable: {exc}") from exc
        except RuntimeError as exc:  # no credential after all
            raise runtime.ProviderUnavailable(str(exc)) from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        message = ((data.get("error") or {}).get("message") if isinstance(data, dict) else None) or f"HTTP {response.status_code}"
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"Cloud Translation returned {response.status_code}: {message}")
        if response.status_code in (401, 403):
            raise runtime.ProviderUnavailable(f"Cloud Translation refused this deployment ({response.status_code}): {message}")
        if response.status_code >= 400:
            # An unsupported target language is a 400: another engine may still handle it.
            raise runtime.PermanentProviderError(f"Cloud Translation returned {response.status_code}: {message}")
        translations = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(translations, list) or len(translations) != len(strings):
            raise runtime.InvalidProviderResponse("Cloud Translation returned a different number of strings.")
        out = [t.get("translatedText") if isinstance(t, dict) else None for t in translations]
        if not all(isinstance(t, str) for t in out):
            raise runtime.InvalidProviderResponse("Cloud Translation returned a non-text translation.")
        chars_in = sum(len(s) for s in strings)
        chars_out = sum(len(t) for t in out)
        return runtime.ProviderResult(value=out, cost_micros=int(round((chars_in + chars_out) * USD_PER_CHAR * 1_000_000)),
                                      cost_measured=True, usage=f"strings={len(strings)} chars_in={chars_in} chars_out={chars_out}")


PROVIDERS = [_CloudTranslate()]
