// POST { text, target?, source? } -> { text }
import { apiKey, translate } from "../lib/sarvam.js";
export async function POST(request) {
  try {
    if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
    const { text, target = "en-IN", source = "auto" } = await request.json();
    return Response.json({ text: await translate(text || "", target, source) });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
