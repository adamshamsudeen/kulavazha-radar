"""Turn a boatman's Malayalam voice report into structured data with Sarvam-105B."""
import json
import re

from . import config
from .sarvam import chat

SYSTEM = f"""You read short voice reports from boatmen and fishers on Vembanad Lake, Kerala, about floating
water hyacinth mats. Words for the weed (all mean water hyacinth): കുളവാഴ, കുളവാഴക്കൂട്ടം, പോള, പോളപ്പായൽ, കാക്കപ്പോള,
പായൽ, kulavazha, pola, payal (never banana or reed).
Reply with ONE JSON object and nothing else. All string values must be in double quotes. Keys:
"place_id": one id from the list below that best matches the place mentioned, or null
"place_text": the place as spoken
"size": "small" | "medium" | "large" | "huge"  (ചെറിയ=small, ഇടത്തരം=medium, വലിയ=large, വളരെ വലിയ/ഭീമൻ=huge)
"area_m2": number if the speaker gave an area (1 acre = 4047, 1 cent = 40.5), else null
"blocking": true if boats or ferries are stuck or cannot pass, else false
"translation": faithful one-sentence English translation; call the weed "water hyacinth"; keep negations exactly
Places:
""" + "\n".join(f"{p['id']} = {p['en']} / {p['ml']}" for p in config.PLACES)


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Repair the common failure: an unquoted non-ASCII value, e.g. "key": പുന്നമട...,
        raw = re.sub(r'(":\s*)([^"\s\d\[\{ntf-][^,\n}]*)', lambda g: g.group(1) + json.dumps(g.group(2).strip()), raw)
        return json.loads(raw)


async def extract_report(transcript: str) -> dict:
    try:
        out = await chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": transcript}],
                         json_mode=True)
        j = _parse_json(out)
        if not any(p["id"] == j.get("place_id") for p in config.PLACES):
            j["place_id"] = heuristic_place(transcript)
        if j.get("size") not in config.SIZES:
            j["size"] = heuristic_size(transcript)
        j["blocking"] = bool(j.get("blocking"))
        j["source"] = "sarvam-105b"
        return j
    except Exception as e:  # never break the demo: fall back to keyword rules
        return {
            "place_id": heuristic_place(transcript),
            "size": heuristic_size(transcript),
            "area_m2": None,
            "blocking": bool(re.search(r"പറ്റുന്നില്ല|കുടുങ്ങി|stuck|block", transcript, re.I)),
            "translation": None,
            "source": "keyword-fallback",
            "error": str(e)[:200],
        }


def heuristic_place(text: str):
    t = text.lower()
    for p in config.PLACES:
        if any(a.lower() in t for a in p["aliases"]):
            return p["id"]
    return None


def heuristic_size(text: str) -> str:
    if re.search(r"വളരെ വലിയ|ഭീമ|huge|very big|ഏക്കർ", text, re.I):
        return "huge"
    if re.search(r"വലിയ|large|big", text, re.I):
        return "large"
    if re.search(r"ചെറിയ|small", text, re.I):
        return "small"
    return "medium"
