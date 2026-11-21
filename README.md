# Kulavazha Radar · കുളവാഴ റഡാർ

Voice-first water hyacinth (kulavazha) radar for Vembanad Lake, built on **Sarvam AI**.
A data layer for turning a nuisance weed into paid harvest work and MushPellets mushroom substrate.

## What it does
1. **Report by voice in Malayalam** from any phone (scan the QR on the big screen) → **Saaras v4** speech-to-text.
2. **Sarvam-105B** extracts place, size and whether boats are blocked, and gives a faithful English line for officials.
3. A pin drops on the lake with a **6-hour wind-drift forecast** (Open-Meteo). If the mat will reach a ferry route, the route turns red and **Bulbul v3** speaks a Malayalam warning; the reporter hears a Malayalam confirmation.
4. Each mat becomes a **harvest job**: estimated wet tonnes → substrate pellet kg → ₹ to the harvesting crew, counting toward the 100-tonne MushPellets pilot.
5. When harvested, a photo of the weighbridge/harvest slip is read by **Sarvam Vision** to log the real weight.
6. **Brief for officials**: Sarvam-105B writes it, **Sarvam-Translate** renders it in Malayalam, Bulbul reads it out.
7. **Weed Watch camera**: a CP Plus (or any RTSP) camera frame is grabbed every few seconds; when green mat cover passes a threshold a report is filed automatically.
8. **Houseboat Translator** (`/translator.html`): guest (English/Hindi/…) ↔ crew (Malayalam) speech translation.

All biomass, pellet and ₹ figures are **estimates**; assumptions are in `public/places.json` (`assumptions`).

## Stack
- **Backend:** Python 3.10+, FastAPI, httpx (`backend/`). `backend/sarvam.py` wraps every Sarvam API used.
- **Frontend:** plain HTML/CSS/JS + Leaflet (`public/`), no build step.
- **Deploy:** Vercel (Python function at `api/index.py`), or run on a laptop.

## Run locally
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add SARVAM_API_KEY
uvicorn backend.app:app --reload --port 3000   # http://localhost:3000  (API docs at /docs)
```
Without a key the app runs in **Demo mode** with canned Malayalam reports.

## Phones on the big screen (laptop demo)
Expose the laptop with a tunnel, e.g. `cloudflared tunnel --url http://localhost:3000`, put the URL in `.env` as `PUBLIC_URL`, restart. The QR code then points phones at it.

## Camera
Set `CAM_URL` (e.g. `rtsp://admin:PASS@192.168.1.64:554/cam/realmonitor?channel=1&subtype=1`) and `CAM_PLACE`. Needs `ffmpeg`.

## Offline demo audio
`python -m tools.bake_audio` writes Bulbul voice files into `public/audio/` so demo mode speaks with real Sarvam voices without network.

## Backup deploy on Render (free)

1. Open https://render.com/deploy?repo=https://github.com/adamshamsudeen/kulavazha-radar and sign in with GitHub.
2. Paste your Sarvam key into `SARVAM_API_KEY` when asked, then click **Apply**.
3. Wait for the first build (about 3 minutes). Your URL is `https://kulavazha-radar-xxxx.onrender.com`. The QR code uses it automatically, via `RENDER_EXTERNAL_URL`.

Free services sleep after about 15 minutes without traffic, and the first visit after that takes about a minute. Open the URL a few minutes before you demo. The live feed is in memory, so it clears whenever the service sleeps. The camera grabber needs ffmpeg, so it's off on Render.
