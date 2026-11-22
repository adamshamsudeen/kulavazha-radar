// Shared data for browser and serverless functions.
// Coordinates are approximate points on the water near each landmark.

export const LAKE_CENTER = [9.545, 76.39];

// Places, ferry routes, sizes and estimate assumptions live in places.json
// so the Python backend reads the same source of truth.
const CFG = await (await fetch(new URL("./places.json", import.meta.url))).json();
export const PLACES = CFG.places;
export const FERRY_ROUTES = CFG.ferry_routes;
export const SIZES = CFG.sizes;
// ESTIMATES ONLY. Edit places.json to align with the MushPellets proposal numbers.
export const ASSUMPTIONS = CFG.assumptions;

export function estimate(area_m2) {
  const A = ASSUMPTIONS;
  const wet_t = (area_m2 * A.wet_kg_per_m2) / 1000;
  const dry_kg = wet_t * 1000 * A.dry_matter_fraction;
  const pellet_kg = dry_kg * A.pellet_kg_per_dry_kg;
  return {
    area_m2,
    wet_t: Math.round(wet_t * 10) / 10,
    dry_kg: Math.round(dry_kg),
    pellet_kg: Math.round(pellet_kg),
    harvest_rs: Math.round(wet_t * A.harvest_rs_per_wet_t),
    pellet_rs: Math.round(pellet_kg * A.pellet_rs_per_kg),
    mushroom_kg: Math.round(dry_kg * (A.mushroom_kg_per_dry_kg || 0)),
  };
}

// Canned reports for demo / offline mode. Extraction is what Sarvam-105B returned (or would return).
export const DEMO_REPORTS = [
  {
    id: "demo1",
    reporter: "Rajan, ferry crew",
    transcript: "പുന്നമടയിൽ നെഹ്റു ട്രോഫി ഫിനിഷിങ് പോയിന്റിന് അടുത്ത് വലിയൊരു കുളവാഴക്കൂട്ടം ഉണ്ട്, ബോട്ടിന് പോകാൻ പറ്റുന്നില്ല.",
    translation: "Near the Nehru Trophy finishing point at Punnamada there is a large hyacinth mat; boats can't get through.",
    extraction: { place_id: "punnamada", size: "large", blocking: true, area_m2: null },
  },
  {
    id: "demo2",
    reporter: "Suma, Kainakary",
    transcript: "കൈനകരി ജെട്ടിയുടെ അടുത്ത് ഇടത്തരം കുളവാഴ അടിഞ്ഞുകൂടിയിട്ടുണ്ട്.",
    translation: "Medium-sized hyacinth has piled up near Kainakary jetty.",
    extraction: { place_id: "kainakary", size: "medium", blocking: false, area_m2: null },
  },
  {
    id: "demo3",
    reporter: "Babu, fisherman",
    transcript: "മുഹമ്മ ജെട്ടിക്ക് തെക്ക് ഭാഗത്ത് കായലിൽ വളരെ വലിയ പായൽ കൂട്ടം ഒഴുകി വരുന്നു, ഏകദേശം അര ഏക്കർ ഉണ്ടാകും.",
    translation: "A very large hyacinth mat is drifting in the lake south of Muhamma jetty, about half an acre.",
    extraction: { place_id: "muhamma", size: "huge", blocking: false, area_m2: 2000 },
  },
  {
    id: "demo4",
    reporter: "Anil, houseboat driver",
    transcript: "പള്ളാത്തുരുത്തി പാലത്തിന് താഴെ ചെറിയ കുളവാഴ കൂട്ടം കുടുങ്ങി കിടക്കുന്നു.",
    translation: "A small hyacinth mat is stuck under Pallathuruthy bridge.",
    extraction: { place_id: "pallathuruthy", size: "small", blocking: false, area_m2: null },
  },
];

// Typical late-November evening wind over Vembanad (from NW, light), used when offline.
export const DEMO_WIND = {
  speed_ms: [3.6, 3.9, 4.1, 3.8, 3.4, 3.1],
  dir_deg: [300, 305, 310, 310, 315, 320],
};

export function warningText(place, size, route) {
  const s = SIZES[size] || SIZES.medium;
  if (route) {
    return `മുന്നറിയിപ്പ്. ${place.ml} ഭാഗത്ത് ${s.ml} കുളവാഴക്കൂട്ടം കണ്ടെത്തി. അടുത്ത ആറ് മണിക്കൂറിനുള്ളിൽ ഇത് ${route.ml} ബോട്ട് പാതയിലേക്ക് ഒഴുകിയെത്താൻ സാധ്യതയുണ്ട്. ബോട്ടുകൾ ശ്രദ്ധിച്ച് യാത്ര ചെയ്യുക.`;
  }
  return `അറിയിപ്പ്. ${place.ml} ഭാഗത്ത് ${s.ml} കുളവാഴക്കൂട്ടം റിപ്പോർട്ട് ചെയ്തു. വിളവെടുപ്പ് സംഘത്തെ അറിയിച്ചിട്ടുണ്ട്.`;
}
