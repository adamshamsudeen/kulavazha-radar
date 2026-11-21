"""Settings and shared data (places.json is shared with the frontend)."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")
LLM_MODEL = os.environ.get("SARVAM_LLM_MODEL", "sarvam-105b")
TTS_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "anand")
CAM_URL = os.environ.get("CAM_URL", "")
CAM_PLACE = os.environ.get("CAM_PLACE", "punnamada")
# Public base URL (e.g. a tunnel to this laptop) used for the "report from your phone" QR code.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

DATA = json.loads((PUBLIC / "places.json").read_text(encoding="utf-8"))
PLACES = DATA["places"]
SIZES = DATA["sizes"]

# Words the generic translator gets wrong for this domain.
ML_GLOSSARY_FIX = {
    "ഞാങ്ങണ": "കുളവാഴ",
    "വാഴ മരങ്ങ": "കുളവാഴ",
    "കുമാർകോം": "കുമരകം",
    "കുമരകോം": "കുമരകം",
    "ജലഹയാസിന്ത്": "കുളവാഴ",
    "വാട്ടർ ഹയാസിന്ത്": "കുളവാഴ",
    "കുമാരകം": "കുമരകം",
    "മുഷപെല്ലെറ്റ്സ്": "മഷ്‌പെല്ലറ്റ്സ്",
    "മുഷ് പെല്ലറ്റ്സ്": "മഷ്‌പെല്ലറ്റ്സ്",
}


def fix_ml(text: str) -> str:
    for wrong, right in ML_GLOSSARY_FIX.items():
        text = text.replace(wrong, right)
    return text
