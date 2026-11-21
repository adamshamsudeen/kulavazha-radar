# Kulavazha Radar · കുളവാഴ റഡാർ

Voice-first water hyacinth (kulavazha) reporting for Vembanad Lake, built on **Sarvam AI**.

1. A boatman reports a mat **by voice in Malayalam** → Saaras v4 speech-to-text.
2. **Sarvam-105B** extracts place, size and whether boats are blocked; **Sarvam-Translate** makes an English copy for officials.
3. A pin drops on the lake with a **6-hour wind-drift forecast** (Open-Meteo). If it will reach a ferry route, the route turns red and **Bulbul v3** speaks a Malayalam warning; the reporter hears a Malayalam confirmation.
4. Each mat becomes a **harvest job**: estimated wet tonnes → substrate pellet kg → ₹ to the harvesting crew, counting toward the 100-tonne MushPellets pilot.
5. When harvested, a photo of the weighbridge/harvest slip is read by **Sarvam Vision** to log the real weight.
6. **Brief for officials**: Sarvam-105B writes a daily brief, Sarvam-Translate renders it in Malayalam, Bulbul reads it out.
7. **Houseboat Translator** (`/translator.html`): guest (English/Hindi/…) ↔ crew (Malayalam) speech translation.

All biomass, pellet and ₹ figures are **estimates**; assumptions live in `public/data.js` (`ASSUMPTIONS`).

## Run locally
```bash
cp .env.example .env   # add SARVAM_API_KEY
npm start              # http://localhost:3000
```
No dependencies. Without a key the app runs in **Demo mode** with canned Malayalam reports (works offline except map tiles).

## Deploy (Vercel)
Import the repo in Vercel (framework: Other), add env var `SARVAM_API_KEY`, deploy. `public/` is served statically; `api/*.js` are serverless functions.

## Bake demo audio (optional, for offline demos)
```bash
SARVAM_API_KEY=... npm run bake-audio   # writes Bulbul warnings to public/audio/
```
