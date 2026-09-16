"""Media workers: image generation, speech synthesis, speech transcription."""

import base64
import binascii

from .. import runtime
from ..providers import imagen, stt, tts

MAX_PROMPT_CHARS = 2_000
MAX_TTS_CHARS = 3_000


async def generate_image(ctx, payload: dict) -> dict:
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise runtime.InvalidRequest("`prompt` is required.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise runtime.InvalidRequest(
            f"`prompt` is {len(prompt)} characters, over the {MAX_PROMPT_CHARS} limit.")
    aspect_ratio = (payload.get("aspect_ratio") or "1:1").strip()

    async def call(provider):
        return await provider.generate(prompt, aspect_ratio=aspect_ratio)

    # A retried generation would re-bill the vendor for a second image, so
    # this step gets exactly one attempt: a generative failure is refunded
    # to the caller (nothing billed), never silently regenerated at our cost.
    value = await ctx.run("generate", imagen.PROVIDERS, call, per_attempt_seconds=90,
                          max_attempts=1)
    return {
        "prompt": prompt, "aspect_ratio": aspect_ratio,
        "image_base64": value["image_base64"], "mime_type": value["mime_type"],
        "model": value["model"],
    }


async def synthesize_speech(ctx, payload: dict) -> dict:
    text = (payload.get("text") or "").strip()
    if not text:
        raise runtime.InvalidRequest("`text` is required.")
    if len(text) > MAX_TTS_CHARS:
        raise runtime.InvalidRequest(
            f"`text` is {len(text)} characters, over the {MAX_TTS_CHARS} limit.")
    voice = payload.get("voice")
    if voice is not None and not isinstance(voice, str):
        raise runtime.InvalidRequest("`voice`, when given, must be a string.")

    async def call(provider):
        return await provider.synthesize(text, voice=voice)

    value = await ctx.run("synthesize", tts.PROVIDERS, call, per_attempt_seconds=45,
                          max_attempts=1)
    return {
        "text_chars": len(text), "voice": value["voice"],
        "audio_base64": value["audio_base64"], "mime_type": value["mime_type"],
    }


async def transcribe_speech(ctx, payload: dict) -> dict:
    audio_base64 = payload.get("audio_base64")
    if not audio_base64 or not isinstance(audio_base64, str):
        raise runtime.InvalidRequest("`audio_base64` is required.")
    try:
        base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError):
        raise runtime.InvalidRequest("`audio_base64` is not valid base64.")
    language_code = (payload.get("language_code") or "en-US").strip()

    async def call(provider):
        return await provider.recognize(audio_base64, language_code=language_code)

    value = await ctx.run("transcribe", stt.PROVIDERS, call, per_attempt_seconds=60)
    return {
        "transcript": value["transcript"], "language_code": value["language_code"],
        "confidence": value.get("confidence"), "model": value["model"],
    }


SKILLS = {
    "image.generate": generate_image,
    "speech.synthesize": synthesize_speech,
    "speech.transcribe": transcribe_speech,
}
