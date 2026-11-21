// POST { reports:[...] } -> { en, ml } : Sarvam-105B writes an officials' brief; Sarvam-Translate renders Malayalam.
import { apiKey, chat, translate } from "../lib/sarvam.js";
export async function POST(request) {
  try {
    if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
    const { reports = [] } = await request.json();
    const lines = reports.slice(0, 30).map((r) => `- ${r.place}: ${r.size} mat, ~${r.wet_t} t wet, status ${r.status}${r.alert ? `, may reach ${r.alert} ferry route` : ""}`).join("\n");
    const en = (await chat([
      { role: "system", content: "You write a crisp daily situation brief (max 90 words, plain sentences, no markdown) for the Alappuzha District Collector and the Agriculture Minister's office about water hyacinth on Vembanad Lake. Cover: biggest risks to ferries, harvest progress toward the 100 t MushPellets pilot, and one recommended action. Say figures are estimates." },
      { role: "user", content: `Today's reports:\n${lines || "(none)"}` },
    ], { maxTokens: 400, temperature: 0.3 })).trim();
    const ml = await translate(en, "ml-IN", "en-IN").catch(() => "");
    return Response.json({ en, ml });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
