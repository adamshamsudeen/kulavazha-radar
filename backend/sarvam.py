"""Thin async client for the Sarvam AI REST APIs. The key never leaves the server."""
import asyncio
import json

import httpx

from . import config

BASE = "https://api.sarvam.ai"
_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))


class SarvamError(RuntimeError):
    pass


def _headers() -> dict:
    return {"api-subscription-key": config.SARVAM_API_KEY}


async def _check(res: httpx.Response, what: str) -> dict:
    if res.status_code >= 400:
        raise SarvamError(f"{what} failed ({res.status_code}): {res.text[:300]}")
    return res.json()


async def speech_to_text(audio: bytes, mime: str = "audio/webm", language: str | None = "ml-IN",
                         mode: str = "transcribe") -> str:
    """Saaras v4 speech-to-text (REST, up to 30 s of audio)."""
    ext = mime.split("/")[-1].split(";")[0] or "webm"
    data = {"model": "saaras:v4", "mode": mode}
    if language:
        data["language_code"] = language
    files = {"file": (f"audio.{ext}", audio, mime.split(";")[0])}
    res = await _client.post(f"{BASE}/speech-to-text", headers=_headers(), data=data, files=files)
    return (await _check(res, "Speech-to-text")).get("transcript", "")


async def chat(messages: list[dict], max_tokens: int = 700, temperature: float = 0.2,
               json_mode: bool = False) -> str:
    """Sarvam-105B chat completion with reasoning switched off for speed."""
    body = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": None,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    res = await _client.post(f"{BASE}/v1/chat/completions", headers=_headers(), json=body)
    if res.status_code == 400 and json_mode:  # model/version without structured output: retry plain
        body.pop("response_format")
        res = await _client.post(f"{BASE}/v1/chat/completions", headers=_headers(), json=body)
    j = await _check(res, "Chat")
    return (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""


async def translate(text: str, target: str = "en-IN", source: str = "auto") -> str:
    """Sarvam-Translate text translation (22 Indian languages + English)."""
    res = await _client.post(f"{BASE}/translate", headers=_headers(), json={
        "input": text[:1900],
        "source_language_code": source,
        "target_language_code": target,
        "model": "sarvam-translate:v1",
    })
    return (await _check(res, "Translate")).get("translated_text", "")


async def text_to_speech(text: str, language: str = "ml-IN", speaker: str | None = None,
                         pace: float = 0.95) -> str:
    """Bulbul v3 text-to-speech. Returns base64 WAV."""
    res = await _client.post(f"{BASE}/text-to-speech", headers=_headers(), json={
        "text": text[:2400],
        "language_code": language,
        "target_language_code": language,
        "model": config.TTS_MODEL,
        "speaker": speaker or config.TTS_SPEAKER,
        "pace": pace,
    })
    return "".join((await _check(res, "Text-to-speech")).get("audios", []))


async def vision_extract(image: bytes, mime: str, schema: dict, timeout_s: float = 25) -> dict:
    """Sarvam Vision (Document Intelligence) field extraction: submit job, poll, fetch results."""
    ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
    res = await _client.post(
        f"{BASE}/doc-ai/v1/job/extract", headers=_headers(),
        data={"schema": json.dumps(schema), "mode": "basic", "output_format": "json"},
        files={"file": (f"slip.{ext}", image, mime)},
    )
    job_id = (await _check(res, "Vision submit"))["job_id"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await asyncio.sleep(1.2)
        st = await _check(await _client.get(f"{BASE}/doc-ai/v1/job/{job_id}/status", headers=_headers()), "Vision status")
        status = str(st.get("status") or st.get("job_state") or "").lower()
        if status in ("completed", "partially_completed"):
            return await _check(await _client.get(f"{BASE}/doc-ai/v1/job/{job_id}/results", headers=_headers()), "Vision results")
        if status in ("failed", "rejected"):
            raise SarvamError(f"Vision job {status}")
    raise SarvamError("Vision job timed out")
