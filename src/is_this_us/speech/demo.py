"""`itu voice-demo` — audition the voice presets with the same sample text and fair pauses.

Base voices ("engines"):

* ``azure``  — the real show voice: each preset's base voice reads each line with the preset's
  character instructions (Azure OpenAI Realtime; needs AZURE_OPENAI_API_KEY). Raw takes are
  cached under AUDIO_CACHE_DIR/voice_demo/, so re-running after FX changes costs no API call.
  ``base__azure.wav`` is the CURRENT show voice (VOICE_PRESET=off) for comparison.
* ``espeak`` — local eSpeak NG (``espeak-ng`` on PATH): robotic stand-in base, no keys.
* ``piper``  — local neural TTS (``pip install piper-tts`` + a voice ``.onnx``; ``--piper-model``
  or PIPER_MODEL): a more natural stand-in base, no keys.
* ``--offline-input FILE`` — any existing recording as the base.

Local engines only audition the *processing*: the character instructions affect only the
Azure model's delivery.

Pause markers: lines are synthesized one by one and joined with silences, so timing is
identical across experiments. ``[pause]`` = 0.7 s, ``[long pause]`` = 1.8 s,
``[pause 1.2]`` = 1.2 s; consecutive lines without a marker get 0.35 s.

Output (default ./voice_previews/, git-ignored), per engine E:
    base__E.wav                    unprocessed base (local engines / offline input; Azure: current voice)
    <preset>__E.raw.wav            Azure only: preset base voice + instructions, unprocessed
    <preset>__E.wav                processed — what the show would play
    <first preset>__pitch-2__E.wav … pitch comparison (other FX equal)
    cues/<preset>/<cue>.wav        procedural sound cues
    + .mp3 / .ogg copies (``--formats``) for phones
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np

from .character import CHARACTER_PRESETS, PRESETS, VoicePreset, resolve_preset
from .cues import NAMES as CUE_NAMES
from .cues import CueBank
from .fx import (
    SAMPLE_RATE,
    find_ffmpeg,
    has_filter,
    measure,
    normalize,
    process_array,
    read_wav,
    to_mono_24k,
    write_wav,
)

SAMPLE_TEXT = ("Hello. I am a dog.\n[pause]\nI have four legs.\n[pause]\nI do not have a tail.\n"
               "[long pause]\nThis is concerning.")
PAUSES = {"pause": 0.7, "short pause": 0.7, "long pause": 1.8}
LINE_GAP_S = 0.35
LEAD_S, TAIL_S = 0.25, 0.5
PITCH_COMPARE = (-2.0, -3.0, -4.0)
ENGINES = ("azure", "espeak", "piper")

Segment = str | float  # a line to speak, or seconds of silence


# ---------------------------------------------------------------------- script with pauses
def parse_script(text: str) -> list[Segment]:
    """Lines and pause markers → [line, seconds, line, …] with exactly one silence between lines."""
    out: list[Segment] = []
    pending: float | None = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"\[\s*(long pause|short pause|pause)\s*([0-9.]+)?\s*s?\s*\]", line, flags=re.IGNORECASE)
        if m:
            secs = float(m.group(2)) if m.group(2) else PAUSES[m.group(1).lower()]
            pending = (pending or 0.0) + secs
            continue
        if out:
            out.append(pending if pending is not None else LINE_GAP_S)
        out.append(line)
        pending = None
    return out


def trim_silence(x: np.ndarray, rate: int = SAMPLE_RATE, threshold_db: float = -45.0,
                 margin_s: float = 0.03) -> np.ndarray:
    """Strip an engine's own leading/trailing silence so the pause markers alone set the timing."""
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x))) or 1.0
    loud = np.nonzero(np.abs(x) >= peak * 10 ** (threshold_db / 20))[0]
    if loud.size == 0:
        return x[:0]
    m = int(margin_s * rate)
    return x[max(0, loud[0] - m): min(x.size, loud[-1] + m)]


def assemble(segments: list[Segment], speak: Callable[[str], np.ndarray]) -> np.ndarray:
    parts = [np.zeros(int(LEAD_S * SAMPLE_RATE), np.float32)]
    for seg in segments:
        if isinstance(seg, str):
            parts.append(trim_silence(speak(seg)).astype(np.float32))
        else:
            parts.append(np.zeros(int(seg * SAMPLE_RATE), np.float32))
    parts.append(np.zeros(int(TAIL_S * SAMPLE_RATE), np.float32))
    return np.concatenate(parts)


# ---------------------------------------------------------------------- engines (line → 24 kHz mono)
def _to_array(path: Path, tmp: Path) -> np.ndarray:
    x, _ = read_wav(to_mono_24k(path, tmp / "conv.wav"))
    return x


def espeak_engine(voice: str = "en-us+m3", pitch: int = 50, speed: int = 140) -> Callable[[str], np.ndarray]:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        raise RuntimeError("espeak-ng not found (Linux: apt install espeak-ng; macOS: brew install espeak-ng)")

    def speak(text: str) -> np.ndarray:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "line.wav"
            subprocess.run([exe, "-v", voice, "-p", str(pitch), "-s", str(speed), "-w", str(out), text],
                           check=True, timeout=30, capture_output=True)
            return _to_array(out, Path(d))
    return speak


def piper_engine(model: str | Path) -> Callable[[str], np.ndarray]:
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise RuntimeError("piper-tts not installed (pip install piper-tts)") from exc
    model = Path(model)
    if not model.exists():
        raise RuntimeError(f"piper voice model not found: {model} (download an .onnx + .onnx.json voice)")
    voice = PiperVoice.load(str(model))

    def speak(text: str) -> np.ndarray:
        import wave

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "line.wav"
            with wave.open(str(out), "wb") as w:
                voice.synthesize_wav(text, w)
            return _to_array(out, Path(d))
    return speak


def azure_engine(settings, preset: VoicePreset, lang: str = "en",
                 direction: str = "") -> Callable[[str], np.ndarray]:
    """One preset's base voice + instructions, line by line, raw (no FX). Cached."""
    from .synthesizer import AudioCache, OpenAIRealtimeSynthesizer

    voice = settings.primary_voice if preset.is_off else preset.base_voice
    synth = OpenAIRealtimeSynthesizer(settings.azure_openai_api_key, settings.openai_narration_model, voice,
                                      AudioCache(Path(settings.audio_cache_dir) / "voice_demo"),
                                      voice_notes=False, character=None if preset.is_off else preset)

    def speak(text: str) -> np.ndarray:
        raw, _ = synth.cache.paths(synth.key_for(text, lang, direction))
        if not raw.exists():
            synth.synthesize(text, lang, direction)
        if not raw.exists():
            raise RuntimeError(f"Azure synthesis failed for {text!r} (see log)")
        x, _ = read_wav(raw)
        return x
    return speak


# ---------------------------------------------------------------------- output helpers
def _fmt(x: np.ndarray) -> str:
    m = measure(x)
    return (f"{m['seconds']:5.2f} s  peak {m['peak_dbfs']:6.1f} dBFS  rms {m['rms_dbfs']:6.1f} dBFS"
            + (f"  CLIPPED {m['clipped'] * 100:.2f}%" if m["clipped"] > 0 else ""))


def export(path: Path, formats: list[str], ffmpeg: str | None) -> list[Path]:
    """Small phone-playable copies next to the WAV (mp3 = libmp3lame, ogg = Opus)."""
    out: list[Path] = []
    if not ffmpeg:
        return out
    codecs = {"mp3": ["-c:a", "libmp3lame", "-q:a", "4"], "ogg": ["-c:a", "libopus", "-b:a", "48k"]}
    for f in formats:
        if f not in codecs:
            continue
        dst = path.with_suffix(f".{f}")
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), *codecs[f],
                            str(dst)], capture_output=True, timeout=60, check=False)
        if r.returncode == 0:
            out.append(dst)
    return out


def _presets(settings, names: str | None) -> list[VoicePreset]:
    wanted = CHARACTER_PRESETS if not names or names == "all" else [n.strip() for n in names.split(",") if n.strip()]
    return [resolve_preset(settings, name=n) for n in wanted]


def _engine_names(settings, engines: str, offline_input) -> list[str] | str:
    """Engine list, or an error message."""
    if offline_input is not None:
        return ["input"]
    if engines == "auto":
        if settings.voice_configured:
            return ["azure"]
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            return ["espeak"]
        return ("No AZURE_OPENAI_API_KEY and no espeak-ng: nothing to synthesize with.\n"
                "Use --engine espeak|piper, or --offline-input some_recording.wav")
    names = [e.strip() for e in engines.split(",") if e.strip()]
    if "azure" in names and not settings.voice_configured:
        return ("--engine azure needs AZURE_OPENAI_API_KEY (and OPENAI_VOICE_PROVIDER=openai).\n"
                "Without a key: --engine espeak / --engine piper --piper-model …onnx / --offline-input FILE")
    return names


# ---------------------------------------------------------------------- main
def run_demo(settings, *, presets: str | None = None, out: Path = Path("voice_previews"), play: bool = False,
             offline_input: Path | None = None, text: str | None = None, lang: str = "en",
             direction: str = "", base: bool = True, pitch_compare: bool = True, cues: bool = True,
             engines: str = "auto", formats: str = "mp3", espeak_voice: str = "en-us+m3", espeak_pitch: int = 50,
             espeak_speed: int = 140, piper_model: str | None = None, printer=print) -> int:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    segments = parse_script(text or SAMPLE_TEXT)
    chosen = _presets(settings, presets)
    fmts = [f.strip().lower() for f in (formats or "").split(",") if f.strip() and f.strip().lower() != "wav"]
    ffmpeg = find_ffmpeg()
    printer(f"ffmpeg: {ffmpeg or 'NOT FOUND (processing disabled → raw voice)'}"
            + (f"; rubberband: {'yes' if has_filter(ffmpeg, 'rubberband') else 'no (asetrate fallback)'}"
               if ffmpeg else ""))
    printer("sample: " + " ".join(s if isinstance(s, str) else f"[{s:g}s]" for s in segments))
    names = _engine_names(settings, engines, offline_input)
    if isinstance(names, str):
        printer(names)
        return 2

    failed = 0
    written: list[Path] = []

    def save(name: str, x: np.ndarray, note: str = "") -> Path:
        dst = out / f"{name}.wav"
        write_wav(dst, x)
        extra = export(dst, fmts, ffmpeg)
        written.append(dst)
        printer(f"{dst.name:<44}{_fmt(x)}  {note}"
                + (f"  (+{', '.join(p.suffix[1:] for p in extra)})" if extra else ""))
        return dst

    def processed(name: str, raw: np.ndarray, p: VoicePreset, note: str) -> None:
        try:
            y, method = process_array(raw, SAMPLE_RATE, p.fx, seed=f"demo:{p.name}")
            save(name, y, f"[{note}; {method}]")
        except Exception as exc:  # noqa: BLE001 — the show falls back the same way
            save(name, raw, f"[processing FAILED: {exc} → unprocessed]")

    for engine in names:
        speak_for = None
        try:
            if engine == "input":
                src = to_mono_24k(Path(offline_input), out / "_input_24k.wav", ffmpeg)
                base_raw, _ = read_wav(src)
                src.unlink(missing_ok=True)
            elif engine == "espeak":
                base_raw = assemble(segments, espeak_engine(espeak_voice, espeak_pitch, espeak_speed))
            elif engine == "piper":
                model = piper_model or os.environ.get("PIPER_MODEL", "")
                if not model:
                    raise RuntimeError("give --piper-model path/to/voice.onnx (or PIPER_MODEL)")
                base_raw = assemble(segments, piper_engine(model))
            elif engine == "azure":
                base_raw = assemble(segments, azure_engine(settings, PRESETS["off"], lang, direction))

                def speak_for(p):
                    return assemble(segments, azure_engine(settings, p, lang, direction))
            else:
                raise RuntimeError(f"unknown engine (choose {', '.join(ENGINES)})")
        except Exception as exc:  # noqa: BLE001
            printer(f"{engine}: base voice FAILED — {exc}")
            failed += 1
            continue
        if base:  # unprocessed, only loudness-matched to the processed files (fair A/B)
            save(f"base__{engine}", normalize(base_raw),
                 "[current show voice, unprocessed]" if engine == "azure" else "[unprocessed base]")
        raw_for: dict[str, np.ndarray] = {}
        for p in chosen:
            raw = base_raw
            if speak_for is not None:
                try:
                    raw = speak_for(p)
                except Exception as exc:  # noqa: BLE001
                    printer(f"{p.name}__{engine}: synthesis FAILED — {exc}")
                    failed += 1
                    continue
                if base:
                    save(f"{p.name}__{engine}.raw", normalize(raw), f"[{p.base_voice} + instructions, unprocessed]")
            raw_for[p.name] = raw
            src_name = p.base_voice if engine == "azure" else engine
            processed(f"{p.name}__{engine}", raw, p,
                      f"{src_name}; pitch {p.fx.pitch_semitones:+g} st, rate {p.fx.rate:g}")
        if pitch_compare and raw_for:
            first = next(p for p in chosen if p.name in raw_for)
            for st in PITCH_COMPARE:
                processed(f"{first.name}__pitch{st:+g}__{engine}", raw_for[first.name],
                          replace(first, fx=replace(first.fx, pitch_semitones=st)), f"{st:+g} st")

    if cues:
        for p in chosen:
            bank = CueBank(Path(settings.audio_cache_dir) / "voice_demo" / "cues" / p.name,
                           replace(p.cues, enabled=True))
            made = []
            for n in CUE_NAMES:
                src = bank.path(n)
                if src:
                    dst = out / "cues" / p.name / f"{n}.wav"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    export(dst, fmts, ffmpeg)
                    made.append(n)
            printer(f"cues/{p.name}/: {', '.join(made)}")

    if play and written:
        from ..performance.voice import Speaker

        speaker = Speaker(enabled=True)
        if not speaker.available:
            printer("no audio player found (ffplay / afplay / aplay / paplay) — files are saved")
        else:
            for f in written:
                printer(f"▶ {f.name}")
                asyncio.run(speaker.play(f))
    printer(f"→ {out.resolve()}")
    return 1 if failed else 0
