"""Kulavazha Radar API (FastAPI).

Run locally:   uvicorn backend.app:app --reload --port 3000   (serves the frontend too)
On Vercel:     api/index.py imports this app; /api/* is routed to it.
"""
import asyncio
import base64
import shutil
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import camera, config, sarvam
from .extract import extract_report

app = FastAPI(title="Kulavazha Radar", description="Voice-first water hyacinth radar for Vembanad Lake, on Sarvam AI")

# Simple guard for a public demo URL: caps Sarvam-spending calls (and feed posts) per IP and in total.
import collections
from fastapi import Request

PAID = {"/api/report", "/api/tts", "/api/translate", "/api/slip", "/api/brief", "/api/converse", "/api/feed", "/api/phone-report"}
PER_IP_PER_MIN = int(__import__("os").environ.get("RATE_PER_MIN", "20"))
TOTAL_PER_HOUR = int(__import__("os").environ.get("RATE_TOTAL_PER_HOUR", "600"))
_hits: dict = collections.defaultdict(collections.deque)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path in PAID:
        now = time.time()
        ip = (request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
              or (request.client.host if request.client else "?"))
        q, allq = _hits[ip], _hits["*"]
        while q and now - q[0] > 60:
            q.popleft()
        while allq and now - allq[0] > 3600:
            allq.popleft()
        if len(q) >= PER_IP_PER_MIN or len(allq) >= TOTAL_PER_HOUR:
            return JSONResponse({"error": "Rate limit reached for this demo. Try again in a minute."}, status_code=429)
        q.append(now)
        allq.append(now)
    return await call_next(request)


def err(e: Exception, status: int = 502) -> JSONResponse:
    return JSONResponse({"error": str(e)[:400]}, status_code=status)


def need_key():
    if not config.SARVAM_API_KEY:
        return JSONResponse({"error": "SARVAM_API_KEY not set"}, status_code=503)
    return None


def ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


@app.get("/api/status")
async def status():
    return {"ok": True, "live": bool(config.SARVAM_API_KEY), "camera": bool(camera.current_url()), "cam_place": camera.status()["place"],
            "public_url": config.PUBLIC_URL, "backend": "python-fastapi"}


# 1. Boatman voice report -> transcript + structured extraction ------------------------------
class ReportIn(BaseModel):
    audio_b64: str | None = None
    mime: str = "audio/webm"
    text: str | None = None


@app.post("/api/report")
async def report(body: ReportIn):
    timings: dict = {}
    t0 = time.perf_counter()
    try:
        transcript = (body.text or "").strip()
        if not transcript and body.audio_b64:
            if (r := need_key()):
                return r
            transcript = await sarvam.speech_to_text(base64.b64decode(body.audio_b64), body.mime)
            timings["stt_ms"] = ms_since(t0)
        if not transcript:
            return JSONResponse({"error": "Empty report"}, status_code=400)
        if not config.SARVAM_API_KEY:
            return {"transcript": transcript, "translation": None, "extraction": {"source": "no-key"}, "timings": timings}
        t1 = time.perf_counter()
        extraction = await extract_report(transcript)
        timings["llm_ms"] = ms_since(t1)
        translation = extraction.get("translation")
        if not translation:  # fall back to Sarvam-Translate
            try:
                translation = await sarvam.translate(transcript, "en-IN", "ml-IN")
            except Exception:
                translation = None
        return {"transcript": transcript, "translation": translation, "extraction": extraction, "timings": timings}
    except Exception as e:
        return err(e)


# 2. Bulbul voice -------------------------------------------------------------------------------
class TtsIn(BaseModel):
    text: str
    speaker: str | None = None
    language: str = "ml-IN"


@app.post("/api/tts")
async def tts(body: TtsIn):
    if (r := need_key()):
        return r
    try:
        return {"audio_b64": await sarvam.text_to_speech(body.text, body.language, body.speaker)}
    except Exception as e:
        return err(e)


# 3. Translate ----------------------------------------------------------------------------------
class TranslateIn(BaseModel):
    text: str
    target: str = "en-IN"
    source: str = "auto"


@app.post("/api/translate")
async def translate(body: TranslateIn):
    if (r := need_key()):
        return r
    try:
        out = await sarvam.translate(body.text, body.target, body.source)
        return {"text": config.fix_ml(out) if body.target == "ml-IN" else out}
    except Exception as e:
        return err(e)


# 4. Harvest slip -> real weight (Sarvam Vision) ----------------------------------------------
SLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "wet_weight_kg": {"type": "number", "description": "Total wet weight of water hyacinth (kulavazha / പായൽ) harvested, in kilograms. Convert tonnes to kg."},
        "date": {"type": "string", "description": "Date on the slip"},
        "crew": {"type": "string", "description": "Name of the harvesting group, crew or Kudumbashree unit"},
        "location": {"type": "string", "description": "Place where the weed was harvested or landed"},
        "vehicle_or_boat": {"type": "string", "description": "Boat or vehicle number, if present"},
    },
}


class SlipIn(BaseModel):
    image_b64: str
    mime: str = "image/jpeg"


@app.post("/api/slip")
async def slip(body: SlipIn):
    if (r := need_key()):
        return r
    t0 = time.perf_counter()
    try:
        out = await sarvam.vision_extract(base64.b64decode(body.image_b64), body.mime, SLIP_SCHEMA)
        return {"result": out.get("result") or {}, "confidence": out.get("annotations"), "ms": ms_since(t0)}
    except Exception as e:
        return err(e)


# 5. Daily brief for officials ----------------------------------------------------------------
class BriefIn(BaseModel):
    reports: list[dict] = []


@app.post("/api/brief")
async def brief(body: BriefIn):
    if (r := need_key()):
        return r
    try:
        rs = body.reports[:30]
        done = sum(float(r.get("wet_t") or 0) for r in rs if r.get("status") == "harvested")
        waiting = sum(float(r.get("wet_t") or 0) for r in rs if r.get("status") != "harvested")
        lines = "\n".join(
            f"- {r.get('place')}: {r.get('size')} mat, about {r.get('wet_t')} t wet, status {r.get('status')}"
            + (f", forecast to reach the {r['alert']} route" if r.get("alert") else "") for r in rs) or "(no reports)"
        facts = f"Pilot target 100 t wet weed. Harvested so far: {done:.1f} t. Reported and waiting: {waiting:.1f} t."
        en = (await sarvam.chat([
            {"role": "system", "content": "You write a crisp situation brief (max 80 words, plain sentences, no markdown, no headings) "
             "for the Alappuzha District Collector and the Agriculture Minister's office about water hyacinth on Vembanad Lake. "
             "Use ONLY the numbers given; do not invent percentages or figures. Cover: the biggest risk to ferries, harvest progress "
             "toward the MushPellets pilot, and one recommended action. Note that tonnages are estimates."},
            {"role": "user", "content": f"{facts}\nReports:\n{lines}"},
        ], max_tokens=400, temperature=0.3)).strip()
        try:
            ml = config.fix_ml(await sarvam.translate(en.replace("water hyacinth", "kulavazha (water hyacinth)"), "ml-IN", "en-IN"))
        except Exception:
            ml = ""
        return {"en": en, "ml": ml}
    except Exception as e:
        return err(e)


# 6. Houseboat Translator: guest <-> Malayalam crew ------------------------------------------
VOICE = {"ml-IN": "anand", "en-IN": "priya", "hi-IN": "priya"}


class ConverseIn(BaseModel):
    audio_b64: str | None = None
    mime: str = "audio/webm"
    text: str | None = None
    source: str = "en-IN"
    target: str = "ml-IN"


@app.post("/api/converse")
async def converse(body: ConverseIn):
    if (r := need_key()):
        return r
    t: dict = {}
    try:
        t0 = time.perf_counter()
        heard = body.text or await sarvam.speech_to_text(base64.b64decode(body.audio_b64 or ""), body.mime, body.source)
        t["stt_ms"] = ms_since(t0)
        t1 = time.perf_counter()
        translated = await sarvam.translate(heard, body.target, body.source)
        if body.target == "ml-IN":
            translated = config.fix_ml(translated)
        t["translate_ms"] = ms_since(t1)
        t2 = time.perf_counter()
        try:
            audio = await sarvam.text_to_speech(translated, body.target, VOICE.get(body.target, "priya"))
        except Exception:
            audio = ""
        t["tts_ms"] = ms_since(t2)
        return {"heard": heard, "translated": translated, "audio_b64": audio, "timings": t}
    except Exception as e:
        return err(e)


# 7. Live feed: phones post reports, the big screen polls -----------------------------------
FEED: dict = {"seq": 0, "items": []}


@app.get("/api/feed")
async def feed_get(since: int = 0):
    return {"seq": FEED["seq"], "items": [i for i in FEED["items"] if i["seq"] > since]}


@app.post("/api/feed")
async def feed_post(item: dict):
    FEED["seq"] += 1
    photo = item.get("photo")
    clean = {
        "seq": FEED["seq"],
        "ts": int(time.time() * 1000),
        "reporter": str(item.get("reporter") or "Boatman")[:60],
        "transcript": str(item.get("transcript") or "")[:1000],
        "translation": (str(item["translation"])[:1000] if item.get("translation") else None),
        "extraction": item.get("extraction") or {},
        "photo": photo if isinstance(photo, str) and len(photo) < 300_000 else None,
        "green": item.get("green"),
        "gps": item.get("gps"),
        "timings": item.get("timings"),
        "source": str(item.get("source") or "phone")[:20],
    }
    FEED["items"] = (FEED["items"] + [clean])[-200:]
    return {"ok": True, "seq": clean["seq"]}


# 7b. Phone call report: a Sarvam Samvaad voice agent calls this tool mid-call -----------------
class PhoneReportIn(BaseModel):
    place: str | None = None        # where the weed is, as the caller said it (Malayalam or English)
    size: str | None = None         # how big, as the caller said it
    blocking: bool | str | None = None
    transcript: str | None = None   # optional: full call transcript so far
    caller: str | None = None       # optional: caller phone number (Samvaad "User Identifier")
    interaction_id: str | None = None


def _mask(num: str | None) -> str:
    d = "".join(c for c in (num or "") if c.isdigit())
    return f"xxxxxx{d[-4:]}" if len(d) >= 4 else ""


@app.post("/api/phone-report")
async def phone_report(body: PhoneReportIn, request: Request):
    token = __import__("os").environ.get("PHONE_REPORT_TOKEN", "")
    if token and request.headers.get("x-api-key") != token and request.query_params.get("key") != token:
        return JSONResponse({"error": "bad key"}, status_code=401)
    if body.interaction_id and any(i.get("call_id") == body.interaction_id for i in FEED["items"]):
        return {"ok": True, "duplicate": True, "reply_ml": "ഈ റിപ്പോർട്ട് ഇതിനകം കിട്ടി. നന്ദി."}
    parts = []
    if body.place:
        parts.append(f"സ്ഥലം: {body.place}.")
    if body.size:
        parts.append(f"വലിപ്പം: {body.size}.")
    if str(body.blocking).lower() in ("true", "yes", "1", "അതെ"):
        parts.append("ബോട്ടുകൾക്ക് പോകാൻ പറ്റുന്നില്ല.")
    summary = " ".join(parts) or (body.transcript or "").strip()
    if not summary:
        return JSONResponse({"error": "Need place or size", "reply_ml": "സ്ഥലം ഒന്നുകൂടി പറയാമോ?"}, status_code=400)
    text = summary + (f"\n\nCall transcript: {body.transcript.strip()[:1500]}" if body.transcript and parts else "")
    t0 = time.perf_counter()
    extraction = await extract_report(text) if config.SARVAM_API_KEY else {
        "place_id": None, "size": "medium", "blocking": False, "source": "no-key"}
    from .extract import heuristic_place
    if not extraction.get("place_id"):
        extraction["place_id"] = heuristic_place(text)
    if str(body.blocking).lower() in ("true", "yes", "1"):
        extraction["blocking"] = True
    timings = {"llm_ms": ms_since(t0)}
    translation = extraction.get("translation")
    if not translation and config.SARVAM_API_KEY:
        try:
            translation = await sarvam.translate(summary, "en-IN", "ml-IN")
        except Exception:
            translation = None
    FEED["seq"] += 1
    item = {
        "seq": FEED["seq"], "ts": int(time.time() * 1000),
        "reporter": f"Phone caller {_mask(body.caller)}".strip(),
        "transcript": summary[:1000], "translation": translation, "extraction": extraction,
        "photo": None, "green": None, "gps": None, "timings": timings, "source": "call",
        "call_id": (body.interaction_id or "")[:80] or None,
    }
    FEED["items"] = (FEED["items"] + [item])[-200:]
    place = next((p for p in config.PLACES if p["id"] == extraction.get("place_id")), None)
    where = place["ml"] if place else (body.place or "കായലിൽ")
    return {
        "ok": True, "seq": item["seq"], "place_id": extraction.get("place_id"),
        "place_en": place["en"] if place else None, "size": extraction.get("size"),
        "blocking": bool(extraction.get("blocking")),
        "reply_ml": f"നന്ദി. {where} ഭാഗത്തെ കുളവാഴ റഡാറിൽ രേഖപ്പെടുത്തി. ഫെറി ജീവനക്കാർക്ക് മുന്നറിയിപ്പ് പോകും.",
    }


# 8. Weed Watch camera (CP Plus or any RTSP / snapshot URL; laptop only) ---------------------
@app.get("/api/cam")
async def cam():
    if not camera.ensure_running():
        return JSONResponse({"error": "CAM_URL not set"}, status_code=404)
    if not camera.STATE["jpeg"]:
        return JSONResponse({"error": camera.STATE["error"] or "waiting for first frame"}, status_code=503)
    return Response(camera.STATE["jpeg"], media_type="image/jpeg",
                    headers={"cache-control": "no-store", "x-frame-age": str(round(time.time() - camera.STATE["ts"], 1))})


@app.get("/api/cam/status")
async def cam_status():
    return camera.status()


# Frontend (local run). On Vercel, public/ is served by the CDN instead.
if config.PUBLIC.exists():
    app.mount("/", StaticFiles(directory=config.PUBLIC, html=True), name="public")
