import { apiKey } from "../lib/sarvam.js";
export function GET() {
  return Response.json({ ok: true, live: Boolean(apiKey()), time: new Date().toISOString() });
}
