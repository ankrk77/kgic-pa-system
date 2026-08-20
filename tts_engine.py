"""
tts_engine.py
-------------
Handles text -> speech (mp3) generation using Microsoft Edge Neural TTS.

IMPORTANT (Aug 2026 fix): This version generates audio entirely IN MEMORY
and returns raw bytes. It never writes to the local disk.

Why: free-tier hosts like Render use an EPHEMERAL filesystem — every time
the service restarts/redeploys/wakes from sleep, anything written to local
disk (like the old static/audio/*.mp3 files) is wiped, even though the
database survives. Generating audio as bytes and handing it to the caller
(who stores it in Postgres as BYTEA) means there is no local file to lose.
"""

import asyncio
import edge_tts

# Choose premium neural voices based on language
VOICE_MAP = {
    "hi": "hi-IN-SwaraNeural",   # Natural sounding Indian female voice for Hindi
    "en": "en-IN-NeerjaNeural",  # Professional Indian female voice for English
}


def generate_audio_bytes(text, lang):
    """
    Generate mp3 audio for `text` in `lang` ('en' or 'hi') and return it as
    raw bytes, or None if generation failed / text was empty.
    """
    if not text or not text.strip():
        return None

    voice = VOICE_MAP.get(lang, VOICE_MAP["en"])

    try:
        async def _generate():
            communicate = edge_tts.Communicate(text.strip(), voice)
            chunks = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    chunks.extend(chunk["data"])
            return bytes(chunks)

        audio_bytes = asyncio.run(_generate())
        if not audio_bytes:
            print(f"[TTS ERROR] Empty audio returned for lang={lang}")
            return None
        return audio_bytes
    except Exception as exc:
        print(f"[TTS ERROR] Could not generate audio (lang={lang}): {exc}")
        return None