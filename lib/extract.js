import { PLACES, SIZES } from "../public/data.js";
import { chat } from "./sarvam.js";

const SYSTEM = `You read short Malayalam (or Manglish/English) voice reports from boatmen on Vembanad Lake, Kerala, about floating water hyacinth (കുളവാഴ / പായൽ / kulavazha / payal) mats.
Return ONLY a JSON object, no prose, with keys:
- "place_id": one id from this list that best matches the place mentioned, or null if none:
${PLACES.map((p) => `  ${p.id} = ${p.en} / ${p.ml}`).join("\n")}
- "place_text": the place exactly as spoken
- "size": one of "small","medium","large","huge" (ചെറിയ=small, ഇടത്തരം=medium, വലിയ=large, വളരെ വലിയ/ഭീമൻ=huge)
- "area_m2": number if the speaker gave a size (1 acre = 4047 m2, 1 cent = 40.5 m2), else null
- "blocking": true if boats/ferries are stuck or cannot pass
- "translation": one-sentence English translation
- "summary_ml": one short Malayalam sentence summarising the report`;

export async function extractReport(transcript) {
  try {
    const out = await chat([
      { role: "system", content: SYSTEM },
      { role: "user", content: transcript },
    ]);
    const m = out.match(/\{[\s\S]*\}/);
    if (!m) throw new Error("no JSON in model output");
    const j = JSON.parse(m[0]);
    if (!PLACES.find((p) => p.id === j.place_id)) j.place_id = heuristicPlace(transcript);
    if (!SIZES[j.size]) j.size = heuristicSize(transcript);
    return { ...j, source: "sarvam-105b" };
  } catch (e) {
    return {
      place_id: heuristicPlace(transcript),
      size: heuristicSize(transcript),
      area_m2: null,
      blocking: /പറ്റുന്നില്ല|കുടുങ്ങി|stuck|block/i.test(transcript),
      translation: null,
      source: "keyword-fallback",
      error: String(e.message || e),
    };
  }
}

export function heuristicPlace(t) {
  const s = t.toLowerCase();
  const hit = PLACES.find((p) => p.aliases.some((a) => s.includes(a.toLowerCase())));
  return hit ? hit.id : null;
}

export function heuristicSize(t) {
  if (/വളരെ വലിയ|ഭീമ|huge|very big|ഏക്കർ/i.test(t)) return "huge";
  if (/വലിയ|large|big/i.test(t)) return "large";
  if (/ചെറിയ|small/i.test(t)) return "small";
  return "medium";
}
