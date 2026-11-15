"""Send audio files to ElevenLabs once and save the raw responses for replay (development tool).

Saves .stt-replay/<audio sha256>.json, the format STT_PROVIDER=replay reads (app/stt.py). Files that already
have a saved response are skipped, so nothing is paid for twice. Reads ELEVENLABS_API_KEY from the
environment or from .env; the key is never printed.

Usage: uv run python scripts/stt_capture.py data/shopnest/recordings/rec_001.wav [...]
"""

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ingest import sha256_of  # noqa: E402
from app.stt import ELEVENLABS_URL, ElevenLabs  # noqa: E402

REPLAY = ROOT / ".stt-replay"


def api_key() -> str:
    if os.environ.get("ELEVENLABS_API_KEY"):
        return os.environ["ELEVENLABS_API_KEY"]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ELEVENLABS_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("ELEVENLABS_API_KEY not set")


def main() -> None:
    REPLAY.mkdir(exist_ok=True)
    key = api_key()
    for name in sys.argv[1:]:
        path = Path(name)
        out = REPLAY / f"{sha256_of(path)}.json"
        if out.exists():
            print(f"{name}: already saved")
            continue
        with path.open("rb") as f:
            r = httpx.post(ELEVENLABS_URL, headers={"xi-api-key": key}, timeout=120,
                           data={"model_id": ElevenLabs.model, "language_code": "tr",
                                 "use_multi_channel": "true", "timestamps_granularity": "word"},
                           files={"file": (path.name, f, "audio/wav")})
        if r.status_code != 200:
            sys.exit(f"{name}: HTTP {r.status_code}")
        out.write_text(json.dumps(r.json(), ensure_ascii=False))
        payload = r.json()
        for ch in payload.get("transcripts", [payload]):
            print(f"{name} channel {ch.get('channel_index')}: {ch.get('text')}")


if __name__ == "__main__":
    main()
