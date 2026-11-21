// Pre-generate Bulbul audio for demo reports so Demo mode speaks real Sarvam voices offline.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEMO_REPORTS, PLACES, FERRY_ROUTES, warningText } from "../public/data.js";
import { textToSpeech } from "../lib/sarvam.js";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
try { for (const l of fs.readFileSync(path.join(root, ".env"), "utf8").split("\n")) { const m = l.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/); if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim(); } } catch {}
if (!process.env.SARVAM_API_KEY) { console.error("Set SARVAM_API_KEY"); process.exit(1); }
const out = path.join(root, "public", "audio");
fs.mkdirSync(out, { recursive: true });

const jobs = [["confirm", "നന്ദി. നിങ്ങളുടെ റിപ്പോർട്ട് രേഖപ്പെടുത്തി. വിളവെടുപ്പ് സംഘത്തെ അറിയിച്ചിട്ടുണ്ട്.", "kavitha"]];
for (const d of DEMO_REPORTS) {
  const p = PLACES.find((x) => x.id === d.extraction.place_id);
  for (const r of [null, ...FERRY_ROUTES]) jobs.push([`${p.id}_${d.extraction.size}_${r ? r.id : "none"}`, warningText(p, d.extraction.size, r)]);
}
for (const [name, text, speaker] of jobs) {
  const b64 = await textToSpeech(text, { speaker });
  fs.writeFileSync(path.join(out, `${name}.wav`), Buffer.from(b64, "base64"));
  console.log("wrote", name);
}
