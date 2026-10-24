"""Tiny procedural character sounds (no samples, no network), cached as WAV.

    (frequencies / timbre scale with the preset: the Hertz presets use low, buzzy beeps)
    startup  — two soft rising notes (the robot wakes up)
    boop     — a questioning "boop?" (rising glide)
    chirp    — a delighted chirp (recognised a pose)
    puzzled  — a wobbly, confused little "hm?" (internet memes)
    chime    — a gentle closing chime (end of the show)

All are short (< 0.8 s), soft sine / gentle-FM tones with smooth envelopes, normalised
quietly. They are meant for a few key moments, NOT between sentences. Disable with
``VOICE_CUES=false``.

    bank = CueBank(cache_dir / "cues", preset.cues)
    path = bank.path("chirp")            # generates + caches on first use
    await bank.play("chirp", speaker)     # any object with `async play(path)`
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .character import CueSettings

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
CUE_VERSION = "cue1"
NAMES = ("startup", "boop", "chirp", "puzzled", "chime")
ALIASES = {
    "start": "startup", "hello": "startup", "wake": "startup",
    "question": "boop", "ask": "boop",
    "delight": "chirp", "recognised": "chirp", "recognized": "chirp", "yes": "chirp",
    "meme": "puzzled", "confused": "puzzled",
    "closing": "chime", "end": "chime", "goodbye": "chime",
}


def _env(n: int, rate: int, attack: float = 0.006, release: float = 0.05) -> np.ndarray:
    """Smooth attack + exponential-ish release, no clicks."""
    e = np.ones(n, dtype=np.float64)
    a = min(n, max(1, int(attack * rate)))
    r = min(n - a, max(1, int(release * rate)))
    e[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    if r > 0:
        e[n - r:] *= (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r)))
    return e


def _tone_base(f0: float, f1: float, dur: float, rate: int, *, fm_ratio: float = 2.0, fm_index: float = 0.6,
          vibrato_hz: float = 0.0, vibrato_st: float = 0.0, harmonic: float = 0.15, buzz: float = 0.0) -> np.ndarray:
    """A glide f0→f1 (exponential), gentle FM sparkle, optional wobble."""
    n = max(1, int(dur * rate))
    t = np.arange(n) / rate
    f = f0 * (f1 / f0) ** (t / max(dur, 1e-6))
    if vibrato_hz and vibrato_st:
        f = f * 2 ** (vibrato_st * np.sin(2 * np.pi * vibrato_hz * t) / 12)
    phase = 2 * np.pi * np.cumsum(f) / rate
    mod = fm_index * np.exp(-t * 18) * np.sin(fm_ratio * phase)  # sparkle only at the onset
    y = np.sin(phase + mod) + harmonic * np.sin(2 * phase)
    if buzz > 0:  # odd harmonics: square-ish electronic beep
        y = y + buzz * (np.sin(3 * phase) / 3 + np.sin(5 * phase) / 5 + np.sin(7 * phase) / 7)
    return y * _env(n, rate, release=min(0.08, dur * 0.6))


def _silence(dur: float, rate: int) -> np.ndarray:
    return np.zeros(int(dur * rate))


def synth_cue(name: str, *, rate: int = SAMPLE_RATE, pitch: float = 1.0, tempo: float = 1.0,
              variant: int = 0, buzz: float = 0.0) -> np.ndarray:
    """Float32 cue normalised to peak 1.0 (callers scale the volume)."""
    name = ALIASES.get(name, name)
    p, d = pitch, 1.0 / max(0.25, tempo)
    _tone = functools.partial(_tone_base, buzz=buzz)
    if name == "startup":
        parts = [_tone(660 * p, 700 * p, 0.11 * d, rate), _silence(0.04 * d, rate),
                 _tone(990 * p, 1040 * p, 0.16 * d, rate, fm_index=0.4)]
    elif name == "boop":
        parts = [_tone(520 * p, 500 * p, 0.09 * d, rate, fm_index=0.3), _silence(0.03 * d, rate),
                 _tone(560 * p, 860 * p, 0.17 * d, rate, fm_index=0.3)]
    elif name == "chirp":
        parts = [_tone(900 * p, 1500 * p, 0.07 * d, rate, fm_index=0.5), _silence(0.02 * d, rate),
                 _tone(1100 * p, 1900 * p, 0.09 * d, rate, fm_index=0.5)]
    elif name == "puzzled":
        parts = [_tone(700 * p, 520 * p, 0.16 * d, rate, vibrato_hz=9, vibrato_st=0.8, fm_index=0.3),
                 _silence(0.03 * d, rate),
                 _tone(560 * p, 760 * p, 0.14 * d, rate, vibrato_hz=7, vibrato_st=0.6, fm_index=0.3)]
    elif name == "chime":
        n = int(0.75 * d * rate)
        t = np.arange(n) / rate
        y = np.zeros(n)
        for k, (f, delay) in enumerate(((784 * p, 0.0), (1175 * p, 0.12 * d))):
            m = t >= delay
            tt = t[m] - delay
            y[m] += (0.8 if k else 1.0) * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 5.5)
            y[m] += 0.12 * np.sin(2 * np.pi * 2.76 * f * tt) * np.exp(-tt * 12)  # soft bell partial
        parts = [y * _env(n, rate, attack=0.004, release=0.12)]
    elif name == "tiny_chirp":  # used inside voice clips (speech/fx.py)
        base = (1250, 1400, 1150)[variant % 3] * p
        parts = [_tone(base, base * 1.35, 0.045, rate, fm_index=0.4, harmonic=0.05), _silence(0.02, rate),
                 _tone(base * 1.1, base * 1.6, 0.05, rate, fm_index=0.4, harmonic=0.05)]
    else:
        raise KeyError(f"unknown cue {name!r}; known: {', '.join(NAMES)}")
    y = np.concatenate(parts)
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / peak).astype(np.float32)


class CueBank:
    """Cached cue WAVs for one preset. `enabled=False` → every call is a silent no-op."""

    def __init__(self, directory: Path | None, settings: CueSettings | None = None):
        self.settings = settings or CueSettings(enabled=False)
        self.dir = Path(directory) if directory else None
        self.enabled = bool(self.settings.enabled and self.dir is not None)

    def _key(self, name: str) -> str:
        blob = json.dumps({"n": name, "s": asdict(self.settings), "v": CUE_VERSION}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    def path(self, name: str) -> Path | None:
        """The cue's WAV (generated on first use), or None when cues are off / unknown / failed."""
        if not self.enabled:
            return None
        name = ALIASES.get(name, name)
        if name not in NAMES:
            log.warning("unknown sound cue %r (known: %s)", name, ", ".join(NAMES))
            return None
        out = self.dir / f"{name}-{self._key(name)}.wav"
        if out.exists():
            return out
        try:
            from .fx import write_wav

            y = synth_cue(name, pitch=self.settings.pitch, tempo=self.settings.tempo, buzz=self.settings.buzz)
            write_wav(out, y * 0.6 * max(0.0, min(1.0, self.settings.volume)))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("could not make sound cue %s: %s", name, exc)
            return None

    def generate_all(self) -> dict[str, Path | None]:
        return {n: self.path(n) for n in NAMES}

    async def play(self, name: str, speaker) -> Path | None:
        """Play through any object with `async play(path)`; never raises."""
        p = self.path(name)
        if p is None or speaker is None or not getattr(speaker, "available", True):
            return None
        try:
            await speaker.play(p)
        except Exception as exc:  # noqa: BLE001 — cancellation (BaseException) still propagates
            log.warning("sound cue %s could not play: %s", name, exc)
        return p
