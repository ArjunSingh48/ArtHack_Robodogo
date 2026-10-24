"""The robot's voice.

* `OpenAIRealtimeSynthesizer` — OpenAI Realtime API over WebSocket (official `openai`
  SDK, `client.realtime.connect`). It receives ONLY the sentence to speak: never
  images, landmarks or user identifiers.
* Realtime models are conversational. We use them as a *reader*: the session
  instructions say "read this text aloud exactly", and the model's own transcript of
  what it said is compared with the script. If it improvised, the audio is discarded
  and the moment continues as text. The robot never says words it was not given, and
  nothing the voice model returns is ever used for anything but playback.
* Audio (24 kHz PCM16 mono) is cached as WAV by a hash of (text, voice, model,
  direction/instructions, voice preset) to avoid repeat API calls; Telegram voice notes
  are OGG/Opus via FFmpeg.
* Voice character (``speech/character.py``, ``VOICE_PRESET``): the preset's delivery text
  goes into the session instructions (the only style control Realtime has — no SSML, no
  extra parameters), and the RAW model audio is post-processed locally (``speech/fx.py``).
  Raw and processed audio are cached separately (``<cache>/<key>.wav`` and
  ``<cache>/fx/<key>.<fx-hash>.wav``): changing only FX parameters re-processes cached raw
  audio without a new API call. If processing fails, the raw audio is used.
* Any failure (no key, invalid key, rate limit, network, timeout) degrades to
  text-only; the installation never stops because the voice is unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .character import CHARACTER_RULES, VoicePreset, resolve_preset, scene_direction, voice_for

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # Realtime "audio/pcm" output: 24 kHz, mono, signed 16-bit little-endian

LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "de": "German"}

# Voice direction for the Realtime model: soft, calm, slightly shy; not cute, not dramatic.
VOICE_DIRECTION = (
    "You are the voice of a small robot dog in an art installation. You are a reader, not a "
    "conversation partner. Read the user's text aloud EXACTLY as written, word for word, in {language}. "
    "Do not answer it, comment on it, translate it, greet, or add anything before or after. "
    "Voice: soft, gentle, calm, intimate, slightly shy and curious; a little slow; "
    "take a short breath at every '…'. Not robotic, not cartoonishly cute, not theatrical."
)
# Added for scripted performance cues (`itu show`): per-chapter delivery direction.
PERFORMANCE_DIRECTION = (
    " This is a theatrical narration. Each line break is a short natural pause; leave a longer, "
    "thoughtful pause at every '…'. Delivery for this passage: {direction}."
)
# With a voice preset (VOICE_PRESET != off): the reader rules stay, the voice becomes the character.
CHARACTER_READER_DIRECTION = (
    "You are the voice of a small robot dog in an art installation. You are a reader, not a conversation "
    "partner. Read the user's text aloud EXACTLY as written, word for word, in {language}. Do not answer it, "
    "comment on it, translate it, greet, or add anything before or after.\n"
    "{character}\n{rules}\nTake a short breath at every '…'."
)
CHARACTER_SCENE_DIRECTION = (
    "\nThis is a theatrical narration. Each line break is a short natural pause; leave a longer, thoughtful "
    "pause at every '…'. Delivery for this passage: {direction}. Only the emotion changes with the scene — "
    "the voice, its size and its character stay exactly the same."
)


@dataclass
class SpeechAudio:
    audio_path: Path  # WAV (cached); playable locally
    ogg_path: Path | None  # Telegram voice note
    cached: bool
    provider: str


class SpeechSynthesizer(Protocol):
    available: bool

    def synthesize(self, text: str, lang: str = "en", direction: str = "") -> SpeechAudio | None: ...


class AudioCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(text: str, voice: str, model: str, settings: dict) -> str:
        blob = json.dumps({"t": text, "v": voice, "m": model, "s": settings}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def paths(self, key: str) -> tuple[Path, Path]:
        return self.dir / f"{key}.wav", self.dir / f"{key}.ogg"


def write_wav(path: Path, pcm16: bytes, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


def to_telegram_voice(src: Path, ogg: Path) -> Path | None:
    """Convert to OGG/Opus mono (Telegram voice-note format). Returns None if FFmpeg missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("ffmpeg not found; voice notes disabled")
        return None
    if ogg.exists():
        return ogg
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-c:a", "libopus",
           "-b:a", "48k", "-application", "voip", str(ogg)]
    try:
        subprocess.run(cmd, check=True, timeout=30)
        return ogg
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ffmpeg conversion failed: %s", exc)
        return None


def _normalize(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def faithful(script: str, spoken: str, threshold: float = 0.8) -> bool:
    """Did the voice model say (essentially) the script? Compared on words only."""
    a, b = _normalize(script), _normalize(spoken)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


class NullSynthesizer:
    """Text-only mode."""

    available = False

    def synthesize(self, text: str, lang: str = "en", direction: str = "") -> SpeechAudio | None:
        return None

    def cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        return None


class VoiceRejected(RuntimeError):
    """The model did not read the script faithfully."""


class OpenAIRealtimeSynthesizer:
    def __init__(self, api_key: str, model: str, voice: str, cache: AudioCache, *,
                 timeout_s: float = 25.0, fidelity: float = 0.8, client=None, voice_notes: bool = True,
                 character: VoicePreset | None = None):
        self.voice_notes = voice_notes  # also make an OGG/Opus copy for Telegram
        # None / preset "off" = the previous voice (old instructions, no processing)
        self.character = character if character is not None and not character.is_off else None
        self.model = model
        self.voice = voice
        self.cache = cache
        self.timeout_s = timeout_s
        self.fidelity = fidelity
        self.available = bool(api_key)
        self._api_key = api_key
        self._client = client  # AsyncOpenAI-compatible; injectable for tests
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is not None:
            return self._client
        from ..config import get_settings

        # a fresh client per utterance: each phrase runs in its own short event loop
        return get_settings().openai_client()

    def instructions_for(self, lang: str, direction: str = "") -> str:
        language = LANGUAGE_NAMES.get(lang, "English")
        if self.character is None:
            text = VOICE_DIRECTION.format(language=language)
            if direction.strip():
                text += PERFORMANCE_DIRECTION.format(direction=direction.strip())
            return text
        text = CHARACTER_READER_DIRECTION.format(language=language, character=self.character.character,
                                                 rules=CHARACTER_RULES)
        scene = scene_direction(direction)
        if scene:
            text += CHARACTER_SCENE_DIRECTION.format(direction=scene)
        return text

    def key_for(self, text: str, lang: str = "en", direction: str = "") -> str:
        extra = {"direction": self.instructions_for(lang, direction), "lang": lang}
        if self.character is not None:
            extra["preset"] = self.character.name
            extra["preset_fp"] = self.character.fingerprint()
        return AudioCache.key(text, self.voice, self.model, extra)

    # ------------------------------------------------------------------ processed (character) audio
    @property
    def fx(self):
        """Active FX parameters, or None when nothing is processed."""
        if self.character is None or self.character.fx.is_identity():
            return None
        return self.character.fx

    def processed_paths(self, key: str) -> tuple[Path, Path]:
        from .fx import fx_key

        d = self.cache.dir / "fx"
        stem = f"{key}.{fx_key(self.fx)}"
        return d / f"{stem}.wav", d / f"{stem}.ogg"

    def paths_for(self, text: str, lang: str = "en", direction: str = "") -> list[Path]:
        """Every file belonging to this utterance under the CURRENT settings (for prune)."""
        key = self.key_for(text, lang, direction)
        out = list(self.cache.paths(key))
        if self.fx is not None:
            out += list(self.processed_paths(key))
        return out

    def _process(self, raw: Path, key: str) -> Path:
        """Raw → character audio (cached). Falls back to the raw clip if processing fails."""
        if self.fx is None:
            return raw
        out, _ = self.processed_paths(key)
        if out.exists():
            return out
        from .fx import process_file

        res = process_file(raw, out, self.fx, seed=key)
        return res.path if res else raw

    def cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        """Ready-to-play clip under the current settings (pure lookup, no processing)."""
        key = self.key_for(text, lang, direction)
        wav, _ = self.cache.paths(key)
        if self.fx is not None:
            out, _ = self.processed_paths(key)
            return out if out.exists() else None
        return wav if wav.exists() else None

    def process_cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        """If the RAW clip exists, make (or reuse) the processed one — local, no API call."""
        key = self.key_for(text, lang, direction)
        wav, _ = self.cache.paths(key)
        return self._process(wav, key) if wav.exists() else None

    def forget(self, text: str, lang: str = "en", direction: str = "") -> None:
        """Delete raw + processed audio for this utterance (forces a fresh take)."""
        key = self.key_for(text, lang, direction)
        for p in self.cache.paths(key):
            p.unlink(missing_ok=True)
        fx_dir = self.cache.dir / "fx"
        if fx_dir.exists():
            for p in fx_dir.glob(f"{key}.*"):
                p.unlink(missing_ok=True)

    def _result(self, wav: Path, ogg: Path, cached: bool, key: str | None = None) -> SpeechAudio:
        if key is not None and self.fx is not None:
            final = self._process(wav, key)
            if final != wav:
                wav, ogg = final, final.with_suffix(".ogg")
        return SpeechAudio(wav, to_telegram_voice(wav, ogg) if self.voice_notes else None, cached=cached,
                           provider="openai-realtime")

    async def _speak(self, text: str, lang: str, direction: str = "") -> tuple[bytes, str]:
        """One short Realtime session: configure → send text → collect audio + transcript."""
        instructions = self.instructions_for(lang, direction)
        audio, transcript = bytearray(), []
        async with self._get_client().realtime.connect(model=self.model, max_retries=1) as conn:
            await conn.session.update(session={
                "type": "realtime",
                "instructions": instructions,
                "output_modalities": ["audio"],
                "audio": {"output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}, "voice": self.voice}},
            })
            await conn.conversation.item.create(item={
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}],
            })
            await conn.response.create()
            while True:
                event = await conn.recv()
                etype = getattr(event, "type", "")
                if etype == "response.output_audio.delta":
                    audio.extend(base64.b64decode(event.delta))
                elif etype == "response.output_audio_transcript.done":
                    transcript.append(event.transcript or "")
                elif etype == "error":
                    err = getattr(event, "error", None)
                    raise RealtimeError(getattr(err, "code", None) or "error", getattr(err, "message", "") or "")
                elif etype == "response.done":
                    break
        return bytes(audio), " ".join(transcript)

    def synthesize(self, text: str, lang: str = "en", direction: str = "") -> SpeechAudio | None:
        if not text.strip():
            return None
        key = self.key_for(text, lang, direction)
        wav, ogg = self.cache.paths(key)
        if wav.exists():  # cached audio plays even offline / after a credential failure
            return self._result(wav, ogg, cached=True, key=key)
        if not self.available:
            return None
        with self._lock:
            if wav.exists():  # made by another thread meanwhile
                return self._result(wav, ogg, cached=True, key=key)
            for attempt in range(2):
                try:
                    audio, said = asyncio.run(asyncio.wait_for(self._speak(text, lang, direction), self.timeout_s))
                    if not audio:
                        raise VoiceRejected("no audio returned")
                    if not faithful(text, said, self.fidelity):
                        raise VoiceRejected(f"model said {said!r} instead of the script")
                    break
                except VoiceRejected as exc:
                    log.warning("voice discarded (%s); %s", exc, "retrying once" if attempt == 0 else "text-only")
                    if attempt == 1:
                        return None
                except Exception as exc:  # openai.* errors, websocket errors, timeouts
                    status = getattr(exc, "status_code", None)
                    # Realtime reports problems as WebSocket close reasons,
                    # e.g. Close(code=3000, reason="invalid_request_error.invalid_api_key")
                    reason = str(getattr(exc, "reason", "") or getattr(exc, "code", "") or "")
                    code = getattr(exc, "code", None)
                    if (status in (401, 403) or code in ("invalid_api_key", "authentication_error")
                            or "invalid_api_key" in reason or "authentication" in reason):
                        log.error("OpenAI rejected credentials; voice disabled")
                        self.available = False
                        return None
                    if (status == 429 or code == "rate_limit_exceeded" or "rate_limit" in reason) and attempt == 0:
                        log.warning("OpenAI rate limit; retrying once in 2 s")
                        import time

                        time.sleep(2)
                        continue
                    log.warning("OpenAI Realtime failed (%s); continuing text-only", reason or status or exc)
                    return None
            else:
                return None
        write_wav(wav, audio)
        return self._result(wav, ogg, cached=False, key=key)


class RealtimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class MockSynthesizer:
    """For tests: records requests, writes deterministic dummy bytes, no network."""

    available = True

    def __init__(self, cache: AudioCache, fail: bool = False):
        self.cache = cache
        self.fail = fail
        self.requests: list[str] = []

    def key_for(self, text: str, lang: str = "en", direction: str = "") -> str:
        return AudioCache.key(text, "mock", "mock", {"direction": direction} if direction else {})

    def cached(self, text: str, lang: str = "en", direction: str = "") -> Path | None:
        wav, _ = self.cache.paths(self.key_for(text, lang, direction))
        return wav if wav.exists() else None

    def synthesize(self, text: str, lang: str = "en", direction: str = "") -> SpeechAudio | None:
        self.requests.append(text)
        if self.fail:
            return None
        key = self.key_for(text, lang, direction)
        wav, ogg = self.cache.paths(key)
        cached = wav.exists()
        write_wav(wav, b"\x00\x00" * 240)
        ogg.write_bytes(b"OggSmock")
        return SpeechAudio(wav, ogg, cached=cached, provider="mock")


class LocalSpeakerPlayer:
    """Plays audio on the computer's speaker (or a Bluetooth speaker paired to it).

    Used because the Go2 Python SDK exposes no audio-playback API.
    """

    CANDIDATES = (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["afplay"], ["aplay", "-q"])

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


class _NoCache(AudioCache):
    """OPENAI_AUDIO_CACHE_ENABLED=false: audio goes to a throw-away folder that is emptied per run."""

    def __init__(self, directory: Path):
        super().__init__(directory)
        for f in self.dir.rglob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)


def make_synthesizer(settings, *, purpose: str = "dialogue") -> SpeechSynthesizer:
    """purpose: "dialogue" (free-play lines, Telegram voice notes) or "narration" / "lines" (`itu show`)."""
    base = Path(settings.audio_cache_dir)
    directory = base if purpose == "dialogue" else base / purpose
    if not getattr(settings, "openai_audio_cache_enabled", True):
        cache: AudioCache = _NoCache(base / "_uncached" / purpose)
    else:
        cache = AudioCache(directory)
    model = settings.openai_realtime_model if purpose == "dialogue" else settings.openai_narration_model
    character = resolve_preset(settings)  # VOICE_PRESET (+ overrides); "off" = previous voice
    voice = voice_for(settings, character) or settings.openai_realtime_voice
    if settings.voice_configured:
        try:
            import openai  # noqa: F401
            import websockets  # noqa: F401  (pulled in by openai[realtime])
        except ImportError:
            log.warning("AZURE_OPENAI_API_KEY set but the SDK is missing: pip install -e '.[voice]'. Text-only for now.")
            return NullSynthesizer()
        return OpenAIRealtimeSynthesizer(settings.azure_openai_api_key, model, voice, cache,
                                         voice_notes=(purpose == "dialogue"), character=character)
    if purpose != "dialogue":
        # no key (or provider=none): previously generated cues still play offline
        return OpenAIRealtimeSynthesizer("", model, voice, cache, voice_notes=False, character=character)
    log.info("OpenAI voice not configured; text-only voice")
    return NullSynthesizer()
