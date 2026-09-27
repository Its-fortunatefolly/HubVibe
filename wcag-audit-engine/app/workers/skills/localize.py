"""No language barrier: every bee answers in the caller's language.

The prose-producing bees (llm.*, research.*, search.web, verify.claims,
monitor.check, market.intel, data.question, commerce.availability) write
their answer in `language` natively. Every other bee returns data plus a
few human-readable strings -- notes, details, reasons, summaries -- and
those are translated here, after the skill has run and before the delivery
contract is checked, in one batched model call whose cost is measured on
the same job. Data is never touched: only strings under the prose keys,
and never URLs, ids, dates or numbers.

Engine: Google Cloud Translation's LLM model first (purpose-built, same
count out as in); Gemini in JSON mode when Translation is unavailable or
refuses the target language. The standard NMT model is not used: on the
node's own finding strings it changed meaning (see providers/translate.py).
"""

import copy
import json
import re
from typing import Optional

from .. import runtime
from ..providers import gemini
from ..providers import translate as cloud_translate

# Keys whose string values (or lists of strings) are prose for a human.
PROSE_KEYS = frozenset({"notes", "note", "detail", "details", "summary", "message", "reason", "reasons",
                        "advice", "recommendation", "explanation", "warnings", "caveats", "news_note",
                        "eligibility_notes", "disclaimer"})
MAX_CHARS_PER_CALL = 12000
MAX_TOTAL_CHARS = 48000
_SKIP = re.compile(r"^(https?://\S+|[\w.-]+@[\w.-]+|[-+]?\d[\d,.:/\-T Z]*|0x[0-9a-fA-F]+|[A-Za-z0-9_\-]{1,3})$")
_SYSTEM = ("You are a precise translator for a data API. Translate each string in `strings` into the language "
           "with BCP-47 tag {language}. Keep numbers, units, currency codes, dates, URLs, product names, codes and "
           "quoted identifiers exactly as they are. Do not add, drop, merge or reorder items. Answer ONLY with JSON: "
           "{{\"translations\": [...]}} with exactly {n} strings in the same order.")


def _is_prose(value) -> bool:
    return isinstance(value, str) and len(value.strip()) > 3 and not _SKIP.match(value.strip())


def collect(result, path=(), keys=PROSE_KEYS):
    """(path, string) pairs for every prose string under a prose key."""
    found = []
    if isinstance(result, dict):
        for key, value in result.items():
            here = path + (key,)
            if key in keys:
                if _is_prose(value):
                    found.append((here, value))
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        if _is_prose(item):
                            found.append((here + (i,), item))
            elif isinstance(value, (dict, list)):
                found.extend(collect(value, here, keys))
    elif isinstance(result, list):
        for i, item in enumerate(result):
            if isinstance(item, (dict, list)):
                found.extend(collect(item, path + (i,), keys))
    return found


def _set(result, path, value):
    target = result
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value


def chunks(strings: list, limit: int = MAX_CHARS_PER_CALL) -> list:
    out, cur, size = [], [], 0
    for s in strings:
        if cur and size + len(s) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s)
    if cur:
        out.append(cur)
    return out


async def translate_strings(ctx, strings: list, language: str) -> list:
    """Translate `strings` in order: Cloud Translation when it can, the model otherwise."""
    out = []
    for batch in chunks(strings):
        try:
            async def mt(provider):
                return await provider.translate(batch, language)
            out.extend(await ctx.run("translate", cloud_translate.PROVIDERS, mt, per_attempt_seconds=30, max_attempts=2))
            continue
        except runtime.WorkerError:
            pass
        out.extend(await _model_translate(ctx, batch, language))
    return out


async def _model_translate(ctx, strings: list, language: str) -> list:
    """Translate with the model in JSON mode (the fallback engine)."""
    out = []
    for batch in chunks(strings):
        prompt = json.dumps({"strings": batch}, ensure_ascii=False)
        system = _SYSTEM.format(language=language, n=len(batch))

        async def call(provider):
            return await provider.generate_json(prompt, system=system, temperature=0.0)
        value = await ctx.run("localize", gemini.PROVIDERS, call, per_attempt_seconds=45, max_attempts=2)
        data = value.get("json") if isinstance(value, dict) else None
        translations = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(translations, list) or len(translations) != len(batch) or not all(isinstance(t, str) for t in translations):
            raise runtime.InvalidProviderResponse(
                f"The translation step returned {len(translations) if isinstance(translations, list) else 'no'} strings for {len(batch)}.")
        out.extend(translations)
    return out


async def apply(ctx, result: dict, language: Optional[str], extra_keys=()) -> dict:
    """The result with its prose strings translated; unchanged when there is
    nothing to translate or no language was asked for. `extra_keys` adds
    prose keys for a caller whose results name them differently (the audits'
    `help`)."""
    if not language or not isinstance(result, dict):
        return result
    found = collect(result, keys=PROSE_KEYS | frozenset(extra_keys))
    if not found:
        return result
    total = 0
    kept = []
    for path, text in found:
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        kept.append((path, text))
        total += len(text)
    translated = await translate_strings(ctx, [t for _, t in kept], language)
    out = copy.deepcopy(result)
    for (path, _), text in zip(kept, translated):
        _set(out, path, text)
    return out
