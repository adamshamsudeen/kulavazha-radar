"""Pre-generate Bulbul audio for the demo reports so Demo mode speaks real Sarvam voices offline.

    python -m tools.bake_audio
"""
import asyncio
import base64

from backend import config, sarvam

SIZES_ML = {k: v["ml"] for k, v in config.SIZES.items()}
DEMO = [("punnamada", "large"), ("kainakary", "medium"), ("muhamma", "huge"), ("pallathuruthy", "small")]


def warning(place, size, route):
    s = SIZES_ML[size]
    if route:
        return (f"മുന്നറിയിപ്പ്. {place['ml']} ഭാഗത്ത് {s} കുളവാഴക്കൂട്ടം കണ്ടെത്തി. അടുത്ത ആറ് മണിക്കൂറിനുള്ളിൽ ഇത് "
                f"{route['ml']} ബോട്ട് പാതയിലേക്ക് ഒഴുകിയെത്താൻ സാധ്യതയുണ്ട്. ബോട്ടുകൾ ശ്രദ്ധിച്ച് യാത്ര ചെയ്യുക.")
    return f"അറിയിപ്പ്. {place['ml']} ഭാഗത്ത് {s} കുളവാഴക്കൂട്ടം റിപ്പോർട്ട് ചെയ്തു. വിളവെടുപ്പ് സംഘത്തെ അറിയിച്ചിട്ടുണ്ട്."


async def main():
    out = config.PUBLIC / "audio"
    out.mkdir(exist_ok=True)
    jobs = [("confirm", "നന്ദി. നിങ്ങളുടെ റിപ്പോർട്ട് രേഖപ്പെടുത്തി. വിളവെടുപ്പ് സംഘത്തെ അറിയിച്ചിട്ടുണ്ട്.", "kavitha")]
    places = {p["id"]: p for p in config.PLACES}
    for pid, size in DEMO:
        for route in [None, *config.DATA["ferry_routes"]]:
            jobs.append((f"{pid}_{size}_{route['id'] if route else 'none'}", warning(places[pid], size, route), None))
    for name, text, speaker in jobs:
        b64 = await sarvam.text_to_speech(text, speaker=speaker)
        (out / f"{name}.wav").write_bytes(base64.b64decode(b64))
        print("wrote", name)


if __name__ == "__main__":
    asyncio.run(main())
