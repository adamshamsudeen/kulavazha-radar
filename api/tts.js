// POST { text, speaker? } -> { audio_b64 }
import { apiKey, textToSpeech } from "../lib/sarvam.js";

export async function POST(request) {
  try {
    if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
    const { text, speaker } = await request.json();
    if (!text) return Response.json({ error: "No text" }, { status: 400 });
    const audio_b64 = await textToSpeech(text.slice(0, 2400), { speaker });
    return Response.json({ audio_b64 });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
