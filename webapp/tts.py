"""Text-to-speech for photo-short narration.

Providers (pick with LEGEND_TTS_PROVIDER, or the first one with keys):
- "clova":      NAVER Cloud CLOVA Voice Premium — NCP_CLOVA_KEY_ID / NCP_CLOVA_KEY (natural Korean voices)
- "elevenlabs": ElevenLabs — ELEVENLABS_API_KEY (+ ELEVENLABS_VOICE_ID), multilingual model
Each slide is synthesised separately, so slide length follows the voice and captions stay in sync.
Endpoints are configurable; confirm parameter names with the provider's current docs before going live.
"""

from __future__ import annotations

import os

import httpx

from highlight_engine.photos import decode_audio


class TTSError(Exception):
    pass


class ClovaTTS:
    name = "clova"
    voices = [("nara", "나라 (여성, 밝음)"), ("nminsang", "민상 (남성, 차분)"), ("vdain", "다인 (여성, 또렷)"),
              ("jinho", "진호 (남성, 활기)")]

    def __init__(self, key_id: str, key: str, http: httpx.Client | None = None,
                 url: str = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts") -> None:
        self.key_id, self.key, self.url = key_id, key, os.environ.get("LEGEND_CLOVA_TTS_URL", url)
        self.http = http or httpx.Client(timeout=30)

    def synth(self, text: str, voice: str | None = None, speed: int = 0) -> bytes:
        r = self.http.post(self.url, headers={"X-NCP-APIGW-API-KEY-ID": self.key_id, "X-NCP-APIGW-API-KEY": self.key},
                           data={"speaker": voice or "nara", "text": text, "volume": 0, "speed": speed, "pitch": 0, "format": "mp3"})
        if r.status_code != 200:
            raise TTSError(f"클로바 보이스 오류 {r.status_code}: {r.text[:120]}")
        return r.content


class ElevenLabsTTS:
    name = "elevenlabs"
    voices = [("", "기본 목소리")]

    def __init__(self, api_key: str, voice_id: str, http: httpx.Client | None = None,
                 base: str = "https://api.elevenlabs.io") -> None:
        self.api_key, self.voice_id = api_key, voice_id
        self.base = os.environ.get("LEGEND_ELEVENLABS_URL", base).rstrip("/")
        self.http = http or httpx.Client(timeout=60)

    def synth(self, text: str, voice: str | None = None, speed: int = 0) -> bytes:
        r = self.http.post(f"{self.base}/v1/text-to-speech/{voice or self.voice_id}", params={"output_format": "mp3_44100_128"},
                           headers={"xi-api-key": self.api_key},
                           json={"text": text, "model_id": "eleven_multilingual_v2"})
        if r.status_code != 200:
            raise TTSError(f"ElevenLabs 오류 {r.status_code}: {r.text[:120]}")
        return r.content


def from_env(http: httpx.Client | None = None):
    want = os.environ.get("LEGEND_TTS_PROVIDER", "")
    clova = os.environ.get("NCP_CLOVA_KEY_ID"), os.environ.get("NCP_CLOVA_KEY")
    eleven = os.environ.get("ELEVENLABS_API_KEY")
    if want in ("", "clova") and all(clova):
        return ClovaTTS(*clova, http=http)
    if want in ("", "elevenlabs") and eleven:
        return ElevenLabsTTS(eleven, os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"), http=http)
    return None


def narrate(provider, text: str, voice: str | None = None):
    """Synthesise and decode to mono float32 samples (empty text → None)."""
    if not text.strip():
        return None
    return decode_audio(provider.synth(text, voice))
