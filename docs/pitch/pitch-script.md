# Kulavazha Radar: 3-minute pitch and demo script

**Tracks:** Beyond English (Sarvam) · Alappuzha Challenge · Beyond the Screen
**One line:** Boatmen report water hyacinth by voice in Malayalam. Ferries get warned before the weed reaches them. Every mat becomes paid harvest work and raw material for mushroom farming.

---

## 0:00 to 0:30 · The problem (look at the judges, not the screen)
- "On 3 September, the Muhamma–Kumarakom ferry got stuck in the middle of Vembanad for hours. Kulavazha, water hyacinth, had wrapped its propeller. Onmanorama says this happens almost daily."
- "Every boatman here knows kulavazha: the mats that block ferries, choke canals and strand fishing boats."
- "Nobody tracks where the mats are or where they're going. Reports happen by phone call, in Malayalam, and get lost. The weed is cleared and dumped, so it's a cost, never a resource."
- "The people who see it first are boatmen and fishers. They won't fill in an English form. They will **speak**."

## 0:30 to 1:45 · Live demo (big screen = Radar page, live mode)
1. **Phone report (the wow moment).** "Scan this QR." Ask a judge to scan it, or use your own phone. Speak in Malayalam:
   > "പുന്നമടയിൽ നെഹ്റു ട്രോഫി ഫിനിഷിങ് പോയിന്റിന് അടുത്ത് വലിയൊരു കുളവാഴക്കൂട്ടം ഉണ്ട്, ബോട്ടിന് പോകാൻ പറ്റുന്നില്ല"
   - Point at the pipeline as it ticks: **Saaras v4** heard it → **Sarvam-105B** found the place, the size and "boats blocked" → English line for officials.
   - The pin drops at Punnamada with the **6-hour wind-drift arrow** (live wind).
   - The phone hears a **Bulbul** Malayalam "thank you, crew informed". If the drift crosses a ferry lane, the route turns red and the ferry warning plays in Malayalam.
2. **Harvest job.** Click *Assign crew* → *📄 Scan slip*. Photograph a handwritten slip ("Kainakary Kudumbashree, 11,800 kg") → **Sarvam Vision** reads the weight → the counter moves toward **100 t**, with ₹ to the crew, pellet kg and mushroom potential.
3. **Brief for officials.** Click it → **Sarvam-105B** writes the brief, **Sarvam-Translate** renders it in Malayalam, **Bulbul** reads it aloud. "This is what lands on the Collector's desk every morning."
4. **(If the camera is set up)** Point at the Weed Watch card: "This CP Plus camera on our houseboat files a report by itself when the green cover stays above 30%."
5. **(20 s) PayalCam.** Click the red **● Watch the lake live** tab. "This is a live camera on our houseboat. Anyone with the link can watch Punnamada Lake, chat, and take turns steering it. Tap **Postcard** to keep a snapshot." Pitch line: **"Crowd-sourced lake watching: anyone, anywhere, can help spot pola."** Next step: a **Report weed** button beside Postcard, so any viewer can snap a frame and file a report. (Say "coming next" unless the button is live.)
6. **(15 s)** Switch to the **Houseboat Translator** tab: a guest says "Can we stop to see the birds?" and the crew hears it in Malayalam.

## 1:45 to 2:30 · Why it matters
- "Five Sarvam models in one loop: **Saaras v4, Sarvam-105B, Sarvam-Translate, Bulbul v3 and Sarvam Vision**. The guest translator reuses Saaras, Sarvam-Translate and Bulbul. Malayalam in, Malayalam out. English only where officials need it."
- "It's the data layer for a real project: the **MushPellets** pilot we're proposing to the Kerala Agriculture Minister with Dr Arya. It turns hyacinth into mushroom substrate pellets and mycelium packaging, with a ₹57.5 lakh, 12-month pilot targeting **100 tonnes** of wet weed."
- "100 t of wet weed is about 8 t dry matter, which becomes substrate for roughly **4 to 12 t of oyster mushrooms** depending on the blend. The weed becomes income for fisher and Kudumbashree crews."
- All figures on screen are labelled as **estimates**, with sources in the assumptions panel.

## 2:30 to 3:00 · Ask and close
- "Next: WhatsApp voice notes and a missed-call voice line for boatmen without smartphones, field weighing to calibrate the estimates, and a pilot with CWRDM and KAU."
- "Kulavazha Radar: the lake reports itself, in its own language."

---

## Demo checklist (6:30 AM)
- [ ] Laptop on power, server running: `uvicorn backend.app:app --host 0.0.0.0 --port 3000`
- [ ] Public URL or tunnel open on the big screen, QR visible (check `PUBLIC_URL`)
- [ ] Mode toggle shows **Live**; test one sample chip
- [ ] Phone volume up, mic permission granted on the phone browser
- [ ] Handwritten harvest slip ready (crew name, date, weight in kg)
- [ ] Clear old demo data: Assumptions → *Reset demo data*
- [ ] **Fallback:** if the network drops, switch to **Demo** (canned reports and the baked Bulbul voices still work)
- [ ] PayalCam: camera turned to face the lake (not the cabin), controls open, tab link works
- [ ] Weed Watch: set `CAM_URL` to PayalCam's HLS stream if you want the auto-report card on screen (it is hidden while unset)
- [ ] Backup screen recording of one full run on the laptop desktop

## Likely judge questions
- **How accurate is the tonnage?** "It's estimated from mat size, around 20 kg/m² for dense mats. The slip scan replaces the estimate with the weighed figure, and the pilot calibrates both."
- **Why voice?** "Boatmen talk, they don't type. Saaras handles Malayalam and code-mixed Manglish."
- **Drift model?** "Wind-driven, at 2 to 5% of wind speed from debris models, with live Open-Meteo wind. Not yet validated on Vembanad. That's pilot work."
- **What about no smartphone?** "Next step is WhatsApp voice notes and a missed-call voice line built on the same Sarvam loop, so they just call."
- **Why Sarvam and not a global model?** "Malayalam and code-mixed Manglish speech is where Sarvam is strongest, Bulbul has natural Malayalam voices for the replies, and Sarvam-105B gives us structured JSON from messy speech. One Indian stack for hearing, understanding, translating, speaking and reading slips."
- **Does it fail safe?** "If 105B isn't sure of the place, the phone asks the reporter to confirm instead of guessing. No signal? The report is saved on the phone and sent later. No network at all? Demo mode with baked Bulbul audio."
- **Is it working or a mock-up?** "It's live right now. Scan the QR." (Backup: kulavazha-radar.onrender.com)
- **Who runs the camera?** "PayalCam is our own camera on the houseboat. Viewers share the controls in turns, and the owner can pause them. Weed Watch reads the same feed, so the crowd and the camera both watch the lake."
