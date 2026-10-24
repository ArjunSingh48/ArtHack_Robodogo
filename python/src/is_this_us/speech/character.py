"""The robot's voice *character*: presets = base voice + delivery instructions + audio FX + cues.

Direction (2026-10-24): a small physical robot dog with an unexpectedly LOW, synthetic,
slightly nasal, buzzy, deadpan voice — inspired by "Hertz", the talking delivery robot dog
of the retailer re. Not a natural adult speaker, not booming bass, not a high cartoon voice.
Cute through contrast: small body, serious low machine voice, awkward pauses.

Three experiments of the SAME character:

* ``hertz_deadpan``  — low-mid voice, flat mechanical delivery, light buzz, nasal band, small hollow body.
* ``hertz_buzzy``    — more electronic: channel-vocoder buzz on a fixed low carrier blended with the voice.
* ``hertz_tin_can``  — hollow, boxy small-speaker robot: band-limited, resonant tin-can comb.

A preset bundles

* ``base_voice``   — an OpenAI voice that exists in BOTH Realtime and GPT-Live
  (alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar). ``VOICE_BASE_VOICE`` overrides it.
* ``character``    — delivery text added to the Realtime session ``instructions`` (the only
  style control the Realtime / GPT-Live endpoints have; no SSML, no extra parameters).
  Personality comes from delivery and sound — never from rewriting the text.
* ``fx``           — local post-processing (``speech/fx.py``).
* ``cues``         — optional procedural sound cues (``speech/cues.py``).

The default is ``VOICE_PRESET=off`` — the previous voice, unchanged — until the team has
listened to the previews and picks one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace

log = logging.getLogger(__name__)

# Voices accepted by BOTH the Realtime API and GPT-Live (see docs/VOICE.md).
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")

PRESET_OFF = "off"
DEFAULT_PRESET = PRESET_OFF  # do not change the performance voice until the team picks one
RECOMMENDED_PRESET = "hertz_deadpan"


@dataclass(frozen=True)
class FxParams:
    """Post-processing parameters. 0 disables a stage."""

    pitch_semitones: float = 0.0  # perceived pitch, timing preserved (negative = lower)
    formant_semitones: float = 0.0  # vocal-tract size; keep near 0 (− = bigger, + = smaller)
    rate: float = 1.0  # speaking rate (1.0 = unchanged)
    highpass_hz: float = 0.0
    lowpass_hz: float = 0.0  # band-limiting ("small speaker"); 0 = off
    low_mid_cut_db: float = 0.0  # peaking EQ at 280 Hz
    nasal_hz: float = 0.0  # peaking EQ centre for a slightly nasal colour (~1–2 kHz)
    nasal_db: float = 0.0
    nasal_q: float = 1.4
    presence_db: float = 0.0  # peaking EQ at 3.2 kHz (intelligibility)
    air_db: float = 0.0  # high shelf at 8 kHz
    vocoder_mix: float = 0.0  # channel vocoder (speech envelope on a fixed-pitch buzz carrier), blended
    vocoder_hz: float = 100.0  # carrier pitch (monotone robot)
    vocoder_bands: int = 24
    vocoder_noise: float = 0.25  # noise in the carrier, keeps consonants intelligible
    ring_mix: float = 0.0  # ring modulation blend (electronic edge)
    ring_hz: float = 90.0
    ring_shape: str = "sine"  # sine | saw (buzzier)
    chorus_mix: float = 0.0
    resonance_mix: float = 0.0  # short comb ("hollow body" / "tin can")
    resonance_hz: float = 200.0  # 1 / delay: 3–8 ms ↔ 125–333 Hz
    resonance_taps: int = 4
    resonance_decay: float = 0.45
    wobble_semitones: float = 0.0  # gentle irregular pitch fluctuation
    wobble_hz: float = 3.5
    chirp_probability: float = 0.0  # chance of ONE tiny chirp before a clip (deterministic per clip)
    chirp_level: float = 0.18
    target_rms_dbfs: float = -19.0  # loudness normalisation (speech-active RMS)
    peak_dbfs: float = -1.5  # soft ceiling, never clips

    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    def is_identity(self) -> bool:
        return self == FxParams(target_rms_dbfs=self.target_rms_dbfs, peak_dbfs=self.peak_dbfs)


@dataclass(frozen=True)
class CueSettings:
    enabled: bool = True
    volume: float = 0.5  # 0..1 (cues are additionally normalised quiet)
    pitch: float = 1.0  # multiplies every cue frequency
    tempo: float = 1.0  # >1 = snappier
    buzz: float = 0.0  # 0..1 odd harmonics: a square-ish, electronic beep


@dataclass(frozen=True)
class VoicePreset:
    name: str
    base_voice: str
    character: str
    fx: FxParams = field(default_factory=FxParams)
    cues: CueSettings = field(default_factory=CueSettings)
    description: str = ""

    @property
    def is_off(self) -> bool:
        return self.name == PRESET_OFF

    def fingerprint(self) -> str:
        """Everything that changes the *generated* audio (not FX): name + voice + character."""
        blob = json.dumps({"n": self.name, "v": self.base_voice, "c": self.character}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]


# Shared by every preset: how the reader must express the character.
CHARACTER_RULES = (
    "Express the character ONLY through the sound of your voice: pitch, tone, rhythm, tempo, pauses and "
    "intonation. Read every word exactly as written and pronounce it clearly — no changed words, no added "
    "words, no beeps, no robot noises, no barking, no laughs. A museum audience must understand every word. "
    "You are NOT a natural adult narrator, NOT a radio announcer, NOT a booming movie-trailer voice and NOT a "
    "high cartoon voice. The humour comes only from contrast and timing: completely serious about small things."
)

HERTZ_DEADPAN = VoicePreset(
    name="hertz_deadpan",
    base_voice="ash",
    description="low-mid, deadpan and mechanical; light buzz, nasal band, small hollow body",
    character=(
        "Your voice is a small robot dog with an unexpectedly low, serious machine voice: low-mid pitch (not "
        "bass), dry, flat and deadpan, matter-of-fact. Slightly mechanical articulation: each word placed "
        "precisely, like a machine reading a status report. Very little emotional colour. Funny, slightly "
        "awkward pauses before the important word. Once in a while an abrupt change of intonation on a single "
        "word, then straight back to flat."
    ),
    fx=FxParams(pitch_semitones=-3.0, formant_semitones=-0.5, rate=0.97, highpass_hz=90, low_mid_cut_db=-1.5,
                nasal_hz=1500, nasal_db=4.0, nasal_q=1.4, presence_db=1.5, air_db=-2.0,
                vocoder_mix=0.18, vocoder_hz=100.0, ring_mix=0.04, ring_hz=100.0, ring_shape="saw",
                resonance_mix=0.14, resonance_hz=190.0, resonance_taps=4, resonance_decay=0.45),
    cues=CueSettings(enabled=True, volume=0.45, pitch=0.55, tempo=0.9, buzz=0.5),
)

HERTZ_BUZZY = VoicePreset(
    name="hertz_buzzy",
    base_voice="echo",
    description="more electronic: vocoder buzz on a fixed low carrier, monotone-ish",
    character=(
        "Your voice is a small robot dog with a low, electronic, almost monotone machine voice: low-mid pitch, "
        "very little pitch movement, even and regular rhythm, mechanical and deadpan. Every word clear and "
        "separate. Small awkward pauses, as if processing. Completely serious."
    ),
    fx=FxParams(pitch_semitones=-2.0, formant_semitones=0.0, rate=0.98, highpass_hz=80,
                nasal_hz=1300, nasal_db=3.0, nasal_q=1.2, presence_db=2.0, air_db=-1.0,
                vocoder_mix=0.55, vocoder_hz=98.0, vocoder_bands=28, vocoder_noise=0.3,
                resonance_mix=0.08, resonance_hz=250.0),
    cues=CueSettings(enabled=True, volume=0.45, pitch=0.5, tempo=0.85, buzz=0.9),
)

HERTZ_TIN_CAN = VoicePreset(
    name="hertz_tin_can",
    base_voice="verse",
    description="hollow, boxy small-speaker robot: band-limited, resonant tin-can comb, slight pitch down",
    character=(
        "Your voice is a small robot dog speaking through its own little built-in speaker: low-mid, dry, a bit "
        "nasal, deadpan and matter-of-fact, compact and precise. Short awkward pauses; an occasional abrupt "
        "change of intonation, then flat again. Completely serious."
    ),
    fx=FxParams(pitch_semitones=-1.5, formant_semitones=0.0, rate=1.0, highpass_hz=250, lowpass_hz=4000,
                nasal_hz=1700, nasal_db=5.0, nasal_q=2.0, presence_db=1.0,
                vocoder_mix=0.08, vocoder_hz=105.0,
                resonance_mix=0.30, resonance_hz=380.0, resonance_taps=6, resonance_decay=0.55),
    cues=CueSettings(enabled=True, volume=0.45, pitch=0.65, tempo=1.0, buzz=0.3),
)

OFF = VoicePreset(name=PRESET_OFF, base_voice="", character="", fx=FxParams(), cues=CueSettings(enabled=False),
                  description="current voice: OPENAI_VOICE, original instructions, no processing (default)")

PRESETS: dict[str, VoicePreset] = {p.name: p for p in (HERTZ_DEADPAN, HERTZ_BUZZY, HERTZ_TIN_CAN, OFF)}
CHARACTER_PRESETS = tuple(n for n in PRESETS if n != PRESET_OFF)

# ---------------------------------------------------------------------- scene delivery
# Emotion per chapter; identity comes from the preset. A script may write `@voice: scene:<id>`
# (expanded here) or keep its own free-text direction — both are combined with the character.
SCENES: dict[str, str] = {
    "last_dog": "booting up: careful, a little hesitant, very literal",
    "prehistory": "fascinated, curious and slightly confused — an analytical machine discovering something old",
    "middle_ages": "very serious, trying hard to sound intelligent",
    "paintings": "gentle curiosity, quietly impressed by how many there are",
    "portraits": "gentle curiosity",
    "photography": "thoughtful and slightly melancholic; slower, longer pauses",
    "internet": "suddenly very excited: quicker, abrupt jumps in intonation, comic timing — still the same low "
                "machine voice",
    "invitation": "warm, encouraging and awkwardly shy, turning to a real person",
    "human_interaction": "warm, encouraging and awkwardly shy",
    "present": "quiet, gentle and intimate — still exactly the same low little robot, just softer",
    "ending": "quiet, gentle and intimate — still exactly the same low little robot, just softer",
}


def scene_direction(direction: str) -> str:
    """Expand `scene:<id>` to the scene's emotion; anything else is returned unchanged."""
    d = (direction or "").strip()
    if d.lower().startswith("scene:"):
        sid = d.split(":", 1)[1].strip().lower()
        return SCENES.get(sid, sid.replace("_", " "))
    return d


def get_preset(name: str | None) -> VoicePreset:
    key = (name or DEFAULT_PRESET).strip().lower().replace("-", "_")
    if key in ("", "default"):
        key = DEFAULT_PRESET
    if key in ("none", "false", "0", "disabled"):
        key = PRESET_OFF
    if key not in PRESETS:
        raise ValueError(f"unknown VOICE_PRESET {name!r}; choose one of: {', '.join(PRESETS)}")
    return PRESETS[key]


def resolve_preset(settings=None, name: str | None = None) -> VoicePreset:
    """The active preset with all VOICE_* overrides from settings applied."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    try:
        preset = get_preset(name or getattr(settings, "voice_preset", DEFAULT_PRESET))
    except ValueError as exc:
        log.warning("%s — using %s", exc, DEFAULT_PRESET)
        preset = PRESETS[DEFAULT_PRESET]
    if preset.is_off:
        return preset
    base = (getattr(settings, "voice_base_voice", "") or "").strip()
    if base:
        preset = replace(preset, base_voice=base)
    fx = preset.fx
    overrides: dict = {}
    for field_name, attr in (("pitch_semitones", "voice_pitch_semitones"),
                             ("formant_semitones", "voice_formant_semitones"),
                             ("rate", "voice_rate")):
        v = getattr(settings, attr, None)
        if v is not None:
            overrides[field_name] = float(v)
    color = getattr(settings, "voice_coloration", None)
    if color is not None:
        c = max(0.0, float(color))
        overrides.update(chorus_mix=fx.chorus_mix * c, ring_mix=fx.ring_mix * c,
                         resonance_mix=fx.resonance_mix * c, vocoder_mix=min(1.0, fx.vocoder_mix * c))
    if not getattr(settings, "voice_chirps", True):
        overrides["chirp_probability"] = 0.0
    if not getattr(settings, "voice_fx_enabled", True):
        fx, overrides = FxParams(), {}  # identity → raw audio
    preset = replace(preset, fx=replace(fx, **overrides))
    vol = getattr(settings, "voice_cue_volume", None)
    cues = replace(preset.cues, enabled=preset.cues.enabled and bool(getattr(settings, "voice_cues", True)),
                   volume=float(vol) if vol is not None else preset.cues.volume)
    return replace(preset, cues=cues)


def voice_for(settings, preset: VoicePreset | None = None) -> str:
    """The voice the whole character uses (narration, lines, conversation, talk)."""
    preset = preset if preset is not None else resolve_preset(settings)
    if preset.is_off or not preset.base_voice:
        return settings.primary_voice
    return preset.base_voice


def live_character_note(preset: VoicePreset | None) -> str:
    """Session-level voice direction for live conversation (GPT-Live / Realtime)."""
    if preset is None or preset.is_off or not preset.character:
        return ""
    return ("\nYOUR VOICE (how you sound — the same voice as in the rest of the performance):\n"
            f"{preset.character}\n{CHARACTER_RULES}\n"
            "Keep answers short and very clear. Personality is in HOW you say things, not in silly words.\n")
