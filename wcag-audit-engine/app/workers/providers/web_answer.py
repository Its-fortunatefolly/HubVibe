"""Web search answered from Brave's independent index.

Brave returns the live results (its own index, not a Google or Bing proxy);
Gemini on Vertex then writes the answer from those results alone, and every
result it was given is returned as a source. One Brave request ($0.005) and
one short model call per search, with the model's thinking step switched
off: measured 2026-09-28, that is 1.4-2.0 s for the answer instead of 3-5 s,
with the same answers.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from . import brave, gemini, google_auth

RESULTS = int(os.environ.get("WORKER_WEB_ANSWER_RESULTS", "10"))
MAX_ANSWER_TOKENS = int(os.environ.get("WORKER_WEB_ANSWER_MAX_TOKENS", "700"))
_SYSTEM = ("You answer a web search from the numbered search results you are given, and from nothing else. "
           "Be concise and factual: at most about 150 words unless the query asks for a list. State names, numbers and dates exactly as the results give them. "
           "When results disagree, say so. When the results do not contain the answer, say that plainly "
           "instead of answering from memory. Cite results inline as [n].")


def _snippet(r: dict) -> str:
    parts = [r.get("description") or ""] + [s for s in (r.get("extra_snippets") or []) if isinstance(s, str)]
    text = " ".join(p.strip() for p in parts if p and p.strip())
    return (text[:900] + "...") if len(text) > 900 else text


def results_block(rows: list) -> str:
    lines = []
    for i, r in enumerate(rows, 1):
        when = r.get("page_age") or r.get("age") or ""
        lines.append(f"[{i}] {r.get('title') or ''} ({r.get('url')}){' - ' + when if when else ''}\n{_snippet(r)}")
    return "\n\n".join(lines)


def prompt_for(query: str, rows: list, language: Optional[str] = None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    prompt = f"Today is {today}. Search query: {query}\n\nSearch results:\n\n{results_block(rows)}\n\nAnswer the query."
    if language:
        prompt += (f" Write the answer in the language with BCP-47 tag '{language}'; "
                   "keep names, quotations and numbers exactly as found.")
    return prompt


def _no_thinking(model: str) -> dict:
    # Gemini 3 models take thinkingLevel; earlier ones take thinkingBudget.
    return {"thinkingLevel": "minimal"} if model.startswith("gemini-3") else {"thinkingBudget": 0}


async def write(prompt: str) -> runtime.ProviderResult:
    """The answer from the first configured model that gives one: without
    its thinking step, or with it when a model refuses the setting."""
    last = None
    for model in gemini.PROVIDERS:
        for thinking in (_no_thinking(model.model), None):
            try:
                return await model.generate(prompt, system=_SYSTEM, temperature=0.1, thinking=thinking,
                                            max_output_tokens=MAX_ANSWER_TOKENS)
            except runtime.PermanentProviderError as exc:
                last = exc
                if thinking is None or "think" not in str(exc).lower():
                    break
            except (runtime.TransientProviderError, runtime.InvalidProviderResponse) as exc:
                last = exc
                break
    if last is None:
        raise runtime.ProviderUnavailable("No Gemini model is configured to write the answer.")
    raise last


class _BraveAnswer:
    id = "brave-search+vertex"

    def available(self) -> bool:
        return brave.PROVIDERS[0].available() and google_auth.configured()

    def unavailable_reason(self) -> str:
        return brave.PROVIDERS[0].unavailable_reason() or google_auth.unavailable_reason()

    async def search(self, query: str, language: Optional[str] = None) -> runtime.ProviderResult:
        found = await brave.PROVIDERS[0].web(query, count=RESULTS, language=language)
        rows = [r for r in (found.value["web"] + found.value["news"]) if r.get("url")][:RESULTS]
        if not rows:
            raise runtime.InvalidRequest("Brave found no web results for this query. Nothing was charged.")
        written = await write(prompt_for(query, rows, language))
        return runtime.ProviderResult(
            value={"answer": written.value["text"], "model": written.value["model"],
                   "sources": [{"url": r["url"], "title": r.get("title")} for r in rows],
                   "queries": [query]},
            cost_micros=found.cost_micros + written.cost_micros,
            cost_measured=found.cost_measured and written.cost_measured,
            usage=f"{found.usage} {written.usage}")


PROVIDERS = [_BraveAnswer()]
