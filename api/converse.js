// Houseboat Translator. POST { audio_b64?, mime?, text?, from, to } -> { heard, translated, audio_b64 }
import { apiKey, speechToText, translate, textToSpeech } from "../lib/sarvam.js";

const VOICE = { "ml-IN": "anand", "en-IN": "priya", "hi-IN": "priya", "ta-IN": "priya" };

export async function POST(request) {
  const t = {};
  try {
    if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
    const { audio_b64, mime = "audio/webm", text, from = "en-IN", to = "ml-IN" } = await request.json();
    let t0 = Date.now();
    const heard = text || (await speechToText(Buffer.from(audio_b64, "base64"), mime, "transcribe", from));
    t.stt_ms = Date.now() - t0; t0 = Date.now();
    const translated = await translate(heard, to, from);
    t.translate_ms = Date.now() - t0; t0 = Date.now();
    const audio = await textToSpeech(translated, { language: to, speaker: VOICE[to] }).catch(() => "");
    t.tts_ms = Date.now() - t0;
    return Response.json({ heard, translated, audio_b64: audio, timings: t });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
