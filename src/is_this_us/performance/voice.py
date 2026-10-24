"""The show's voice: cached narration clips, short dynamic lines, cancellable playback.

* Narration cues are pre-generated (`itu show voice pregenerate`) with the OpenAI
  Realtime API (`OPENAI_NARRATION_MODEL`, default gpt-realtime-2.1-mini) and stored as
  WAV under `<AUDIO_CACHE_DIR>/narration/`. The cache key covers text, voice, model,
  language and delivery direction, so editing a line or changing the voice simply makes
  a new clip; `--prune` removes clips no longer referenced.
* Dynamic lines (pose hints, consent) use a separate cache (`.../lines/`).
* Speaking never blocks the show indefinitely: a missing clip is generated with a
  timeout; if that fails, the line is shown as a subtitle for its reading time.
* Playback runs as a subprocess that is terminated on pause / skip / cancel.
* Voice character (VOICE_PRESET, docs/VOICE.md): the synthesizers apply the preset's
  instructions + audio processing to every kind of line; `ShowVoice.cue(name)` plays the
  preset's short procedural sound cues (startup, boop, chirp, puzzled, chime).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path

from .catalog import PerformanceCatalog
from .lines import Lines
from .script import Script

log = logging.getLogger(__name__)

PLAYERS = (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["afplay"], ["aplay", "-q"], ["paplay"])


def reading_seconds(text: str) -> float:
    """How long a line stays on screen when there is no audio."""
    words = len(re.findall(r"\w+", text))
    lines = text.count("\n") + 1
    return 0.8 + words / 2.6 + 0.4 * (lines - 1) + 0.5 * text.count("…")


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError, EOFError):
        return 0.0


class Speaker:
    """Async, cancellable local playback. Without a player, waits for the clip's duration."""

    def __init__(self, enabled: bool = True, command: list[str] | None = None):
        self.cmd = command
        if self.cmd is None and enabled:
            self.cmd = next((c for c in PLAYERS if shutil.which(c[0])), None)
        self.enabled = enabled

    @property
    def available(self) -> bool:
        return bool(self.cmd)

    async def play(self, path: Path, time_scale: float = 1.0) -> None:
        if not self.cmd or time_scale < 1.0:
            await asyncio.sleep(wav_seconds(path) * time_scale)
            return
        proc = await asyncio.create_subprocess_exec(*self.cmd, str(path), stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            await proc.wait()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
            raise


@dataclass
class Spoken:
    text: str
    audio: Path | None
    seconds: float


class ShowVoice:
    """One voice for the whole show. `narration` and `lines` are SpeechSynthesizers."""

    def __init__(self, narration, lines, speaker: Speaker, *, lang: str = "en", allow_generation: bool = True,
                 generation_timeout_s: float = 20.0, time_scale: float = 1.0, cues=None):
        self.narration = narration
        self.lines = lines
        self.speaker = speaker
        self.lang = lang
        self.allow_generation = allow_generation
        self.generation_timeout_s = generation_timeout_s
        self.time_scale = time_scale
        self._prefetching: set[str] = set()
        self.log: list[Spoken] = []
        # Sound cues: given explicitly, or derived from the narration synthesizer's voice preset.
        self.cues = cues if cues is not None else _cues_for(narration)

    def _synth(self, kind: str):
        return self.narration if kind == "narration" else self.lines

    async def audio_for(self, text: str, *, kind: str = "narration", direction: str = "") -> Path | None:
        synth = self._synth(kind)
        cached = getattr(synth, "cached", None)
        if cached is not None:
            hit = cached(text, self.lang, direction)
            if hit:
                return hit
        process = getattr(synth, "process_cached", None)
        if process is not None:  # raw clip cached, only the FX changed: local processing, no API call
            hit = await asyncio.to_thread(process, text, self.lang, direction)
            if hit:
                return hit
        if not self.allow_generation or not getattr(synth, "available", False):
            return None
        try:
            result = await asyncio.wait_for(asyncio.to_thread(synth.synthesize, text, self.lang, direction),
                                            self.generation_timeout_s)
        except asyncio.TimeoutError:
            log.warning("voice generation timed out; showing the line as text")
            return None
        return result.audio_path if result else None

    def prefetch(self, text: str, *, kind: str = "narration", direction: str = "") -> None:
        """Generate the next clip in the background while the current one plays."""
        synth = self._synth(kind)
        if not self.allow_generation or not getattr(synth, "available", False) or text in self._prefetching:
            return
        cached = getattr(synth, "cached", None)
        if cached is not None and cached(text, self.lang, direction):
            return
        self._prefetching.add(text)

        def work():
            try:
                synth.synthesize(text, self.lang, direction)
            finally:
                self._prefetching.discard(text)

        asyncio.get_running_loop().run_in_executor(None, work)

    async def speak(self, text: str, *, kind: str = "narration", direction: str = "") -> Spoken:
        audio = await self.audio_for(text, kind=kind, direction=direction)
        if audio is not None:
            secs = wav_seconds(audio)
            await self.speaker.play(audio, self.time_scale)
        else:
            secs = reading_seconds(text)
            await asyncio.sleep(secs * self.time_scale)
        spoken = Spoken(text, audio, secs)
        self.log.append(spoken)
        return spoken

    async def silence(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.time_scale)

    # ------------------------------------------------------------------ character sound cues
    async def cue(self, name: str) -> Path | None:
        """Play a short character sound (startup | boop | chirp | puzzled | chime) and wait for it.

        Silent no-op when cues are disabled (VOICE_CUES=false / VOICE_PRESET=off), when there is
        no player, in fast rehearsal, or if anything fails. Never raises (except cancellation).
        """
        if self.cues is None or not getattr(self.cues, "enabled", False) or self.time_scale < 1.0:
            return None
        if not self.speaker.available:
            return None
        return await self.cues.play(name, self.speaker)

    def cue_soon(self, name: str) -> None:
        """Fire-and-forget variant of `cue` (e.g. a chirp while the next line is prepared)."""
        if self.cues is None or not getattr(self.cues, "enabled", False):
            return
        try:
            asyncio.get_running_loop().create_task(self.cue(name))
        except RuntimeError:
            pass


def _cues_for(synth):
    """A CueBank for the synthesizer's voice preset (cached next to the audio cache), or None."""
    character = getattr(synth, "character", None)
    cache = getattr(synth, "cache", None)
    if character is None or cache is None:
        return None
    from ..speech.cues import CueBank

    return CueBank(Path(cache.dir).parent / "cues", character.cues)


# ---------------------------------------------------------------------- cache management
@dataclass
class CueAudio:
    cue_id: str
    kind: str
    text: str
    direction: str
    cached: Path | None


def all_utterances(script: Script, catalog: PerformanceCatalog, lines: Lines) -> list[CueAudio]:
    """Every fixed thing the show can say: narration cues (each pose request rendered) + lines."""
    out: list[CueAudio] = []
    requests = sorted({a.interactive.request for a in catalog.interactive("any")})
    for ch in script.chapters:
        for c in ch.cues:
            if c.kind == "try":
                out.append(CueAudio(c.id, "narration", c.text, ch.voice, None))
                out.append(CueAudio(f"{c.id}.yes", "narration", c.yes, ch.voice, None))
                out.append(CueAudio(f"{c.id}.no", "narration", c.no, ch.voice, None))
                continue
            if c.kind in ("round", "consent", "rescue"):  # presentation: the line said before the action
                if "{where}" in c.text:
                    for key in ("screen", "telegram"):
                        out.append(CueAudio(f"{c.id}~{key}", "narration",
                                            c.render(where=lines.text("consent_where", key)), ch.voice, None))
                else:
                    out.append(CueAudio(c.id, "narration", c.text, ch.voice, None))
                continue
            if c.kind != "speech":
                continue
            if c.text.strip() == "{practised}":
                out.append(CueAudio(c.id, "narration", lines.text("invite", "practised"), ch.voice, None))
                continue
            if c.needs_pose_request:
                # finale "encounter": the general invitation; finale "imitate": each painting's own request
                out.append(CueAudio(c.id, "narration", c.render(pose_request=lines.text("encounter", "invite")),
                                    ch.voice, None))
                for i, req in enumerate(requests):
                    out.append(CueAudio(f"{c.id}~{i + 1}", "narration", req, ch.voice, None))
            else:
                out.append(CueAudio(c.id, "narration", c.text, ch.voice, None))
    for key, text in lines.fixed_lines():
        out.append(CueAudio(key, "lines", text, "", None))
    return out


def cache_status(items: list[CueAudio], voice: ShowVoice) -> list[CueAudio]:
    for it in items:
        synth = voice._synth(it.kind)
        it.cached = synth.cached(it.text, voice.lang, it.direction) if hasattr(synth, "cached") else None
    return items


def prune(items: list[CueAudio], voice: ShowVoice) -> int:
    """Delete clips in the narration/lines caches that the current script no longer uses."""
    keep: set[Path] = set()
    for it in items:
        synth = voice._synth(it.kind)
        if hasattr(synth, "paths_for"):  # raw + processed (current FX) files
            keep |= set(synth.paths_for(it.text, voice.lang, it.direction))
        elif hasattr(synth, "key_for"):
            wav, ogg = synth.cache.paths(synth.key_for(it.text, voice.lang, it.direction))
            keep |= {wav, ogg}
    removed = 0
    for synth in {id(voice.narration): voice.narration, id(voice.lines): voice.lines}.values():
        cache = getattr(synth, "cache", None)
        if cache is None:
            continue
        fx_dir = cache.dir / "fx"  # processed clips (old FX settings are "unused" too)
        for f in (list(cache.dir.glob("*.wav")) + list(cache.dir.glob("*.ogg"))
                  + list(fx_dir.glob("*.wav")) + list(fx_dir.glob("*.ogg"))):
            if f not in keep:
                f.unlink(missing_ok=True)
                removed += 1
    return removed
