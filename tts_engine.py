"""
tts_engine.py
-------------
Handles text -> speech (mp3) generation using Microsoft Edge Neural TTS.
(Cloud-Ready Version - Playback is now handled by the frontend browser)
"""

import os
import asyncio
import edge_tts

# Audio folder setup
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'audio')
os.makedirs(AUDIO_DIR, exist_ok=True)


def generate_audio(text, lang, filename):
    """
    Generate an mp3 file using Microsoft Edge Neural TTS.
    Returns the absolute file path on success, or None if generation failed.
    """
    if not text or not text.strip():
        return None

    path = os.path.join(AUDIO_DIR, filename)
    
    # Choose premium neural voices based on language
    if lang == 'hi':
        # Swara is a very natural sounding Indian female voice for Hindi
        voice = "hi-IN-SwaraNeural" 
    else:
        # Neerja is a professional Indian female voice for English
        voice = "en-IN-NeerjaNeural"

    try:
        # edge_tts is asynchronous, so we run it in a sync wrapper
        async def _generate():
            communicate = edge_tts.Communicate(text.strip(), voice)
            await communicate.save(path)
            
        asyncio.run(_generate())
        return path
    except Exception as exc:
        print(f"[TTS ERROR] Could not generate audio for '{filename}': {exc}")
        return None