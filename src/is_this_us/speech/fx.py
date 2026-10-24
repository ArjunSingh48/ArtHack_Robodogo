"""Lightweight voice post-processing: a small robot character from a normal TTS voice.

Chain (every stage optional):

1. **FFmpeg** — pitch shift with timing preserved (up or down), independent formant
   shift, speaking rate, EQ (high/low-pass band-limiting, nasal peak, presence, air).
   * preferred: ``asetrate`` by the *formant* amount (moves pitch + formants together),
     then ``rubberband`` for the remaining pitch with formants preserved and tempo
     corrected → pitch +P st, formants +F st, duration / rate.
   * fallback when this FFmpeg has no librubberband: ``asetrate`` + ``atempo`` by the
     full pitch amount (formants follow pitch — a bit more "cartoon"; logged once).
2. **numpy** — synthetic coloration: channel vocoder (speech envelope in N bands on a
   fixed-pitch sawtooth+noise carrier, blended with the dry voice = electronic buzz),
   ring modulation (sine or saw carrier), chorus, a short comb "hollow body / tin can"
   resonance, gentle pitch wobble, an occasional tiny chirp (deterministic per clip, so
   caching stays stable), loudness normalisation with a soft ceiling (never clips).

If FFmpeg is missing or anything fails, :func:`process_file` returns ``None`` and logs a
warning; callers then use the unprocessed audio. The show never stops because of FX.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import math
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .character import FxParams

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
PROCESSOR_VERSION = "fx2"  # bump when the algorithm changes (part of the processed-audio cache key)
_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


@dataclass
class FxResult:
    path: Path
    method: str  # "rubberband" | "asetrate" | "numpy-only"
    seconds: float


# ---------------------------------------------------------------------- wav io
def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float32 in [-1, 1]. Accepts PCM16/PCM32/8-bit, any channel count."""
    with wave.open(str(path)) as w:
        n, ch, sw, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def write_wav(path: Path, x: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    tmp.replace(path)  # atomic: a half-written clip is never played


def measure(x: np.ndarray, rate: int = SAMPLE_RATE) -> dict:
    """Duration, peak and speech-active RMS (dBFS), clipped-sample fraction."""
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return {"seconds": x.size / float(rate or 1), "peak_dbfs": _db(peak), "rms_dbfs": _db(active_rms(x, rate)),
            "clipped": float(np.mean(np.abs(x) >= 0.999)) if x.size else 0.0}


def _db(v: float) -> float:
    return 20 * math.log10(v) if v > 1e-9 else -120.0


def active_rms(x: np.ndarray, rate: int = SAMPLE_RATE, gate_db: float = -40.0) -> float:
    """RMS over 20 ms frames that are within `gate_db` of the loudest frame (ignores pauses)."""
    if x.size == 0:
        return 0.0
    hop = max(1, int(rate * 0.02))
    n = x.size // hop
    if n == 0:
        return float(np.sqrt(np.mean(x ** 2)))
    frames = x[: n * hop].reshape(n, hop)
    e = np.sqrt(np.mean(frames ** 2, axis=1))
    loud = e >= e.max() * 10 ** (gate_db / 20)
    return float(np.sqrt(np.mean(frames[loud] ** 2))) if loud.any() else 0.0


# ---------------------------------------------------------------------- ffmpeg stage
def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@functools.lru_cache(maxsize=8)
def has_filter(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)


def _atempo_chain(factor: float) -> list[str]:
    """atempo accepts 0.5..2 per instance on older FFmpeg builds: chain if needed."""
    out = []
    while factor > 2.0:
        out.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        out.append("atempo=0.5")
        factor /= 0.5
    if abs(factor - 1.0) > 1e-4:
        out.append(f"atempo={factor:.6f}")
    return out


def ffmpeg_filters(fx: FxParams, rate: int, *, rubberband: bool) -> tuple[list[str], str]:
    """The -af chain for pitch / formant / rate / EQ / wobble, and the method name."""
    chain: list[str] = []
    method = "none"
    p, f, r = fx.pitch_semitones, fx.formant_semitones, max(0.5, min(2.0, fx.rate or 1.0))
    if abs(p) > 1e-3 or abs(f) > 1e-3 or abs(r - 1.0) > 1e-3:
        if rubberband:
            f = max(-12.0, min(12.0, f))
            rf = 2 ** (f / 12)
            if abs(f) > 1e-3:  # resample: pitch AND formants up by f, duration / rf
                chain += [f"asetrate={rate * rf:.3f}", f"aresample={rate}"]
            rp = 2 ** ((p - f) / 12)  # remaining pitch, formants untouched
            tempo = r / rf  # undo the speed-up, then apply speaking rate
            chain.append(f"rubberband=pitch={rp:.6f}:tempo={tempo:.6f}:formant=preserved:pitchq=quality:"
                         "transients=smooth:window=standard")
            method = "rubberband"
        else:
            rp = 2 ** (p / 12)
            if abs(p) > 1e-3:
                chain += [f"asetrate={rate * rp:.3f}", f"aresample={rate}"]
            chain += _atempo_chain(r / rp)
            method = "asetrate"
    if fx.highpass_hz > 0:
        chain.append(f"highpass=f={fx.highpass_hz:.1f}:poles=2")
    if fx.lowpass_hz > 0:
        chain.append(f"lowpass=f={fx.lowpass_hz:.1f}:poles=2")
    if fx.low_mid_cut_db:
        chain.append(f"equalizer=f=280:t=q:w=1.0:g={fx.low_mid_cut_db:.2f}")
    if fx.nasal_hz > 0 and fx.nasal_db:
        chain.append(f"equalizer=f={fx.nasal_hz:.1f}:t=q:w={max(0.3, fx.nasal_q):.2f}:g={fx.nasal_db:.2f}")
    if fx.presence_db:
        chain.append(f"equalizer=f=3200:t=q:w=1.2:g={fx.presence_db:.2f}")
    if fx.air_db:
        chain.append(f"highshelf=f=8000:g={fx.air_db:.2f}")
    return chain, method


def run_ffmpeg(x: np.ndarray, rate: int, chain: list[str], ffmpeg: str, timeout_s: float = 30.0) -> np.ndarray:
    # float32 in and out: EQ / pitch may push peaks above 0 dBFS inside FFmpeg without clipping;
    # loudness normalisation with a soft ceiling follows in numpy.
    pcm = np.ascontiguousarray(x, dtype="<f4").tobytes()
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
           "-af", ",".join(chain), "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "pipe:1"]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip()[:300] or f"ffmpeg exit {proc.returncode}")
    y = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)
    if not np.all(np.isfinite(y)):
        raise RuntimeError("ffmpeg returned non-finite samples")
    return y


def to_mono_24k(src: Path, dst: Path, ffmpeg: str | None = None) -> Path:
    """Convert any audio file FFmpeg can read into 24 kHz mono PCM16 (for --offline-input)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if ffmpeg is None:
        x, rate = read_wav(src)
        if rate != SAMPLE_RATE:
            raise RuntimeError("ffmpeg is needed to resample the offline input to 24 kHz")
        write_wav(dst, x)
        return dst
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1",
                    "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)], check=True, timeout=60)
    return dst


# ---------------------------------------------------------------------- numpy stage
def chorus(x: np.ndarray, mix: float, rate: int = SAMPLE_RATE, base_ms: float = 13.0, depth_ms: float = 2.0,
           speed_hz: float = 0.7) -> np.ndarray:
    """One modulated, slightly detuned copy: a synthetic 'doubled' texture."""
    if mix <= 0 or x.size == 0:
        return x
    n = np.arange(x.size, dtype=np.float64)
    delay = (base_ms + depth_ms * np.sin(2 * np.pi * speed_hz * n / rate)) * rate / 1000.0
    pos = n - delay
    i0 = np.clip(np.floor(pos).astype(np.int64), 0, x.size - 1)
    i1 = np.clip(i0 + 1, 0, x.size - 1)
    frac = (pos - np.floor(pos)).astype(np.float32)
    wet = x[i0] * (1 - frac) + x[i1] * frac
    wet[pos < 0] = 0.0
    return (x * (1 - mix * 0.5) + wet * mix).astype(np.float32)


def wobble(x: np.ndarray, semitones: float, hz: float, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Gentle, irregular pitch fluctuation (two incommensurate slow LFOs on a short delay line).

    Peak deviation ≈ `semitones`; keep ≤ 0.4 — more sounds seasick / vibrato-like.
    (FFmpeg 6's `vibrato` filter returns inf samples on float input, so this is done in numpy.)
    """
    if semitones <= 0 or hz <= 0 or x.size == 0:
        return x
    dev = 2 ** (min(semitones, 1.0) / 12) - 1  # relative frequency deviation
    depth_s = dev / (2 * np.pi * hz)  # delay modulation amplitude giving that deviation
    n = np.arange(x.size, dtype=np.float64)
    t = n / rate
    lfo = 0.7 * np.sin(2 * np.pi * hz * t) + 0.3 * np.sin(2 * np.pi * hz * 0.37 * t + 1.3)
    delay = (depth_s * (1 + lfo) + 0.0005) * rate
    pos = n - delay
    i0 = np.clip(np.floor(pos).astype(np.int64), 0, x.size - 1)
    i1 = np.clip(i0 + 1, 0, x.size - 1)
    frac = (pos - np.floor(pos)).astype(np.float32)
    return (x[i0] * (1 - frac) + x[i1] * frac).astype(np.float32)


def _saw(hz: float, n: int, rate: int, harmonics: int | None = None) -> np.ndarray:
    """Band-limited sawtooth (additive), peak ≈ 1."""
    t = np.arange(n, dtype=np.float64) / rate
    k_max = harmonics or max(1, int((rate / 2 * 0.9) // hz))
    y = np.zeros(n)
    for k in range(1, k_max + 1):
        y += np.sin(2 * np.pi * k * hz * t) / k
    return (y / (np.max(np.abs(y)) or 1.0)).astype(np.float32)


def ring_mod(x: np.ndarray, mix: float, hz: float, rate: int = SAMPLE_RATE, shape: str = "sine") -> np.ndarray:
    """Ring modulation blend (electronic edge / buzz with a saw carrier). Large mixes = Dalek: keep < 0.15."""
    if mix <= 0 or x.size == 0:
        return x
    if shape == "saw":
        carrier = _saw(hz, x.size, rate, harmonics=12)
    else:
        carrier = np.sin(2 * np.pi * hz * np.arange(x.size, dtype=np.float64) / rate).astype(np.float32)
    return (x * (1 - mix) + x * carrier * mix).astype(np.float32)


def _stft(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    win = np.hanning(n_fft).astype(np.float32)
    pad = np.concatenate([np.zeros(n_fft, np.float32), x, np.zeros(n_fft, np.float32)])
    n = 1 + (pad.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    return np.fft.rfft(pad[idx] * win, axis=1)


def _istft(spec: np.ndarray, n_fft: int, hop: int, length: int) -> np.ndarray:
    win = np.hanning(n_fft).astype(np.float32)
    frames = np.fft.irfft(spec, n=n_fft, axis=1).astype(np.float32) * win
    n = frames.shape[0]
    out = np.zeros(hop * (n - 1) + n_fft, np.float32)
    norm = np.zeros_like(out)
    for i in range(n):  # overlap-add (n is a few thousand frames at most)
        out[i * hop: i * hop + n_fft] += frames[i]
        norm[i * hop: i * hop + n_fft] += win ** 2
    out /= np.maximum(norm, 1e-6)
    return out[n_fft: n_fft + length]


def vocoder(x: np.ndarray, mix: float, carrier_hz: float = 100.0, bands: int = 24, noise: float = 0.25,
            rate: int = SAMPLE_RATE, lo_hz: float = 80.0, hi_hz: float = 7500.0) -> np.ndarray:
    """Channel vocoder: the voice's spectral envelope (log-spaced bands) imposed on a fixed-pitch
    sawtooth (+ a little noise for consonants), blended with the dry voice. Monotone electronic buzz."""
    if mix <= 0 or x.size == 0:
        return x
    n_fft, hop = 1024, 256
    sx = _stft(x, n_fft, hop)
    rng = np.random.default_rng(0)
    carrier = _saw(carrier_hz, x.size, rate) + noise * rng.standard_normal(x.size).astype(np.float32) * 0.5
    sc = _stft(carrier.astype(np.float32), n_fft, hop)
    freqs = np.fft.rfftfreq(n_fft, 1 / rate)
    edges = np.geomspace(lo_hz, hi_hz, bands + 1)
    gain = np.zeros(sx.shape, np.float32)
    ex, ec = np.abs(sx) ** 2, np.abs(sc) ** 2
    for b in range(bands):
        m = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if not m.any():
            continue
        es = ex[:, m].mean(axis=1)
        e_c = ec[:, m].mean(axis=1)
        gain[:, m] = np.sqrt(es / np.maximum(e_c, 1e-12))[:, None]
    wet = _istft(sc * gain, n_fft, hop, x.size)
    # match the wet level to the dry level so `mix` is a true blend
    wr, dr = float(np.sqrt(np.mean(wet ** 2)) or 1.0), float(np.sqrt(np.mean(x ** 2)))
    wet *= dr / wr
    return (x * (1 - mix) + wet * mix).astype(np.float32)


def resonance(x: np.ndarray, mix: float, hz: float, rate: int = SAMPLE_RATE, taps: int = 4,
              decay: float = 0.45) -> np.ndarray:
    """Short FIR comb (a few decaying echoes 1/hz apart): 'hollow body' / 'tin can', no long ringing."""
    if mix <= 0 or x.size == 0 or hz <= 0:
        return x
    d = max(1, round(rate / hz))
    wet = np.zeros_like(x)
    g = 1.0
    for k in range(1, taps + 1):
        g *= decay
        if k * d >= x.size:
            break
        wet[k * d:] += g * x[: x.size - k * d]
    return (x + wet * mix).astype(np.float32)


def chirp_sound(rate: int = SAMPLE_RATE, seed: int = 0) -> np.ndarray:
    """A tiny soft rising two-blip chirp (~110 ms), peak 1.0."""
    from .cues import synth_cue

    return synth_cue("tiny_chirp", rate=rate, variant=seed)


def maybe_chirp(x: np.ndarray, fx: FxParams, seed: str, rate: int = SAMPLE_RATE) -> np.ndarray:
    """With probability fx.chirp_probability (deterministic per seed), put one tiny chirp before the clip."""
    if fx.chirp_probability <= 0 or not seed or x.size == 0:
        return x
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    if (h % 10_000) / 10_000.0 >= fx.chirp_probability:
        return x
    c = chirp_sound(rate, seed=h % 3)
    peak = float(np.max(np.abs(x))) or 1.0
    gap = np.zeros(int(0.07 * rate), dtype=np.float32)
    return np.concatenate([c * fx.chirp_level * peak, gap, x]).astype(np.float32)


def normalize(x: np.ndarray, target_rms_dbfs: float = -19.0, peak_dbfs: float = -1.5,
              rate: int = SAMPLE_RATE) -> np.ndarray:
    """Speech-active RMS to the target, then a soft ceiling so peaks never clip."""
    if x.size == 0:
        return x
    rms = active_rms(x, rate)
    if rms <= 1e-6:
        return x
    y = x * (10 ** (target_rms_dbfs / 20) / rms)
    ceiling = 10 ** (peak_dbfs / 20)
    knee = ceiling * 10 ** (-4.0 / 20)  # soft knee 4 dB below the ceiling
    a = np.abs(y)
    over = a > knee
    if over.any():
        span = ceiling - knee
        y = y.copy()
        y[over] = np.sign(y[over]) * (knee + span * np.tanh((a[over] - knee) / span))
    return y.astype(np.float32)


# ---------------------------------------------------------------------- public api
def process_array(x: np.ndarray, rate: int, fx: FxParams, *, seed: str = "",
                  ffmpeg: str | None = "auto") -> tuple[np.ndarray, str]:
    """Raises on failure (callers that must not fail use :func:`process_file`)."""
    if ffmpeg == "auto":
        ffmpeg = find_ffmpeg()
    chain: list[str] = []
    method = "numpy-only"
    if ffmpeg:
        chain, m = ffmpeg_filters(fx, rate, rubberband=has_filter(ffmpeg, "rubberband"))
        if m == "asetrate":
            _warn_once("no-rubberband", "this FFmpeg has no rubberband filter: pitch via asetrate+atempo "
                       "(formants follow pitch). Install an FFmpeg built with librubberband for the intended sound.")
        method = m if m != "none" else method
    else:
        chain, _ = ffmpeg_filters(fx, rate, rubberband=False)
        if chain:
            raise RuntimeError("ffmpeg not found (needed for pitch / rate / EQ)")
    if chain:
        x = run_ffmpeg(x, rate, chain, ffmpeg)
    elif rate != SAMPLE_RATE:
        raise RuntimeError("input is not 24 kHz and no FFmpeg stage ran")
    x = wobble(x, fx.wobble_semitones, fx.wobble_hz)
    x = vocoder(x, fx.vocoder_mix, fx.vocoder_hz, fx.vocoder_bands, fx.vocoder_noise)
    x = chorus(x, fx.chorus_mix)
    x = ring_mod(x, fx.ring_mix, fx.ring_hz, shape=fx.ring_shape)
    x = resonance(x, fx.resonance_mix, fx.resonance_hz, taps=fx.resonance_taps, decay=fx.resonance_decay)
    x = maybe_chirp(x, fx, seed)
    x = normalize(x, fx.target_rms_dbfs, fx.peak_dbfs)
    return x, method


def process_file(src: Path, dst: Path, fx: FxParams, *, seed: str = "", ffmpeg: str | None = "auto") -> FxResult | None:
    """Process `src` into `dst`. Returns None (and logs) on any failure — use the raw audio then."""
    try:
        x, rate = read_wav(src)
        y, method = process_array(x, rate, fx, seed=seed, ffmpeg=ffmpeg)
        if y.size == 0 or not np.all(np.isfinite(y)):
            raise RuntimeError("processing produced no (valid) audio")
        write_wav(dst, y)
        return FxResult(dst, method, y.size / SAMPLE_RATE)
    except Exception as exc:  # noqa: BLE001 — never stop the show for an effect
        _warn_once(f"fx-fail:{type(exc).__name__}:{str(exc)[:60]}",
                   "voice processing failed (%s); using the unprocessed voice", exc)
        return None


def fx_key(fx: FxParams) -> str:
    return f"{PROCESSOR_VERSION}-{fx.fingerprint()}"
