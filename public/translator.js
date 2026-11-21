const $ = (s) => document.querySelector(s);
const state = { live: false, mode: "demo", busy: false };

const DEMO = [
  { side: "guest", from: "en-IN", heard: "How long until we reach Kumarakom? And can we stop to see the birds?", translated: "കുമരകത്ത് എത്താൻ എത്ര സമയമെടുക്കും? പക്ഷികളെ കാണാൻ നമുക്ക് നിർത്താമോ?" },
  { side: "crew", from: "ml-IN", heard: "ഏകദേശം ഒന്നര മണിക്കൂർ. പക്ഷി സങ്കേതത്തിന് അടുത്ത് പത്ത് മിനിറ്റ് നിർത്താം.", translated: "About one and a half hours. We can stop for ten minutes near the bird sanctuary." },
  { side: "guest", from: "hi-IN", heard: "खाने में क्या है? क्या करीमीन फ्राई मिलेगा?", translated: "ഭക്ഷണത്തിന് എന്താണുള്ളത്? കരിമീൻ ഫ്രൈ കിട്ടുമോ?" },
  { side: "crew", from: "ml-IN", heard: "ഉണ്ട്, ഉച്ചയ്ക്ക് കരിമീൻ പൊള്ളിച്ചതും ചോറും ഉണ്ട്. മുന്നിൽ കുളവാഴ ഉള്ളതുകൊണ്ട് പത്ത് മിനിറ്റ് വൈകും.", translated: "हाँ, दोपहर में करीमीन पोल्लिचथु और चावल है। आगे जलकुंभी होने की वजह से दस मिनट की देरी होगी।", to: "hi-IN" },
];

async function init() {
  try { state.live = !!(await (await fetch("api/status")).json()).live; } catch {}
  setMode(state.live ? "live" : "demo");
  $("#samples").innerHTML = DEMO.map((d, i) => `<button class="chip" data-i="${i}">${d.side === "guest" ? "Guest" : "Crew"}: ${esc(d.heard.slice(0, 28))}…</button>`).join("");
  $("#samples").onclick = (e) => { const b = e.target.closest(".chip"); if (b) runSample(DEMO[+b.dataset.i]); };
}
function setMode(m) {
  if (m === "live" && !state.live) { toast("Live Sarvam is not configured on the server."); m = "demo"; }
  state.mode = m;
  $("#modeLive").classList.toggle("on", m === "live");
  $("#modeDemo").classList.toggle("on", m === "demo");
}
$("#modeLive").onclick = () => setMode("live");
$("#modeDemo").onclick = () => setMode("demo");

function addBubble({ side, heard, translated, audio, ms }) {
  $("#convo .hint")?.remove();
  const el = document.createElement("div");
  el.className = `bubble ${side}`;
  el.innerHTML = `<div class="who">${side === "guest" ? "Guest said" : "Crew said · ജീവനക്കാർ"}</div>
    <div class="src">${esc(heard)}</div>
    <div class="dst">${esc(translated)}</div>
    <div class="meta">${ms ? `${(ms / 1000).toFixed(1)} s` : ""} ${audio ? `<button>🔊 Play</button>` : ""}</div>`;
  if (audio) el.querySelector("button").onclick = () => play(audio);
  $("#convo").appendChild(el);
  $("#convo").scrollTop = 1e6;
}

async function runSample(d) {
  const to = d.to || (d.side === "guest" ? "ml-IN" : $("#guestLang").value);
  if (state.mode === "live") return converse({ text: d.heard, from: d.from, to, side: d.side });
  addBubble({ side: d.side, heard: d.heard, translated: d.translated });
  speakBrowser(d.translated, d.side === "guest" ? "ml-IN" : (d.to || "en-IN"));
}

async function converse({ audio_b64, mime, text, from, to, side }) {
  state.busy = true;
  const t0 = Date.now();
  try {
    const r = await fetch("api/converse", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ audio_b64, mime, text, from, to }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    const audio = j.audio_b64 ? `data:audio/wav;base64,${j.audio_b64}` : null;
    addBubble({ side, heard: j.heard, translated: j.translated, audio, ms: Date.now() - t0 });
    if (audio) play(audio);
  } catch (e) {
    toast(`Translation failed: ${e.message}`);
  } finally { state.busy = false; }
}

// Recording
let rec = null;
function bindMic(btn, lbl, side) {
  btn.onclick = async () => {
    if (state.busy) return;
    if (rec && rec.state === "recording") { rec.stop(); return; }
    if (state.mode === "demo") { runSample(DEMO.find((d) => d.side === side)); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "audio/mp4";
      rec = new MediaRecorder(stream, { mimeType: mime });
      const chunks = [];
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        btn.classList.remove("rec"); lbl.textContent = side === "guest" ? "Tap to speak" : "സംസാരിക്കാൻ തൊടുക";
        const blob = new Blob(chunks, { type: rec.mimeType });
        if (blob.size < 2000) return toast("Didn't catch that.");
        const b64 = await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(fr.result.split(",")[1]); fr.readAsDataURL(blob); });
        btn.classList.add("busy"); lbl.textContent = "Translating…";
        const g = $("#guestLang").value;
        await converse({ audio_b64: b64, mime: blob.type, from: side === "guest" ? g : "ml-IN", to: side === "guest" ? "ml-IN" : g, side });
        btn.classList.remove("busy"); lbl.textContent = side === "guest" ? "Tap to speak" : "സംസാരിക്കാൻ തൊടുക";
      };
      rec.start(); btn.classList.add("rec"); lbl.textContent = side === "guest" ? "Listening… tap to send" : "കേൾക്കുന്നു…";
      setTimeout(() => rec.state === "recording" && rec.stop(), 25000);
    } catch { toast("Microphone not available."); }
  };
}
bindMic($("#micGuest"), $("#lblGuest"), "guest");
bindMic($("#micCrew"), $("#lblCrew"), "crew");

function play(src) { const a = new Audio(src); a.play().catch(() => {}); }
function speakBrowser(text, lang) { if (!("speechSynthesis" in window)) return; const u = new SpeechSynthesisUtterance(text); u.lang = lang; speechSynthesis.speak(u); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
let tt; function toast(m) { const t = $("#toast"); t.textContent = m; t.hidden = false; clearTimeout(tt); tt = setTimeout(() => (t.hidden = true), 4000); }
init();
