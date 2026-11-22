import {
  LAKE_CENTER, PLACES, FERRY_ROUTES, SIZES, ASSUMPTIONS, estimate,
  DEMO_REPORTS, DEMO_WIND, warningText,
} from "./data.js";

const $ = (s) => document.querySelector(s);
const STORE_KEY = "kr_reports_v1";

const state = {
  live: false,        // server has a Sarvam key
  mode: "demo",       // "live" | "demo"
  wind: null,         // { speed_ms[6], dir_deg[6], source }
  reports: [],
  demoIdx: 0,
};

/* ---------------- Map ---------------- */
const map = L.map("map", { zoomControl: true, attributionControl: true }).setView(LAKE_CENTER, 12);
const satellite = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 18, attribution: "Imagery © Esri" });
const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" });
satellite.addTo(map);
L.control.layers({ Satellite: satellite, Map: streets }, {}, { position: "topleft" }).addTo(map);

const ferryLayer = L.layerGroup().addTo(map);
const matLayer = L.layerGroup().addTo(map);
const ferryLines = {};
for (const r of FERRY_ROUTES) {
  ferryLines[r.id] = L.polyline(r.path, { color: "#5cc8ff", weight: 3, opacity: 0.75 })
    .bindTooltip(r.en, { className: "ferry-tt", sticky: true })
    .addTo(ferryLayer);
}
map.fitBounds(L.latLngBounds(FERRY_ROUTES.flatMap((r) => r.path)).pad(0.08));

/* ---------------- Geometry ---------------- */
const R = 6371000;
function move([lat, lng], bearingDeg, dist) {
  const b = (bearingDeg * Math.PI) / 180;
  const dLat = (dist * Math.cos(b)) / R;
  const dLng = (dist * Math.sin(b)) / (R * Math.cos((lat * Math.PI) / 180));
  return [lat + (dLat * 180) / Math.PI, lng + (dLng * 180) / Math.PI];
}
function toXY([lat, lng], ref) {
  const x = ((lng - ref[1]) * Math.PI / 180) * R * Math.cos((ref[0] * Math.PI) / 180);
  const y = ((lat - ref[0]) * Math.PI / 180) * R;
  return [x, y];
}
function distPointSeg(p, a, b) {
  const [px, py] = toXY(p, a), [bx, by] = toXY(b, a);
  const t = Math.max(0, Math.min(1, (px * bx + py * by) / (bx * bx + by * by || 1)));
  return Math.hypot(px - t * bx, py - t * by);
}
function distToRoute(p, route) {
  let d = Infinity;
  for (let i = 0; i < route.path.length - 1; i++) d = Math.min(d, distPointSeg(p, route.path[i], route.path[i + 1]));
  return d;
}
function bearing(a, b) {
  const [x, y] = toXY(b, a);
  return (Math.atan2(x, y) * 180) / Math.PI;
}

/* ---------------- Wind + drift ---------------- */
async function loadWind() {
  try {
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${LAKE_CENTER[0]}&longitude=${LAKE_CENTER[1]}&hourly=wind_speed_10m,wind_direction_10m&wind_speed_unit=ms&forecast_hours=7&timezone=Asia%2FKolkata`;
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 5000);
    const j = await (await fetch(url, { signal: ctl.signal })).json();
    clearTimeout(to);
    const sp = j.hourly.wind_speed_10m.slice(0, 6), dr = j.hourly.wind_direction_10m.slice(0, 6);
    if (sp.length < 6) throw new Error("short");
    state.wind = { speed_ms: sp, dir_deg: dr, source: "Open-Meteo live" };
  } catch {
    state.wind = { ...DEMO_WIND, source: "offline sample" };
  }
  const s = state.wind.speed_ms[0], d = state.wind.dir_deg[0];
  $("#windBadge").textContent = `🌬 Wind ${s.toFixed(1)} m/s from ${compass(d)} · ${state.wind.source}`;
}
function compass(d) {
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((d % 360) / 45)) % 8];
}
function driftPath(start) {
  const pts = [start];
  let p = start;
  const w = state.wind || { ...DEMO_WIND };
  for (let h = 0; h < 6; h++) {
    const to = (w.dir_deg[h] + 180) % 360;         // wind blows *from* dir
    const meters = ASSUMPTIONS.windage * w.speed_ms[h] * 3600;
    p = move(p, to, meters);
    pts.push(p);
  }
  return pts;
}
function ferryRisk(path) {
  let best = null;
  for (const r of FERRY_ROUTES) {
    // densify path for a fair check
    for (let i = 0; i < path.length - 1; i++) {
      for (let k = 0; k <= 4; k++) {
        const t = k / 4;
        const p = [path[i][0] + (path[i + 1][0] - path[i][0]) * t, path[i][1] + (path[i + 1][1] - path[i][1]) * t];
        const d = distToRoute(p, r);
        if (d < ASSUMPTIONS.ferry_alert_m && (!best || d < best.d)) best = { route: r, d, hour: i + t };
      }
    }
  }
  return best;
}

/* ---------------- Mode ---------------- */
async function initMode() {
  try {
    const j = await (await fetch("api/status")).json();
    state.live = !!j.live;
  } catch { state.live = false; }
  setMode(state.live ? "live" : "demo");
}
function setMode(m) {
  if (m === "live" && !state.live) { toast("Live Sarvam is not configured on the server. Staying in demo mode."); m = "demo"; }
  state.mode = m;
  $("#modeLive").classList.toggle("on", m === "live");
  $("#modeDemo").classList.toggle("on", m === "demo");
  $("#modeNote").textContent = m === "live" ? "Sarvam AI live" : "Offline demo data";
}
$("#modeLive").onclick = () => setMode("live");
$("#modeDemo").onclick = () => setMode("demo");

/* ---------------- Pipeline UI ---------------- */
function step(name, status, t) {
  const li = document.querySelector(`#pipeline li[data-step="${name}"]`);
  li.className = status || "";
  li.querySelector(".t")?.remove();
  if (t) li.insertAdjacentHTML("beforeend", `<span class="t">${t}</span>`);
}
function resetPipeline() { for (const s of ["stt", "llm", "tr", "map", "tts"]) step(s, ""); }

/* ---------------- Reporting ---------------- */
let pendingPhoto = null;

async function submitReport({ audio, mime, text, demo, remote }) {
  const busy = $("#micBtn");
  busy.classList.add("busy");
  resetPipeline();
  try {
    let transcript, translation, ext, reporter = "Voice report", gps = null, fixed = null;
    if (remote) {
      // Report arrived from a boatman's phone (already processed by Sarvam on that request).
      step("stt", "done", remote.timings?.stt_ms ? `${remote.timings.stt_ms} ms` : "phone");
      step("llm", "done", remote.timings?.llm_ms ? `${remote.timings.llm_ms} ms` : "phone");
      step("tr", remote.translation ? "done" : "skip", remote.translation ? "via 105B" : "");
      transcript = remote.transcript; translation = remote.translation; ext = remote.extraction || {};
      if (remote.source === "payalcam" && !ext.area_m2) ext = { ...ext, size: "small", area_m2: camArea("🌿") };
      reporter = `${{ camera: "📹", payalcam: "🌿" }[remote.source] || "📱"} ${remote.reporter}`; gps = remote.gps; fixed = remote.fixed || (remote.source === "camera" && remote.gps) || null;
      if (remote.photo) pendingPhoto = { thumb: remote.photo, green: remote.green };
    } else if (state.mode === "demo" || demo) {
      const d = demo || DEMO_REPORTS[state.demoIdx++ % DEMO_REPORTS.length];
      step("stt", "run"); await sleep(audio ? 900 : 350);
      transcript = d.transcript; step("stt", "done", audio ? "demo" : "sample");
      step("llm", "run"); await sleep(600);
      if (state.mode === "live") {
        // Sample chip in live mode: run the real LLM + Translate on the sample text.
        step("tr", "run");
        const r = await api("api/report", { text: d.transcript });
        ext = r.extraction; step("llm", "done", `${r.timings?.llm_ms ?? "?"} ms`);
        translation = r.translation || ext.translation || d.translation;
        step("tr", translation ? "done" : "skip", translation ? (r.timings?.translate_ms ? `${r.timings.translate_ms} ms` : "via 105B") : "");
      } else {
        ext = d.extraction; step("llm", "done", "demo");
        step("tr", "run"); await sleep(250);
        translation = d.translation; step("tr", "done", "demo");
      }
      reporter = d.reporter;
    } else {
      step("stt", audio ? "run" : "skip");
      step("llm", "run"); step("tr", "run");
      const body = audio ? { audio_b64: await b64(audio), mime } : { text };
      const r = await api("api/report", body);
      transcript = r.transcript; ext = r.extraction;
      if (audio) step("stt", "done", `${r.timings?.stt_ms ?? "?"} ms`);
      step("llm", "done", ext.source === "sarvam-105b" ? `${r.timings?.llm_ms} ms` : "fallback");
      translation = r.translation || ext.translation;
      step("tr", translation ? "done" : "skip", translation ? (r.timings?.translate_ms ? `${r.timings.translate_ms} ms` : "via 105B") : "");
    }
    step("map", "run");
    const rep = await placeReport({ transcript, translation, ext, reporter, gps, fixed });
    step("map", "done", rep.alert ? "ferry risk" : "");
    renderLast(rep);
    step("tts", "run");
    const how = await speak(rep);
    step("tts", how ? "done" : "skip", how || "");
  } catch (e) {
    console.error(e);
    toast(`Report failed: ${e.message}. Switching to demo mode.`, true);
    setMode("demo");
    resetPipeline();
  } finally {
    busy.classList.remove("busy");
  }
}

async function placeReport({ transcript, translation, ext, reporter, gps, fixed }) {
  let place = PLACES.find((p) => p.id === ext.place_id);
  let lat, lng, located = "place";
  if (fixed) {
    [lat, lng] = fixed;
  } else if (place) {
    [lat, lng] = move([place.lat, place.lng], Math.random() * 360, 80 + Math.random() * 180);
  } else {
    const g = (gps && inLake(gps) && gps) || (await geolocate());
    if (g) { [lat, lng] = g; located = "gps"; place = { id: "gps", en: "Reported location (GPS)", ml: "റിപ്പോർട്ട് ചെയ്ത സ്ഥലം" }; }
    else { [lat, lng] = LAKE_CENTER; located = "unknown"; place = { id: "unknown", en: "Location unclear", ml: "കായൽ" }; }
  }
  const size = SIZES[ext.size] ? ext.size : "medium";
  const area = ext.area_m2 && ext.area_m2 > 0 ? Math.min(ext.area_m2, 40000) : SIZES[size].area_m2;
  const drift = driftPath([lat, lng]);
  const risk = ferryRisk(drift);
  const rep = {
    id: "r" + Date.now().toString(36),
    ts: Date.now(), reporter, transcript, translation, place, located,
    lat, lng, size, blocking: !!ext.blocking, est: estimate(area), drift,
    alert: risk ? { route: risk.route.id, hour: Math.max(1, Math.round(risk.hour)), d: Math.round(risk.d) } : null,
    status: "open", photo: pendingPhoto?.thumb || null, green: pendingPhoto?.green ?? null,
    source: ext.source || "demo",
  };
  rep.warning_ml = warningText(place.ml ? place : { ml: "കായൽ" }, size, risk?.route);
  pendingPhoto = null; $("#photoPreview").hidden = true;
  state.reports.unshift(rep);
  save();
  drawReport(rep, true);
  renderJobs(); renderPilot();
  map.flyTo([lat, lng], 13.5, { duration: 1.2 });
  return rep;
}

/* ---------------- Drawing ---------------- */
const drawn = {};
function drawReport(rep, isNew = false) {
  drawn[rep.id]?.forEach((l) => matLayer.removeLayer(l));
  const px = Math.round(18 + Math.sqrt(rep.est.area_m2) * 0.45);
  const cls = ["mat-icon", isNew ? "new" : "", rep.alert ? "alert" : "", rep.status === "harvested" ? "harvested" : ""].join(" ");
  const icon = L.divIcon({ className: "", html: `<div class="${cls}" style="width:${px}px;height:${px}px"></div>`, iconSize: [px, px], iconAnchor: [px / 2, px / 2] });
  const m = L.marker([rep.lat, rep.lng], { icon }).bindPopup(popupHtml(rep), { maxWidth: 280 });
  const layers = [m];
  if (rep.status !== "harvested") {
    const line = L.polyline(rep.drift, { color: "#ffb547", weight: 3, dashArray: "6 6", opacity: 0.95 });
    const end = rep.drift[rep.drift.length - 1];
    const ang = bearing(rep.drift[rep.drift.length - 2], end);
    const head = L.marker(end, { interactive: false, icon: L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9], html: `<svg width="18" height="18" viewBox="0 0 18 18" style="transform:rotate(${ang}deg)"><path d="M9 1 L16 16 L9 12 L2 16 Z" fill="#ffb547"/></svg>` }) });
    const ghost = L.circleMarker(end, { radius: 5, color: "#ffb547", fillOpacity: 0.2, weight: 1 }).bindTooltip("+6 h", { permanent: false });
    layers.push(line, head, ghost);
    if (rep.alert) ferryLines[rep.alert.route]?.setStyle({ color: "#ff6b5b", weight: 5 });
  }
  layers.forEach((l) => l.addTo(matLayer));
  drawn[rep.id] = layers;
  if (isNew) m.openPopup();
}
function refreshFerryColors() {
  for (const r of FERRY_ROUTES) ferryLines[r.id].setStyle({ color: "#5cc8ff", weight: 3 });
  for (const rep of state.reports) if (rep.alert && rep.status !== "harvested") ferryLines[rep.alert.route]?.setStyle({ color: "#ff6b5b", weight: 5 });
}
function popupHtml(rep) {
  const route = rep.alert && FERRY_ROUTES.find((r) => r.id === rep.alert.route);
  return `<b>${esc(rep.place.en)}</b> · ${SIZES[rep.size].en} mat<br>
  <span class="ml" style="color:#e8f3ef">${esc(rep.transcript)}</span><br>
  ${rep.translation ? `<i style="color:#8fb3ab">${esc(rep.translation)}</i><br>` : ""}
  ~${rep.est.wet_t} t wet · ${rep.est.pellet_kg} kg pellets · ₹${rep.est.harvest_rs.toLocaleString("en-IN")} for harvesters <span style="color:#8fb3ab">(est.)</span>
  ${route ? `<br><b style="color:#ff6b5b">⚠ May reach ${esc(route.en)} in ~${rep.alert.hour} h</b>` : ""}
  ${rep.photo ? `<img src="${rep.photo}" alt="report photo">${rep.green != null ? `<div style="color:#8fb3ab">Green cover in photo: ${rep.green}%</div>` : ""}` : ""}
  <div style="margin-top:6px"><button class="btn small" onclick="window.kr.replay('${rep.id}')">🔊 Play warning</button></div>`;
}
function renderLast(rep) {
  const route = rep.alert && FERRY_ROUTES.find((r) => r.id === rep.alert.route);
  const el = $("#lastReport");
  el.hidden = false;
  el.innerHTML = `
    <div class="ml-text">“${esc(rep.transcript)}”</div>
    ${rep.translation ? `<div class="en">${esc(rep.translation)}</div>` : ""}
    <div class="tagrow">
      <span class="t-tag">📍 ${esc(rep.place.en)}</span>
      <span class="t-tag">${SIZES[rep.size].en} · ~${rep.est.wet_t} t</span>
      ${rep.blocking ? `<span class="t-tag alert">Boats blocked</span>` : ""}
      ${route ? `<span class="t-tag alert">⚠ ${esc(route.en)} in ~${rep.alert.hour} h</span>` : ""}
    </div>
    <div class="ml-text" style="font-size:13px;color:#8fb3ab">🔊 ${esc(rep.warning_ml)}</div>`;
}

/* ---------------- Jobs + pilot ---------------- */
function renderJobs() {
  const ul = $("#jobs");
  if (!state.reports.length) { ul.innerHTML = `<li class="empty">No reports yet. Speak or tap a sample above.</li>`; return; }
  ul.innerHTML = state.reports.map((r) => {
    const next = r.status === "open" ? `<button class="btn small" data-act="assign" data-id="${r.id}">Assign crew</button>`
      : r.status === "assigned" ? `<button class="btn small warn" data-act="slip" data-id="${r.id}">📄 Scan slip</button><button class="btn small ghost" data-act="harvest" data-id="${r.id}">Done</button>` : "";
    return `<li class="job" data-id="${r.id}">
      <div class="job-h"><span>${esc(r.place.en)}</span><span class="st ${r.status}">${r.status}</span></div>
      <div class="job-n"><span><b>${r.weighed ? "" : "~"}${r.est.wet_t} t</b> wet ${r.weighed ? `<span class="t-tag weighed">weighed</span>` : ""}</span><span><b>${r.est.pellet_kg}</b> kg pellets</span><span><b>₹${r.est.harvest_rs.toLocaleString("en-IN")}</b> to crew</span></div>
      <div class="row"><span class="muted small">${time(r.ts)} · ${esc(r.reporter)}${r.status === "assigned" ? " · Crew: Kainakary Kudumbashree unit" : ""}</span>${next}</div>
    </li>`;
  }).join("");
}
$("#jobs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  const li = e.target.closest(".job");
  if (b) {
    e.stopPropagation();
    const r = state.reports.find((x) => x.id === b.dataset.id);
    if (b.dataset.act === "slip") { slipFor = r; $("#slipInput").click(); return; }
    r.status = b.dataset.act === "assign" ? "assigned" : "harvested";
    if (r.status === "harvested") toast(`✅ ${r.est.wet_t} t harvested at ${r.place.en}. ₹${r.est.harvest_rs.toLocaleString("en-IN")} credited to the crew (est.).`);
    save(); drawReport(r); refreshFerryColors(); renderJobs(); renderPilot();
  } else if (li) {
    const r = state.reports.find((x) => x.id === li.dataset.id);
    map.flyTo([r.lat, r.lng], 14); drawn[r.id]?.[0]?.openPopup();
  }
});
/* ---------------- Harvest slip (Sarvam Vision) ---------------- */
let slipFor = null;
$("#slipInput").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f || !slipFor) return;
  const r = slipFor; slipFor = null;
  toast("📄 Sarvam Vision is reading the slip…");
  let res;
  try {
    if (state.mode !== "live") throw new Error("demo");
    const small = await shrink(f, 1600);
    const out = await api("api/slip", { image_b64: small.split(",")[1], mime: "image/jpeg" });
    res = out.result; res.ms = out.ms;
  } catch (err) {
    if (state.mode === "live") toast(`Vision failed (${err.message}). Using demo slip values.`, true);
    res = { wet_weight_kg: Math.round(r.est.wet_t * 1000 * (0.8 + Math.random() * 0.3) / 10) * 10, crew: "Kainakary Kudumbashree unit", date: new Date().toLocaleDateString("en-IN") };
  }
  const kg = Number(res.wet_weight_kg);
  if (kg > 0) {
    r.est = estimate(1); // reshape using real weight
    const A = ASSUMPTIONS, wet_t = kg / 1000;
    r.est = { area_m2: r.est.area_m2, wet_t: Math.round(wet_t * 10) / 10, dry_kg: Math.round(kg * A.dry_matter_fraction), pellet_kg: Math.round(kg * A.dry_matter_fraction * A.pellet_kg_per_dry_kg), harvest_rs: Math.round(wet_t * A.harvest_rs_per_wet_t), pellet_rs: Math.round(kg * A.dry_matter_fraction * A.pellet_kg_per_dry_kg * A.pellet_rs_per_kg), mushroom_kg: Math.round(kg * A.dry_matter_fraction * (A.mushroom_kg_per_dry_kg || 0)) };
    r.weighed = true;
  }
  r.status = "harvested"; r.slip = res;
  toast(`✅ Slip read${res.ms ? ` in ${(res.ms / 1000).toFixed(1)} s` : ""}: ${kg ? kg.toLocaleString("en-IN") + " kg" : "weight not found"}${res.crew ? " · " + res.crew : ""}. ₹${r.est.harvest_rs.toLocaleString("en-IN")} to the crew.`);
  save(); drawReport(r); refreshFerryColors(); renderJobs(); renderPilot();
};
async function shrink(file, max) {
  const img = await createImageBitmap(file);
  const k = Math.min(1, max / Math.max(img.width, img.height));
  const c = document.createElement("canvas"); c.width = img.width * k; c.height = img.height * k;
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.85);
}

/* ---------------- Officials' brief ---------------- */
const DEMO_BRIEF = {
  en: "Vembanad hyacinth brief (estimates). Reports today flag mats near Punnamada and Muhamma; the Muhamma mat is forecast to drift onto the Muhamma–Kumarakom ferry route within about an hour. Harvest crews are assigned at Kainakary. The MushPellets pilot has logged roughly 40 of 100 tonnes. Recommended action: deploy the Muhamma crew before the evening ferry runs.",
  ml: "വേമ്പനാട് കുളവാഴ റിപ്പോർട്ട് (ഏകദേശ കണക്കുകൾ). പുന്നമടയിലും മുഹമ്മയിലും കുളവാഴക്കൂട്ടങ്ങൾ റിപ്പോർട്ട് ചെയ്തു. മുഹമ്മയിലെ കൂട്ടം ഏകദേശം ഒരു മണിക്കൂറിനുള്ളിൽ മുഹമ്മ–കുമരകം ബോട്ട് പാതയിലെത്താൻ സാധ്യതയുണ്ട്. വൈകുന്നേരത്തെ ബോട്ട് സർവീസിന് മുമ്പ് മുഹമ്മയിൽ വിളവെടുപ്പ് സംഘത്തെ വിന്യസിക്കണം.",
};
$("#briefBtn").onclick = async () => {
  const box = $("#brief"); box.hidden = false; box.innerHTML = `<span class="muted">Writing brief…</span>`;
  let b;
  try {
    if (state.mode !== "live") throw new Error("demo");
    b = await api("api/brief", { reports: state.reports.map((r) => ({ place: r.place.en, size: r.size, wet_t: r.est.wet_t, status: r.status, alert: r.alert && FERRY_ROUTES.find((x) => x.id === r.alert.route)?.en })) });
  } catch { b = DEMO_BRIEF; }
  box.innerHTML = `<div class="en" style="color:var(--ink)">${esc(b.en)}</div>${b.ml ? `<div class="ml-text" style="font-size:13px">${esc(b.ml)}</div><div><button class="btn small ghost" id="briefSpeak">🔊 Read in Malayalam</button></div>` : ""}`;
  $("#briefSpeak")?.addEventListener("click", async () => {
    try { if (state.mode !== "live") throw 0; const { audio_b64 } = await api("api/tts", { text: b.ml.slice(0, 2400) }); play(`data:audio/wav;base64,${audio_b64}`); }
    catch { const h = await fetch("audio/brief.wav", { method: "HEAD" }).catch(() => null); if (h?.ok) play("audio/brief.wav"); else toast("Voice needs live mode."); }
  });
};

function renderPilot() {
  const done = state.reports.filter((r) => r.status === "harvested");
  const queued = state.reports.filter((r) => r.status !== "harvested");
  const sum = (a, k) => a.reduce((s, r) => s + r.est[k], 0);
  const doneT = sum(done, "wet_t"), qT = sum(queued, "wet_t"), T = ASSUMPTIONS.pilot_target_wet_t;
  $("#pilotHarvested").textContent = doneT.toFixed(1).replace(/\.0$/, "");
  $("#pilotPipeline").textContent = `${qT.toFixed(1).replace(/\.0$/, "")} t reported and waiting`;
  $("#barDone").style.width = Math.min(100, (doneT / T) * 100) + "%";
  $("#barQueued").style.width = Math.min(100 - Math.min(100, (doneT / T) * 100), (qT / T) * 100) + "%";
  $("#kPellet").textContent = sum(done, "pellet_kg").toLocaleString("en-IN");
  $("#kPaid").textContent = "₹" + sum(done, "harvest_rs").toLocaleString("en-IN");
  $("#kMush").textContent = sum(done, "mushroom_kg").toLocaleString("en-IN");
}

/* ---------------- Voice out ---------------- */
const CONFIRM_ML = "നന്ദി. നിങ്ങളുടെ റിപ്പോർട്ട് രേഖപ്പെടുത്തി. വിളവെടുപ്പ് സംഘത്തെ അറിയിച്ചിട്ടുണ്ട്.";
let confirmAudio = null;
async function speak(rep, { confirm = true } = {}) {
  // 1) live Bulbul, 2) pre-baked Bulbul audio for demo reports, 3) browser speech
  if (state.mode === "live") {
    try {
      const t = Date.now();
      const [c, w] = await Promise.all([
        confirm ? (confirmAudio ? Promise.resolve({ audio_b64: confirmAudio }) : api("api/tts", { text: CONFIRM_ML, speaker: "kavitha" })) : null,
        api("api/tts", { text: rep.warning_ml }),
      ]);
      const ms = Date.now() - t;
      if (c) { confirmAudio = c.audio_b64; await playWait(`data:audio/wav;base64,${c.audio_b64}`); }
      rep.audio = w.audio_b64; await play(`data:audio/wav;base64,${w.audio_b64}`);
      return `${ms} ms`;
    } catch (e) { console.warn("TTS failed", e); }
  }
  if (confirm) { try { const h = await fetch("audio/confirm.wav", { method: "HEAD" }); if (h.ok) await playWait("audio/confirm.wav"); } catch {} }
  const baked = `audio/${rep.place.id}_${rep.size}_${rep.alert ? rep.alert.route : "none"}.wav`;
  try {
    const h = await fetch(baked, { method: "HEAD" });
    if (h.ok) { await play(baked); return "Bulbul (cached)"; }
  } catch {}
  if ("speechSynthesis" in window) {
    const u = new SpeechSynthesisUtterance(rep.warning_ml); u.lang = "ml-IN";
    speechSynthesis.speak(u); return "browser voice";
  }
  return null;
}
function playWait(src) {
  return new Promise((res) => { const a = new Audio(src); a.onended = res; a.onerror = res; a.play().catch(res); });
}
function play(src) {
  const a = $("#player"); a.src = src;
  return a.play().catch(() => toast("Tap ▶ Play warning on the pin to hear it (browser blocked autoplay)."));
}
window.kr = {
  replay: async (id) => { const r = state.reports.find((x) => x.id === id); if (r.audio) play(`data:audio/wav;base64,${r.audio}`); else speak(r, { confirm: false }); },
  reset: () => { state.reports = []; save(); location.reload(); },
};

/* ---------------- Mic ---------------- */
let rec = null, chunks = [], recTimer = null;
$("#micBtn").onclick = async () => {
  if ($("#micBtn").classList.contains("busy")) return;
  if (rec && rec.state === "recording") { rec.stop(); return; }
  if (state.mode === "demo" && !navigator.mediaDevices) { submitReport({}); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    chunks = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      clearTimeout(recTimer);
      $("#micBtn").classList.remove("rec");
      $("#micLabel").innerHTML = `Tap to speak in Malayalam<br><span class="ml">മലയാളത്തിൽ പറയൂ</span>`;
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      if (blob.size < 2000) { toast("Didn't catch that. Tap, speak, then tap again."); return; }
      submitReport({ audio: blob, mime: blob.type });
    };
    rec.start();
    $("#micBtn").classList.add("rec");
    $("#micLabel").innerHTML = `Listening… tap again to send<br><span class="ml">കേൾക്കുന്നു…</span>`;
    recTimer = setTimeout(() => rec.state === "recording" && rec.stop(), 25000);
  } catch (e) {
    toast("Microphone not available. Use a sample or type the report.");
  }
};

/* ---------------- Type + samples + photo ---------------- */
$("#typeToggle").onclick = () => { $("#typeForm").hidden = !$("#typeForm").hidden; if (!$("#typeForm").hidden) $("#typeInput").focus(); };
$("#typeForm").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#typeInput").value.trim(); if (!text) return;
  $("#typeInput").value = "";
  if (state.mode === "demo") {
    const hit = DEMO_REPORTS.find((d) => d.extraction.place_id === guessPlace(text));
    submitReport({ demo: { reporter: "Typed report", transcript: text, translation: null, extraction: { place_id: guessPlace(text), size: guessSize(text), blocking: false } } });
  } else submitReport({ text });
};
function guessPlace(t) { const s = t.toLowerCase(); return PLACES.find((p) => p.aliases.some((a) => s.includes(a.toLowerCase())))?.id || null; }
function guessSize(t) { return /വളരെ വലിയ|huge/i.test(t) ? "huge" : /വലിയ|large|big/i.test(t) ? "large" : /ചെറിയ|small/i.test(t) ? "small" : "medium"; }

$("#sampleChips").innerHTML = DEMO_REPORTS.map((d, i) => `<button class="chip" data-i="${i}">${esc(PLACES.find((p) => p.id === d.extraction.place_id).en.split(" (")[0])}</button>`).join("");
$("#sampleChips").onclick = (e) => { const b = e.target.closest(".chip"); if (b) submitReport({ demo: DEMO_REPORTS[+b.dataset.i] }); };

$("#photoInput").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const { thumb, green } = await analysePhoto(f);
  pendingPhoto = { thumb, green };
  const pv = $("#photoPreview"); pv.hidden = false;
  pv.innerHTML = `<img src="${thumb}" alt=""><div>Photo attached. Green cover ≈ <b>${green}%</b><br>Now speak the report.</div>`;
  e.target.value = "";
};
async function analysePhoto(file) {
  const img = await createImageBitmap(file);
  const W = 360, H = Math.round((img.height / img.width) * W);
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const g = c.getContext("2d"); g.drawImage(img, 0, 0, W, H);
  const d = g.getImageData(0, 0, W, H).data;
  let n = 0;
  for (let i = 0; i < d.length; i += 4) { const r = d[i], gg = d[i + 1], b = d[i + 2]; if (gg > 55 && gg > r * 1.08 && gg > b * 1.08) n++; }
  return { thumb: c.toDataURL("image/jpeg", 0.7), green: Math.round((n / (W * H)) * 100) };
}

/* ---------------- Assumptions dialog ---------------- */
$("#showAssumptions").onclick = (e) => {
  e.preventDefault();
  const A = ASSUMPTIONS;
  const N = A.notes || {};
  const row = (label, val, k) => `<tr><td>${label}${N[k] ? `<div class="muted small">${esc(N[k])}</div>` : ""}</td><td>${val}</td></tr>`;
  $("#assumptionsBody").innerHTML = `<table>
    ${row("Fresh biomass of a dense mat", `${A.wet_kg_per_m2} kg/m²`, "wet_kg_per_m2")}
    ${row("Dry matter", `${Math.round(A.dry_matter_fraction * 100)}%`, "dry_matter_fraction")}
    ${row("Substrate pellets", `${A.pellet_kg_per_dry_kg} kg per dry kg`, "pellet_kg_per_dry_kg")}
    ${row("Oyster mushrooms", `${A.mushroom_kg_per_dry_kg} kg per dry kg`, "mushroom_kg_per_dry_kg")}
    ${row("Paid to harvesting group", `₹${A.harvest_rs_per_wet_t}/wet t`, "harvest_rs_per_wet_t")}
    ${row("Indicative pellet value", `₹${A.pellet_rs_per_kg}/kg`, "")}
    ${row("Mat sizes (S / M / L / XL)", `${Object.values(SIZES).map((s) => s.area_m2).join(" / ")} m²`, "")}
    ${row("Drift speed", `${A.windage * 100}% of wind`, "windage")}
    ${row("Ferry alert distance", `${A.ferry_alert_m} m`, "")}
    ${row("Pilot target", `${A.pilot_target_wet_t} t wet (≈5,000 m² of solid mat)`, "")}</table>
    <p class="muted small">Indicative planning figures for the MushPellets pilot, to be calibrated by field weighing. <a href="#" onclick="window.kr.reset();return false">Reset demo data</a></p>`;
  $("#assumptions").showModal();
};

/* ---------------- Helpers ---------------- */
async function api(url, body) {
  const ctl = new AbortController(); const to = setTimeout(() => ctl.abort(), 30000);
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal });
  clearTimeout(to);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function b64(blob) { return new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(fr.result.split(",")[1]); fr.readAsDataURL(blob); }); }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function time(ts) { return new Date(ts).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }); }
let toastT;
function toast(msg, alert = false) { const t = $("#toast"); t.textContent = msg; t.className = "toast" + (alert ? " alert" : ""); t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 4500); }
function inLake([a, o]) { return a > 9.35 && a < 9.8 && o > 76.25 && o < 76.55; }
function geolocate() {
  return new Promise((res) => {
    if (!navigator.geolocation) return res(null);
    navigator.geolocation.getCurrentPosition((p) => {
      const { latitude: a, longitude: o } = p.coords;
      res(a > 9.35 && a < 9.8 && o > 76.25 && o < 76.55 ? [a, o] : null);
    }, () => res(null), { timeout: 4000 });
  });
}
function save() { try { localStorage.setItem(STORE_KEY, JSON.stringify(state.reports.map(({ audio, ...r }) => r))); } catch {} }
function load() {
  try { state.reports = JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); } catch { state.reports = []; }
  // Older camera / live-cam pins used the 600 m² "medium" mat; re-estimate them from what the camera saw.
  let fixed = 0;
  for (const r of state.reports) {
    if (r.camFix !== 2 && /^(📹|🌿)/.test(r.reporter || "")) { r.size = "small"; r.est = estimate(camArea(r.reporter, r.green)); r.camFix = 2; fixed++; }
  }
  if (fixed) save();
}
/* A camera sees only the water near the boat: about CAM_VIEW_M2 m² inside the lake box.
   Visible mat area = that × the green fraction, so 30% cover ≈ 8 m² ≈ 150 kg wet. A PayalCam viewer's
   tap has no measurement, so it counts as a small patch (5 m² ≈ 100 kg). */
const CAM_VIEW_M2 = 25;
function camArea(reporter, green) {
  if (/^🌿/.test(reporter || "")) return 5;
  return Math.max(2, Math.round(((green ?? 25) / 100) * CAM_VIEW_M2));
}

/* ---------------- Live feed from phones ---------------- */
let feedSeq = null, feedQueue = [], feedBusy = false;
const ownFeedSeqs = new Set();
async function pollFeed() {
  try {
    const j = await (await fetch(`api/feed?since=${feedSeq ?? 0}`)).json();
    if (feedSeq === null) { feedSeq = j.seq; }            // ignore backlog on first load
    else { for (const it of j.items) { if (!ownFeedSeqs.has(it.seq)) feedQueue.push(it); feedSeq = Math.max(feedSeq, it.seq); } }
  } catch {}
  if (!feedBusy && feedQueue.length) {
    feedBusy = true;
    const it = feedQueue.shift();
    toast(it.source === "camera" ? `📹 ${it.reporter} auto-report` : it.source === "payalcam" ? `🌿 ${it.reporter} reported from the live cam` : `📱 New report from ${it.reporter}`);
    await submitReport({ remote: it });
    feedBusy = false;
  }
}
setInterval(pollFeed, 2000);

/* ---------------- QR: report from your phone ---------------- */
function drawQR(base) {
  const url = new URL("report.html", base || location.href).href;
  const box = $("#qrBox");
  if (!window.qrcode || !box) return;
  const q = qrcode(0, "M"); q.addData(url); q.make();
  box.querySelector(".qr").innerHTML = q.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  box.querySelector(".qr-url").textContent = url.replace(/^https?:\/\//, "");
}
$("#qrToggle")?.addEventListener("click", () => $("#qrBox").classList.toggle("min"));

/* ---------------- Weed Watch camera ----------------
   Detection runs here, in the browser, on each frame from api/cam (or the laptop webcam with ?cam=webcam).
   - Only pixels inside the "lake area" box count (drag on the tile to redraw it).
   - "Ignore boat" learns the green that is in view now (boat roof, plants) and masks it out.
   - Night / IR (grey picture) and heavy glare pause detection instead of guessing.
   - A mat must stay above the threshold for 3 frames, and after a report the camera re-arms only
     when the mat leaves (cover drops well below) or grows a lot after 10 minutes. No spam.
   - Only the laptop's own screen (localhost, or ?reporter=1) files reports; other viewers just watch. */
const CW = 192;                                   // analysis width (fast, enough for mats)
const camLS = (k, v) => { try { if (v === undefined) return JSON.parse(localStorage.getItem("kr.cam." + k)); localStorage.setItem("kr.cam." + k, JSON.stringify(v)); } catch { return null; } };
const qs = new URLSearchParams(location.search);
const CAM = {
  place: "punnamada", fixed: null, threshold: +(qs.get("thr") || camLS("thr") || 30),
  roi: camLS("roi") || { x0: 0, y0: 0.4, x1: 1, y1: 1 },
  ignore: null, hist: [], streak: 0, armed: true, lastReport: null,
  moments: camLS("moments") || [], source: qs.get("cam") === "webcam" ? "webcam" : "api",
  reporter: qs.get("reporter") === "1" || ["localhost", "127.0.0.1"].includes(location.hostname),
  frame: null, W: 0, H: 0, started: false,
};
{ const ig = camLS("ignore"); if (ig) CAM.ignore = { w: ig.w, h: ig.h, bits: Uint8Array.from(atob(ig.b), (c) => c.charCodeAt(0)) }; }

function hsv(r, g, b) {
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) h = mx === r ? 60 * (((g - b) / d) % 6) : mx === g ? 60 * ((b - r) / d + 2) : 60 * ((r - g) / d + 4);
  return [(h + 360) % 360, mx ? d / mx : 0, mx / 255];
}

function analyseFrame(src) {
  const sw = src.videoWidth || src.naturalWidth || src.width, sh = src.videoHeight || src.naturalHeight || src.height;
  const W = CW, H = Math.max(1, Math.round((sh / sw) * W));
  const c = analyseFrame.c || (analyseFrame.c = document.createElement("canvas"));
  c.width = W; c.height = H;
  const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(src, 0, 0, W, H);
  const d = g.getImageData(0, 0, W, H).data;
  const mask = new Uint8Array(W * H);           // 1 green, 2 ignored, 3 glare, 0 other
  const { x0, y0, x1, y1 } = CAM.roi;
  const rx0 = Math.floor(x0 * W), rx1 = Math.ceil(x1 * W), ry0 = Math.floor(y0 * H), ry1 = Math.ceil(y1 * H);
  const ig = CAM.ignore && CAM.ignore.w === W && CAM.ignore.h === H ? CAM.ignore.bits : null;
  let valid = 0, green = 0, glare = 0, sumV = 0, sumS = 0, n = 0, bx0 = W, by0 = H, bx1 = -1, by1 = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = y * W + x, p = i * 4, r = d[p], gg = d[p + 1], b = d[p + 2];
    const [h, s, v] = hsv(r, gg, b);
    sumV += v; sumS += s; n++;
    if (x < rx0 || x >= rx1 || y < ry0 || y >= ry1) continue;
    if (ig && ig[i]) { mask[i] = 2; continue; }
    if (v > 0.88 && s < 0.18) { mask[i] = 3; glare++; continue; }   // sun glint / sky reflection
    valid++;
    // Water hyacinth: yellow-green to green leaves, reasonably saturated, not in shadow.
    if (h >= 62 && h <= 160 && s > 0.2 && v > 0.13 && gg > r && gg >= b) {
      mask[i] = 1; green++;
      if (x < bx0) bx0 = x; if (x > bx1) bx1 = x; if (y < by0) by0 = y; if (y > by1) by1 = y;
    }
  }
  const meanV = sumV / n, meanS = sumS / n, roiN = Math.max(1, (rx1 - rx0) * (ry1 - ry0));
  let mode = "ok";
  if (meanV < 0.12) mode = "dark";
  else if (meanS < 0.05) mode = "ir";
  else if (glare / roiN > 0.45) mode = "glare";
  const cover = valid > roiN * 0.2 ? Math.round((green / valid) * 100) : 0;
  return { W, H, mask, cover: mode === "ok" ? cover : 0, rawCover: cover, mode, box: bx1 >= 0 ? [bx0, by0, bx1, by1] : null };
}

function drawOverlay(a) {
  const cv = $("#camOverlay"); if (!cv) return;
  cv.width = a.W; cv.height = a.H;
  const g = cv.getContext("2d"), img = g.createImageData(a.W, a.H);
  for (let i = 0; i < a.mask.length; i++) {
    const m = a.mask[i], p = i * 4;
    if (m === 1) { img.data[p] = 57; img.data[p + 1] = 255; img.data[p + 2] = 136; img.data[p + 3] = 150; }
    else if (m === 2) { img.data[p] = 255; img.data[p + 1] = 80; img.data[p + 2] = 80; img.data[p + 3] = 35; }
  }
  g.putImageData(img, 0, 0);
  const { x0, y0, x1, y1 } = CAM.roi;
  g.setLineDash([4, 3]); g.strokeStyle = "rgba(255,255,255,.7)"; g.lineWidth = 1;
  g.strokeRect(x0 * a.W + 0.5, y0 * a.H + 0.5, (x1 - x0) * a.W - 1, (y1 - y0) * a.H - 1);
  if (a.box && a.cover >= CAM.threshold * 0.5) {
    const [bx0, by0, bx1, by1] = a.box;
    g.setLineDash([]); g.strokeStyle = "#39ff88"; g.lineWidth = 1.5;
    g.strokeRect(bx0, by0, bx1 - bx0, by1 - by0);
  }
}

function camState(a) {
  if (a.mode === "dark") return ["Too dark. Detection paused until dawn.", "paused"];
  if (a.mode === "ir") return ["Night IR mode (no colour). Paused.", "paused"];
  if (a.mode === "glare") return ["Sun glare on water. Paused.", "paused"];
  if (!CAM.armed) return [`Reported. Re-arms when the mat moves on.`, "done"];
  if (CAM.streak > 0) return [`Mat detected, confirming ${CAM.streak}/3`, "warn"];
  return ["Watching the lake", "ok"];
}

async function camTick(src) {
  const a = analyseFrame(src);
  CAM.frame = a;
  CAM.hist = [...CAM.hist.slice(-29), a.cover];
  const sm = [...CAM.hist.slice(-3)].sort((x, y) => x - y)[Math.min(1, CAM.hist.length - 1)] ?? 0; // median of 3
  drawOverlay(a);
  // Debounce + hysteresis
  if (a.mode !== "ok") CAM.streak = 0;
  else if (CAM.armed && sm >= CAM.threshold) CAM.streak++;
  else CAM.streak = 0;
  if (!CAM.armed && CAM.lastReport) {
    const gone = sm < CAM.threshold * 0.6;
    const grew = Date.now() - CAM.lastReport.ts > 600000 && sm >= CAM.lastReport.cover + 15;
    if (gone || grew) CAM.armed = true;
  }
  // Report without blocking the frame loop (the voice warning can take a few seconds).
  if (CAM.streak >= 3) { CAM.streak = 0; camReport(src, sm).catch((e) => console.error(e)); }
  const [msg, cls] = camState(a);
  $("#camPct").textContent = a.mode === "ok" ? `${sm}%` : "–";
  $("#camPct").className = "cam-pct " + (sm >= CAM.threshold && a.mode === "ok" ? "hot" : "");
  $("#camState").textContent = msg; $("#camState").className = "cam-state " + cls;
  $("#camStat").innerHTML = `alert at ${CAM.threshold}% · ${spark(CAM.hist)}${CAM.reporter ? "" : " · view only"}`;
}

async function camReport(src, cover) {
  CAM.armed = false; CAM.lastReport = { ts: Date.now(), cover };
  const thumb = camSnapshot(src);
  CAM.moments = [{ ts: Date.now(), cover, thumb: camSnapshot(src, 160) }, ...CAM.moments].slice(0, 4);
  camLS("moments", CAM.moments); renderMoments();
  const flash = $("#camFlash"); flash.hidden = false; flash.classList.remove("go"); void flash.offsetWidth; flash.classList.add("go");
  setTimeout(() => (flash.hidden = true), 4000);
  if (!CAM.reporter) return;
  const size = "small";                                  // a window camera sees a patch, not a lake-wide mat
  const p = PLACES.find((x) => x.id === CAM.place) || PLACES[0];
  toast(`📹 Weed Watch camera: ${cover}% hyacinth cover near ${p.en}. Auto-reporting.`, true);
  const item = { reporter: "Weed Watch camera", source: "camera",
    transcript: `ക്യാമറ: ${p.ml} ഭാഗത്ത് ${SIZES[size].ml} കുളവാഴക്കൂട്ടം (${cover}% പച്ച)`,
    translation: `Camera: ${SIZES[size].en.toLowerCase()} hyacinth mat detected near ${p.en} (${cover}% of the lake in view).`,
    extraction: { place_id: p.id, size, blocking: false, area_m2: camArea("📹", cover) }, photo: thumb, green: cover, gps: CAM.fixed };
  // Share with every screen and phone via the live feed; this screen skips its own item.
  fetch("api/feed", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(item) })
    .then((r) => r.json()).then((j) => { if (j.seq) ownFeedSeqs.add(j.seq); }).catch(() => {});
  await submitReport({ remote: { ...item, fixed: CAM.fixed } });
}

function camSnapshot(src, w = 480) {
  const sw = src.videoWidth || src.naturalWidth || src.width, sh = src.videoHeight || src.naturalHeight || src.height;
  const c = document.createElement("canvas"); c.width = w; c.height = Math.round((sh / sw) * w);
  const g = c.getContext("2d"); g.drawImage(src, 0, 0, c.width, c.height);
  g.drawImage($("#camOverlay"), 0, 0, c.width, c.height);
  g.fillStyle = "rgba(0,0,0,.55)"; g.fillRect(0, c.height - 22, c.width, 22);
  g.fillStyle = "#39ff88"; g.font = "bold 13px system-ui"; g.fillText(`KULAVAZHA ${CAM.hist.at(-1) ?? ""}% · ${new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`, 8, c.height - 7);
  return c.toDataURL("image/jpeg", 0.72);
}

function renderMoments() {
  const el = $("#camMoments"); if (!el) return;
  el.innerHTML = CAM.moments.map((m) => `<figure><img src="${m.thumb}" alt=""><figcaption>✓ ${time(m.ts)} · ${m.cover}%</figcaption></figure>`).join("");
  el.hidden = !CAM.moments.length;
}

async function camLoop() {
  if (CAM.started) return; CAM.started = true;
  $("#camCard").hidden = false; renderMoments();
  if (CAM.source === "webcam") {
    const v = $("#camVideo");
    try { v.srcObject = await navigator.mediaDevices.getUserMedia({ video: { width: 640 } }); await v.play(); }
    catch { $("#camState").textContent = "Webcam blocked"; return; }
    v.hidden = false; $("#camImg").hidden = true;
    setInterval(() => v.videoWidth && camTick(v), 2000);
    return;
  }
  const img = $("#camImg");
  const next = async () => {
    try {
      const r = await fetch(`api/cam?t=${Date.now()}`);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
      const url = URL.createObjectURL(await r.blob());
      await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
      URL.revokeObjectURL(camLoop.prev || ""); camLoop.prev = url;
      $("#camLive").className = "cam-live on";
      await camTick(img);
      setTimeout(next, 2000);
    } catch (e) {
      $("#camLive").className = "cam-live";
      $("#camState").textContent = `Camera offline (${e.message}). Retrying…`; $("#camState").className = "cam-state paused";
      setTimeout(next, 5000);
    }
  };
  next();
}

/* Tile controls: drag to draw the lake area, learn the boat, threshold, big-screen view */
(() => {
  const wrap = $("#camWrap"); if (!wrap) return;
  let drag = null;
  const pt = (e) => { const r = wrap.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]; };
  wrap.addEventListener("pointerdown", (e) => { if (!wrap.classList.contains("edit")) return; drag = pt(e); wrap.setPointerCapture(e.pointerId); });
  wrap.addEventListener("pointermove", (e) => {
    if (!drag) return; const [x, y] = pt(e);
    CAM.roi = { x0: Math.min(drag[0], x), y0: Math.min(drag[1], y), x1: Math.max(drag[0], x), y1: Math.max(drag[1], y) };
    if (CAM.frame) drawOverlay(CAM.frame);
  });
  wrap.addEventListener("pointerup", () => {
    if (!drag) return; drag = null;
    if (CAM.roi.x1 - CAM.roi.x0 < 0.05 || CAM.roi.y1 - CAM.roi.y0 < 0.05) CAM.roi = { x0: 0, y0: 0.4, x1: 1, y1: 1 };
    camLS("roi", CAM.roi); wrap.classList.remove("edit"); $("#camRoiBtn").classList.remove("on");
  });
  $("#camRoiBtn").onclick = () => { wrap.classList.toggle("edit"); $("#camRoiBtn").classList.toggle("on"); toast("Drag on the picture to box the lake water only."); };
  $("#camBoatBtn").onclick = () => {
    const a = CAM.frame; if (!a) return;
    // Grow the current green by 2 px so edges of the boat roof are covered too.
    const bits = new Uint8Array(a.W * a.H);
    for (let y = 0; y < a.H; y++) for (let x = 0; x < a.W; x++) if (a.mask[y * a.W + x] === 1 || (CAM.ignore?.bits?.[y * a.W + x]))
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) { const yy = y + dy, xx = x + dx; if (yy >= 0 && yy < a.H && xx >= 0 && xx < a.W) bits[yy * a.W + xx] = 1; }
    CAM.ignore = { w: a.W, h: a.H, bits };
    camLS("ignore", { w: a.W, h: a.H, b: btoa(String.fromCharCode(...bits)) });
    toast("Learned the green in view now (boat, plants). It will be ignored.");
  };
  $("#camClearBtn").onclick = () => { CAM.ignore = null; camLS("ignore", null); CAM.moments = []; camLS("moments", []); CAM.armed = true; renderMoments(); toast("Camera mask and moments cleared."); };
  $("#camThr").value = CAM.threshold; $("#camThrV").textContent = CAM.threshold + "%";
  $("#camThr").oninput = (e) => { CAM.threshold = +e.target.value; $("#camThrV").textContent = CAM.threshold + "%"; camLS("thr", CAM.threshold); };
  $("#camBigBtn").onclick = () => $("#camCard").classList.toggle("big");
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#camCard").classList.remove("big"); });
})();
function spark(a) { const bars = "▁▂▃▄▅▆▇█"; return a.slice(-20).map((v) => bars[Math.min(7, Math.floor(v / 12.5))]).join(""); }

/* ---------------- Boot ---------------- */
load();
state.reports.slice().reverse().forEach((r) => drawReport(r));
refreshFerryColors(); renderJobs(); renderPilot();
loadWind();
initMode();
drawQR();
fetch("api/status").then((r) => r.json()).then((j) => { if (j.public_url) drawQR(j.public_url.replace(/\/?$/, "/")); if (j.camera || CAM.source === "webcam") {
    fetch("api/cam/status").then((r) => r.json()).then((c) => { CAM.place = c.place || CAM.place; if (c.lat && c.lng) CAM.fixed = [+c.lat, +c.lng]; }).catch(() => {});
    camLoop();
  } }).catch(() => { if (CAM.source === "webcam") camLoop(); });

/* ---------------- Sound gate ---------------- */
// Browsers only allow audio after a user gesture. Any first click or key unlocks it; the button makes that explicit.
{
  const gate = document.getElementById("soundGate");
  const unlock = () => {
    try { const C = window.AudioContext || window.webkitAudioContext; if (C) { const c = new C(); c.resume(); const o = c.createOscillator(), g = c.createGain(); g.gain.value = 0; o.connect(g).connect(c.destination); o.start(); o.stop(c.currentTime + 0.05); } } catch {}
    try { speechSynthesis.speak(new SpeechSynthesisUtterance("")); } catch {}
    gate?.remove();
    removeEventListener("pointerdown", unlock, true); removeEventListener("keydown", unlock, true);
  };
  addEventListener("pointerdown", unlock, true); addEventListener("keydown", unlock, true);
}
