"""Inference workers backed by Gemini on Vertex."""

from .. import runtime
from ..providers import gemini

MAX_INPUT_CHARS = 200_000

_ANALYST = (
    "You are a precise analyst. Answer only from the material provided. "
    "If the material does not contain the answer, say so explicitly rather "
    "than inferring it. Be specific and concise."
)


def _require_text(payload: dict) -> str:
    text = payload.get("text")
    if not text or not isinstance(text, str) or not text.strip():
        raise runtime.InvalidRequest("`text` is required.")
    if len(text) > MAX_INPUT_CHARS:
        raise runtime.InvalidRequest(
            f"`text` is {len(text)} characters, over the {MAX_INPUT_CHARS} limit.")
    return text


async def analyze(ctx, payload: dict) -> dict:
    """Answer a question about supplied text.

    The instruction to refuse rather than infer IS the product: a buyer
    reselling this downstream needs "not stated" to come back as "not stated",
    not as a confident guess. That is the same rule the audit routes follow --
    a check that could not run is never reported as a pass.
    """
    text = _require_text(payload)
    question = (payload.get("question") or "Summarize the key points.").strip()

    prompt = f"Material:\n\n{text}\n\n---\n\nTask: {question}"

    async def call(provider):
        return await provider.generate(
            prompt, system=_ANALYST,
            temperature=float(payload.get("temperature", 0.2)))

    value = await ctx.run("analyze", gemini.PROVIDERS, call, per_attempt_seconds=120)
    return {
        "question": question,
        "answer": value["text"],
        "model": value["model"],
        "input_chars": len(text),
        "tokens": {"prompt": value["prompt_tokens"], "output": value["output_tokens"]},
    }


async def extract_structured(ctx, payload: dict) -> dict:
    """Pull caller-specified fields out of text as JSON.

    `fields` is the caller's schema, and the answer comes back in exactly that
    shape. Returning their keys rather than ours is what makes this
    composable: the next worker, or the buyer's own code, can rely on what it
    asked for instead of parsing prose.
    """
    text = _require_text(payload)
    fields = payload.get("fields")
    if (not isinstance(fields, list) or not fields
            or not all(isinstance(f, str) and f.strip() for f in fields)):
        raise runtime.InvalidRequest("`fields` must be a non-empty list of field names.")

    prompt = (
        f"Material:\n\n{text}\n\n---\n\n"
        f"Extract exactly these fields as a JSON object: {', '.join(fields)}.\n"
        "Use null for any field the material does not state. Do not guess."
    )

    async def call(provider):
        return await provider.generate_json(prompt, system=_ANALYST, temperature=0.0)

    value = await ctx.run("extract_structured", gemini.PROVIDERS, call,
                          per_attempt_seconds=120)
    parsed = value["json"]
    if not isinstance(parsed, dict):
        raise runtime.InvalidProviderResponse("Model returned JSON that is not an object.")
    # Guarantee the caller's keys exist, so a field the page did not state is
    # an explicit null rather than a KeyError in the buyer's code.
    return {
        "fields": {field: parsed.get(field) for field in fields},
        "model": value["model"],
        "tokens": {"prompt": value["prompt_tokens"], "output": value["output_tokens"]},
    }


SKILLS = {"llm.analyze": analyze, "llm.extract": extract_structured}
