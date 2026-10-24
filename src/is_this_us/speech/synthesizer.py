"""The robot's voice.

* `ElevenLabsSynthesizer` — official `elevenlabs` SDK (`client.text_to_speech.convert`).
  Receives ONLY the sentence to speak: never images, landmarks or user identifiers.
* Audio is cached by a hash of (text, voice, model, settings) to avoid repeat API calls.
* Telegram voice notes need OGG/Opus; we convert with FFmpeg (libopus).
* Any failure (no key, invalid key, rate limit, network) degrades to text-only; the
  installation never stops because the voice is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Voice direction: soft, calm, slightly slow; low style exaggeration so it is not "cute".
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.62,
    "similarity_boost": 0.75,
    "style": 0.12,
    "use_speaker_boost": False,
    "speed": 0.9,
}


@dataclass
class SpeechAudio:
    mp3_path: Path
    ogg_path: Path | None
    cached: bool
    provider: str


class SpeechSynthesizer(Protocol):
    available: bool

    def synthesize(self, text: str, lang: str = "en") -> SpeechAudio | None: ...


class AudioCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(text: str, voice_id: str, model_id: str, settings: dict) -> str:
        blob = json.dumps({"t": text, "v": voice_id, "m": model_id, "s": settings}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def paths(self, key: str) -> tuple[Path, Path]:
        return self.dir / f"{key}.mp3", self.dir / f"{key}.ogg"


def to_telegram_voice(mp3: Path, ogg: Path) -> Path | None:
    """Convert to OGG/Opus mono (Telegram voice-note format). Returns None if FFmpeg missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("ffmpeg not found; voice notes disabled (audio still sent as file)")
        return None
    if ogg.exists():
        return ogg
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-c:a", "libopus",
           "-b:a", "48k", "-application", "voip", str(ogg)]
    try:
        subprocess.run(cmd, check=True, timeout=30)
        return ogg
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ffmpeg conversion failed: %s", exc)
        return None


class NullSynthesizer:
    """Text-only mode."""

    available = False

    def synthesize(self, text: str, lang: str = "en") -> SpeechAudio | None:
        return None


class ElevenLabsSynthesizer:
    def __init__(self, api_key: str, voice_id: str, model_id: str, cache: AudioCache,
                 voice_settings: dict | None = None, client=None):
        self.voice_id = voice_id
        self.model_id = model_id
        self.cache = cache
        self.settings = dict(voice_settings or DEFAULT_VOICE_SETTINGS)
        self.available = bool(api_key and voice_id)
        self._client = client
        self._api_key = api_key
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            from elevenlabs.client import ElevenLabs

            self._client = ElevenLabs(api_key=self._api_key)
        return self._client

    def _request(self, text: str, lang: str) -> bytes:
        from elevenlabs import VoiceSettings

        kwargs = dict(
            voice_id=self.voice_id,
            text=text,
            model_id=self.model_id,
            output_format="mp3_44100_128",
            voice_settings=VoiceSettings(**self.settings),
        )
        if lang and lang != "en" and "multilingual" not in self.model_id:
            kwargs["language_code"] = lang
        chunks = self._get_client().text_to_speech.convert(**kwargs)
        return b"".join(chunks)

    def synthesize(self, text: str, lang: str = "en") -> SpeechAudio | None:
        if not self.available or not text.strip():
            return None
        key = AudioCache.key(text, self.voice_id, self.model_id, self.settings)
        mp3, ogg = self.cache.paths(key)
        if mp3.exists():
            return SpeechAudio(mp3, to_telegram_voice(mp3, ogg), cached=True, provider="elevenlabs")
        with self._lock:
            for attempt in range(2):
                try:
                    audio = self._request(text, lang)
                    break
                except Exception as exc:  # SDK raises elevenlabs.core.api_error.ApiError + httpx errors
                    status = getattr(exc, "status_code", None)
                    if status in (401, 403):
                        log.error("ElevenLabs rejected credentials (HTTP %s); voice disabled", status)
                        self.available = False
                        return None
                    if status == 429 and attempt == 0:
                        log.warning("ElevenLabs rate limit; retrying once in 2 s")
                        time.sleep(2)
                        continue
                    log.warning("ElevenLabs request failed (%s); continuing text-only", status or exc)
                    return None
            else:
                return None
        if not audio:
            return None
        mp3.write_bytes(audio)
        return SpeechAudio(mp3, to_telegram_voice(mp3, ogg), cached=False, provider="elevenlabs")


class MockSynthesizer:
    """For tests: records requests, writes deterministic dummy bytes, no network."""

    available = True

    def __init__(self, cache: AudioCache, fail: bool = False):
        self.cache = cache
        self.fail = fail
        self.requests: list[str] = []

    def synthesize(self, text: str, lang: str = "en") -> SpeechAudio | None:
        self.requests.append(text)
        if self.fail:
            return None
        key = AudioCache.key(text, "mock", "mock", {})
        mp3, ogg = self.cache.paths(key)
        cached = mp3.exists()
        mp3.write_bytes(b"ID3mock" + text.encode())
        ogg.write_bytes(b"OggSmock")
        return SpeechAudio(mp3, ogg, cached=cached, provider="mock")


class LocalSpeakerPlayer:
    """Plays audio on the computer's speaker (or a Bluetooth speaker paired to it).

    Used because the Go2 Python SDK exposes no audio-playback API.
    """

    CANDIDATES = (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["afplay"], ["mpg123", "-q"])

    def __init__(self, enabled: bool = True):
        self.cmd = None
        if enabled:
            for c in self.CANDIDATES:
                if shutil.which(c[0]):
                    self.cmd = c
                    break
        self.available = self.cmd is not None

    def play(self, path: Path) -> None:
        if not self.cmd:
            return
        threading.Thread(target=lambda: subprocess.run([*self.cmd, str(path)], check=False),
                         daemon=True).start()


def make_synthesizer(settings) -> SpeechSynthesizer:
    cache = AudioCache(settings.audio_cache_dir)
    if settings.elevenlabs_configured:
        try:
            import elevenlabs  # noqa: F401
        except ImportError:
            log.warning("ELEVENLABS_* set but the SDK is missing: pip install -e '.[voice]'. Text-only for now.")
            return NullSynthesizer()
        return ElevenLabsSynthesizer(settings.elevenlabs_api_key, settings.elevenlabs_voice_id,
                                     settings.elevenlabs_model_id, cache)
    log.info("ElevenLabs not configured; text-only voice")
    return NullSynthesizer()
