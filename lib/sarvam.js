// Thin Sarvam AI REST client. The key stays on the server.
const BASE = "https://api.sarvam.ai";

export function apiKey() {
  return process.env.SARVAM_API_KEY || "";
}

function headers(extra = {}) {
  return { "api-subscription-key": apiKey(), ...extra };
}

async function check(res, what) {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${what} failed (${res.status}): ${body.slice(0, 300)}`);
  }
  return res.json();
}

// Saaras v4 speech-to-text. audio: Buffer, mime: e.g. "audio/webm"
export async function speechToText(audio, mime = "audio/webm", mode = "transcribe", language = "ml-IN") {
  const ext = (mime.split("/")[1] || "webm").split(";")[0];
  const form = new FormData();
  form.append("file", new Blob([audio], { type: mime.split(";")[0] }), `report.${ext}`);
  form.append("model", "saaras:v4");
  form.append("mode", mode);
  if (language) form.append("language_code", language);
  const res = await fetch(`${BASE}/speech-to-text`, { method: "POST", headers: headers(), body: form });
  const j = await check(res, "Speech-to-text");
  return j.transcript || "";
}

// Sarvam-105B chat completion, reasoning off for speed.
export async function chat(messages, { maxTokens = 600, temperature = 0.2 } = {}) {
  const res = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      model: process.env.SARVAM_LLM_MODEL || "sarvam-105b",
      messages,
      temperature,
      max_tokens: maxTokens,
      reasoning_effort: null,
    }),
  });
  const j = await check(res, "Chat");
  return j.choices?.[0]?.message?.content || "";
}

// Bulbul TTS. Returns base64 WAV.
export async function textToSpeech(text, { speaker, model, pace = 0.95, language = "ml-IN" } = {}) {
  const body = {
    text,
    language_code: language,
    target_language_code: language,
    model: model || process.env.SARVAM_TTS_MODEL || "bulbul:v3",
    speaker: speaker || process.env.SARVAM_TTS_SPEAKER || "anand",
    pace,
  };
  const res = await fetch(`${BASE}/text-to-speech`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  const j = await check(res, "Text-to-speech");
  return (j.audios || []).join("");
}

// Sarvam-Translate text translation.
export async function translate(input, target = "en-IN", source = "auto") {
  const res = await fetch(`${BASE}/translate`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      input: input.slice(0, 1900),
      source_language_code: source,
      target_language_code: target,
      model: "sarvam-translate:v1",
    }),
  });
  const j = await check(res, "Translate");
  return j.translated_text || "";
}

// Sarvam Vision (Document Intelligence) field extraction from one image.
// Async job: submit, poll status, fetch results. Gives up after ~timeoutMs.
export async function visionExtract(buf, mime, schema, { timeoutMs = 25000 } = {}) {
  const ext = (mime.split("/")[1] || "jpg").replace("jpeg", "jpg");
  const form = new FormData();
  form.append("file", new Blob([buf], { type: mime }), `slip.${ext}`);
  form.append("schema", JSON.stringify(schema));
  form.append("mode", "basic");
  form.append("output_format", "json");
  const sub = await check(await fetch(`${BASE}/doc-ai/v1/job/extract`, { method: "POST", headers: headers(), body: form }), "Vision submit");
  const id = sub.job_id;
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await new Promise((r) => setTimeout(r, 1200));
    const st = await check(await fetch(`${BASE}/doc-ai/v1/job/${id}/status`, { headers: headers() }), "Vision status");
    const status = String(st.status || st.job_state || "").toLowerCase();
    if (["completed", "partially_completed"].includes(status)) {
      return check(await fetch(`${BASE}/doc-ai/v1/job/${id}/results`, { headers: headers() }), "Vision results");
    }
    if (["failed", "rejected"].includes(status)) throw new Error(`Vision job ${status}`);
  }
  throw new Error("Vision job timed out");
}
