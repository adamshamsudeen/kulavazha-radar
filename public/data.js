// Shared data for browser and serverless functions.
// Coordinates are approximate points on the water near each landmark.

export const LAKE_CENTER = [9.545, 76.39];

export const PLACES = [
  { id: "alappuzha_jetty", en: "Alappuzha Boat Jetty", ml: "ആലപ്പുഴ ബോട്ട് ജെട്ടി", aliases: ["alappuzha", "alleppey", "ആലപ്പുഴ", "boat jetty", "ബോട്ട് ജെട്ടി"], lat: 9.4985, lng: 76.3405 },
  { id: "punnamada", en: "Punnamada (Nehru Trophy finish)", ml: "പുന്നമട", aliases: ["punnamada", "പുന്നമട", "nehru trophy", "നെഹ്റു ട്രോഫി", "finishing point"], lat: 9.5135, lng: 76.3475 },
  { id: "pallathuruthy", en: "Pallathuruthy Bridge", ml: "പള്ളാത്തുരുത്തി", aliases: ["pallathuruthy", "പള്ളാത്തുരുത്തി"], lat: 9.4735, lng: 76.3585 },
  { id: "kainakary", en: "Kainakary Jetty", ml: "കൈനകരി", aliases: ["kainakary", "kainakari", "കൈനകരി"], lat: 9.4905, lng: 76.3815 },
  { id: "r_block", en: "R-Block Kayal", ml: "ആർ ബ്ലോക്ക് കായൽ", aliases: ["r block", "r-block", "ആർ ബ്ലോക്ക്"], lat: 9.5235, lng: 76.3905 },
  { id: "c_block", en: "C-Block Kayal", ml: "സി ബ്ലോക്ക് കായൽ", aliases: ["c block", "c-block", "സി ബ്ലോക്ക്"], lat: 9.5425, lng: 76.3765 },
  { id: "nedumudi", en: "Nedumudi", ml: "നെടുമുടി", aliases: ["nedumudi", "നെടുമുടി"], lat: 9.4415, lng: 76.4045 },
  { id: "champakulam", en: "Champakulam", ml: "ചമ്പക്കുളം", aliases: ["champakulam", "ചമ്പക്കുളം"], lat: 9.4175, lng: 76.4135 },
  { id: "kavalam", en: "Kavalam", ml: "കാവാലം", aliases: ["kavalam", "കാവാലം"], lat: 9.4745, lng: 76.4505 },
  { id: "pathiramanal", en: "Pathiramanal Island", ml: "പാതിരാമണൽ", aliases: ["pathiramanal", "പാതിരാമണൽ"], lat: 9.6185, lng: 76.3895 },
  { id: "muhamma", en: "Muhamma Jetty", ml: "മുഹമ്മ", aliases: ["muhamma", "മുഹമ്മ"], lat: 9.6035, lng: 76.3625 },
  { id: "kumarakom", en: "Kumarakom", ml: "കുമരകം", aliases: ["kumarakom", "കുമരകം"], lat: 9.6145, lng: 76.4245 },
  { id: "thanneermukkom", en: "Thanneermukkom Bund", ml: "തണ്ണീർമുക്കം ബണ്ട്", aliases: ["thanneermukkom", "തണ്ണീർമുക്കം", "bund", "ബണ്ട്"], lat: 9.6765, lng: 76.3935 },
  { id: "vaikom", en: "Vaikom Jetty", ml: "വൈക്കം", aliases: ["vaikom", "വൈക്കം"], lat: 9.7445, lng: 76.3865 },
];

// Approximate state water transport (SWTD) ferry corridors, for warning logic.
export const FERRY_ROUTES = [
  { id: "alp_ktm", en: "Alappuzha – Kottayam ferry", ml: "ആലപ്പുഴ–കോട്ടയം", path: [[9.4985, 76.3405], [9.5045, 76.3600], [9.5105, 76.3880], [9.5230, 76.4150], [9.5420, 76.4550], [9.5640, 76.4850], [9.5830, 76.5100]] },
  { id: "alp_kvl", en: "Alappuzha – Kavalam ferry", ml: "ആലപ്പുഴ–കാവാലം", path: [[9.4985, 76.3405], [9.4840, 76.3640], [9.4760, 76.3900], [9.4740, 76.4200], [9.4745, 76.4505]] },
  { id: "muh_kmk", en: "Muhamma – Kumarakom ferry", ml: "മുഹമ്മ–കുമരകം", path: [[9.6035, 76.3625], [9.6080, 76.3900], [9.6120, 76.4100], [9.6145, 76.4245]] },
  { id: "vkm_tvk", en: "Vaikom – Thavanakkadavu ferry", ml: "വൈക്കം–തവണക്കടവ്", path: [[9.7445, 76.3865], [9.7420, 76.3650], [9.7400, 76.3480]] },
];

export const SIZES = {
  small: { en: "Small", ml: "ചെറിയ", area_m2: 150 },
  medium: { en: "Medium", ml: "ഇടത്തരം", area_m2: 600 },
  large: { en: "Large", ml: "വലിയ", area_m2: 2000 },
  huge: { en: "Very large", ml: "വളരെ വലിയ", area_m2: 5000 },
};

// ESTIMATES ONLY. Every figure shown in the app is derived from these.
// Edit here to align with the MushPellets proposal numbers.
export const ASSUMPTIONS = {
  wet_kg_per_m2: 20,          // dense mat fresh biomass, ~200 t/ha (literature range ~100–400 t/ha)
  dry_matter_fraction: 0.06,  // hyacinth is ~94% water
  pellet_kg_per_dry_kg: 2.0,  // hyacinth dry matter blended ~1:1 with rubberwood sawdust into substrate pellets
  harvest_rs_per_wet_t: 1200, // paid to the harvesting group (Kudumbashree / fisher collective) per wet tonne
  pellet_rs_per_kg: 18,       // indicative substrate pellet value
  pilot_target_wet_t: 100,    // MushPellets 12-month pilot target
  windage: 0.03,              // mat drifts at ~3% of 10 m wind speed (plus small lake current)
  ferry_alert_m: 900,         // warn ferries if forecast path passes within this distance
};

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
    translation: "A very large weed mat is drifting in the lake south of Muhamma jetty, about half an acre.",
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
