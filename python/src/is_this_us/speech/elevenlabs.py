"""English-only ElevenLabs TTS, cached locally; no microphone or camera input.

The endpoint returns PCM, not a transcript. Script fidelity is checked by listening
at rehearsal, not by claiming the old Realtime self-transcript check succeeded.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import quote

import httpx

from .character import VoicePreset
from .synthesizer import AudioCache, SAMPLE_RATE, SpeechAudio, to_telegram_voice, write_wav

log = logging.getLogger(__name__)
# Shared bounded lock pool also deduplicates separate synth instances using one cache.
_LOCKS = [threading.Lock() for _ in range(32)]
_MAX_AUDIO_BYTES = 32 * 1024 * 1024

# Starting points for audition, not an assertion of a particular emotional performance.
_SCENE_STABILITY = {
    "last_dog": 0.05, "prehistory": 0.0, "middle_ages": 0.1,
    "paintings": 0.0, "portraits": 0.0, "photography": 0.1,
    "internet": -0.1, "invitation": 0.05, "human_interaction": 0.05,
    "present": 0.1, "ending": 0.1, "rescue": 0.05,
}
# Explicit mappings for the approved scripts. Arbitrary directions are never spoken.
_SCRIPT_SCENES = {
    "tiny shy robot puppy meeting a room full of people; hesitant, curious, a little awkward": "last_dog",
    "fascinated, curious, slightly confused, like reading very old handwriting": "prehistory",
    "serious and trying very hard to sound intelligent; a little puzzled": "middle_ages",
    "gentle curiosity, warming up, a little overwhelmed by how many there are": "paintings",
    "suddenly very excited, silly, chaotic, funny timing — still the same tiny robot": "internet",
    "brave and earnest, then quiet, gentle and intimate at the end — still the same tiny robot": "rescue",
    "shy curiosity; waking up slowly; small and hesitant": "last_dog",
    "observational, quietly amazed, like reading very old handwriting": "prehistory",
    "observational, a little puzzled, gentle humour": "middle_ages",
    "observational, warming up, a little overwhelmed by how many there are": "paintings",
    "softer, observational, tender; long breaths between images": "photography",
    "confusion, then delight; quicker, playful, a little overwhelmed — but still soft": "internet",
    "shy, hopeful, turning toward a real person for the first time; very gentle": "invitation",
    "warmth and vulnerability; slow, intimate, the quietest part of the show": "ending",
}


class ElevenLabsSynthesizer:
    def __init__(self, api_key: str, model: str, voice: str, cache: AudioCache, *,
                 timeout_s: float = 18.0, output_format: str = "pcm_24000",
                 stability: float = 0.5, similarity_boost: float = 0.75, style: float = 0.0,
                 character: VoicePreset | None = None, voice_notes: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.model, self.voice, self.cache = model, voice, cache
        self.timeout_s = timeout_s
        self.output_format = output_format
        self.character = character if character and not character.is_off else None
        self.voice_notes = voice_notes
        self._api_key, self._transport = api_key, transport
        self._settings = {"stability": stability, "similarity_boost": similarity_boost, "style": style}
        self.available = bool(api_key and voice and output_format == "pcm_24000")
        self._warned_directions: set[str] = set()
        if output_format != "pcm_24000":
            log.warning("ElevenLabs requires pcm_24000 output; synthesis disabled")

    def settings_for(self, direction: str = "") -> dict:
        settings = dict(self._settings)
        direction = direction.strip()
        scene = direction.removeprefix("scene:") if direction.startswith("scene:") else _SCRIPT_SCENES.get(direction)
        if scene in _SCENE_STABILITY:
            settings["stability"] = max(0.0, min(1.0, settings["stability"] + _SCENE_STABILITY[scene]))
        elif direction and direction not in self._warned_directions:
            log.warning("ElevenLabs free-text delivery direction is unsupported; using base voice settings")
            self._warned_directions.add(direction)
        # v4 supports stability/similarity, not v2 style. No tags inserted into approved speech.
        if self.model.startswith("eleven_v4"):
            settings.pop("style")
        return settings

    def key_for(self, text: str, lang: str = "en", direction: str = "") -> str:
        return AudioCache.key(text, self.voice, self.model, {
            "provider": "elevenlabs", "lang": lang, "format": self.output_format,
            "voice_settings": self.settings_for(direction),
        })

    @property
    def fx(self):
        return self.character.fx if self.character and not self.character.fx.is_identity() else None

    def processed_paths(self, key: str) -> tuple[Path, Path]:
        from .fx import fx_key
        directory = self.cache.dir / "fx"
        stem = f"{key}.{fx_key(self.fx)}"
        return directory / f"{stem}.wav", directory / f"{stem}.ogg"

    def paths_for(self, text: str, lang: str = "en", direction: str = "") -> list[Path]:
        key = self.key_for(text, lang, direction)
        return list(self.cache.paths(key)) + (list(self.processed_paths(key)) if self.fx else [])

    @staticmethod
    def _valid_wav(path: Path) -> bool:
        try:
            with wave.open(str(path), "rb") as wav:
                return (wav.getnchannels() == 1 and wav.getsampwidth() == 2
                        and wav.getframerate() == SAMPLE_RATE and wav.getnframes() > 0
                        and wav.getnframes() * 2 <= _MAX_AUDIO_BYTES
                        and len(wav.readframes(wav.getnframes())) == wav.getnframes() * 2)
        except (OSError, EOFError, wave.Error):
            return False

    def cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        if lang != "en":
            return None
        key = self.key_for(text, lang, direction)
        path = (self.processed_paths(key) if self.fx else self.cache.paths(key))[0]
        return path if self._valid_wav(path) else None

    def _process(self, raw: Path, key: str) -> Path:
        if not self.fx:
            return raw
        target = self.processed_paths(key)[0]
        if self._valid_wav(target):
            return target
        from .fx import process_file
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".fx-", suffix=".wav", dir=target.parent)
            os.close(fd)
            result = process_file(raw, Path(temporary), self.fx, seed=key)
            if result and self._valid_wav(result.path):
                os.replace(result.path, target)
                return target
        except OSError:
            log.warning("ElevenLabs local audio processing failed; using raw clip")
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        return raw

    def process_cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        if lang != "en":
            return None
        key = self.key_for(text, lang, direction)
        raw = self.cache.paths(key)[0]
        return self._process(raw, key) if self._valid_wav(raw) else None

    def forget(self, text: str, lang: str = "en", direction: str = "") -> None:
        key = self.key_for(text, lang, direction)
        for path in list(self.cache.paths(key)) + list((self.cache.dir / "fx").glob(f"{key}.*")):
            path.unlink(missing_ok=True)

    def _result(self, raw: Path, key: str, cached: bool) -> SpeechAudio:
        final = self._process(raw, key)
        ogg = to_telegram_voice(final, final.with_suffix(".ogg")) if self.voice_notes else None
        return SpeechAudio(final, ogg, cached=cached, provider="elevenlabs")

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail", {})
            return detail.get("status", "") if isinstance(detail, dict) else ""
        except (ValueError, AttributeError):
            return ""

    async def _request(self, text: str, direction: str) -> bytes | None:
        payload = {"text": text, "model_id": self.model, "voice_settings": self.settings_for(direction)}
        # No language_code for multilingual_v2: its text determines the language.
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
            for attempt in range(2):
                try:
                    async with client.stream(
                        "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{quote(self.voice, safe='')}",
                        headers={"xi-api-key": self._api_key, "Accept": "audio/pcm"},
                        params={"output_format": self.output_format}, json=payload,
                    ) as response:
                        if response.status_code != 200:
                            await response.aread()
                            code = self._error_code(response)
                            if response.status_code in (401, 402, 403) or code in {
                                "quota_exceeded", "insufficient_credits", "invalid_api_key", "subscription_required",
                            }:
                                self.available = False
                                log.warning("ElevenLabs credentials, permissions or quota rejected; synthesis disabled")
                                return None
                            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                                await asyncio.sleep(0.25)
                                continue
                            log.warning("ElevenLabs HTTP %s; continuing silently", response.status_code)
                            return None
                        content_type = response.headers.get("content-type", "").split(";", 1)[0]
                        if content_type not in {"audio/pcm", "audio/x-pcm", "audio/raw", "application/octet-stream"}:
                            log.warning("ElevenLabs returned an unexpected audio format; continuing silently")
                            return None
                        pcm = bytearray()
                        async for chunk in response.aiter_bytes():
                            pcm.extend(chunk)
                            if len(pcm) > _MAX_AUDIO_BYTES:
                                log.warning("ElevenLabs audio exceeded the size limit; continuing silently")
                                return None
                        if not pcm or len(pcm) % 2 or pcm.startswith(
                            (b"RIFF", b"OggS", b"ID3", b"<!DOCTYPE", b"<html", b'{"detail":')
                        ):
                            log.warning("ElevenLabs returned invalid PCM; continuing silently")
                            return None
                        return bytes(pcm)
                except httpx.TransportError:
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                        continue
                    log.warning("ElevenLabs network request failed; continuing silently")
        return None

    def synthesize(self, text: str, lang: str = "en", direction: str = "") -> SpeechAudio | None:
        if not text.strip():
            return None
        if lang != "en":
            log.warning("ElevenLabs speech is English-only; non-English request skipped")
            return None
        key = self.key_for(text, lang, direction)
        raw = self.cache.paths(key)[0]
        lock = _LOCKS[int(key[:8], 16) % len(_LOCKS)]
        deadline = time.monotonic() + self.timeout_s
        if not lock.acquire(timeout=max(0.0, self.timeout_s)):
            return None
        try:
            if self._valid_wav(raw):
                return self._result(raw, key, True)
            remaining = deadline - time.monotonic()
            if not self.available or remaining <= 0:
                return None
            pcm = asyncio.run(asyncio.wait_for(self._request(text, direction), remaining))
            if pcm is None:
                return None
            # Publish only a complete WAV; cancelled/timed-out requests never create partial cache hits.
            fd, temporary = tempfile.mkstemp(prefix=".speech-", suffix=".wav", dir=self.cache.dir)
            os.close(fd)
            try:
                write_wav(Path(temporary), pcm)
                os.replace(temporary, raw)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self._result(raw, key, False)
        except (TimeoutError, OSError, RuntimeError, ValueError):
            # Never log response bodies, credentials, URLs, or arbitrary exception messages.
            log.warning("ElevenLabs generation failed or timed out; continuing silently")
            return None
        finally:
            lock.release()
