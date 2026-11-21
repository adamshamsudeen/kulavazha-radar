// POST { image_b64, mime } -> { result } : Sarvam Vision reads a harvest / weighbridge slip.
import { apiKey, visionExtract } from "../lib/sarvam.js";

const SCHEMA = {
  type: "object",
  properties: {
    wet_weight_kg: { type: "number", description: "Total wet weight of water hyacinth (kulavazha / പായൽ) harvested, in kilograms. Convert tonnes to kg." },
    date: { type: "string", description: "Date on the slip" },
    crew: { type: "string", description: "Name of the harvesting group, crew or Kudumbashree unit" },
    location: { type: "string", description: "Place where the weed was harvested or landed" },
    vehicle_or_boat: { type: "string", description: "Boat or vehicle number if present" },
  },
};

export async function POST(request) {
  const t0 = Date.now();
  try {
    if (!apiKey()) return Response.json({ error: "SARVAM_API_KEY not set" }, { status: 503 });
    const { image_b64, mime = "image/jpeg" } = await request.json();
    const out = await visionExtract(Buffer.from(image_b64, "base64"), mime, SCHEMA);
    return Response.json({ result: out.result || {}, confidence: out.annotations || null, ms: Date.now() - t0 });
  } catch (e) {
    return Response.json({ error: String(e.message || e) }, { status: 502 });
  }
}
