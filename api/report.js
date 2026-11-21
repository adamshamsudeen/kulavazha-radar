// POST { audio_b64?, mime?, text? } -> { transcript, extraction }
import { apiKey, speechToText, translate } from "../lib/sarvam.js";
import { extractReport } from "../lib/extract.js";

export async function POST(request) {
  const t0 = Date.now();
  try {
    const body = await request.json();
    const timings = {};
    let transcript = (body.text || "").trim();
    if (!transcript && body.audio_b64) {
      if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
      const buf = Buffer.from(body.audio_b64, "base64");
      transcript = await speechToText(buf, body.mime || "audio/webm");
      timings.stt_ms = Date.now() - t0;
    }
    if (!transcript) return Response.json({ error: "Empty report" }, { status: 400 });
    const t1 = Date.now();
    // Sarvam-105B extraction and Sarvam-Translate English rendering run in parallel.
    const [extraction, translation] = apiKey()
      ? await Promise.all([
          extractReport(transcript).then((x) => ((timings.llm_ms = Date.now() - t1), x)),
          translate(transcript, "en-IN", "ml-IN").then((x) => ((timings.translate_ms = Date.now() - t1), x)).catch(() => null),
        ])
      : [{ source: "no-key" }, null];
    return Response.json({ transcript, translation, extraction, timings });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
