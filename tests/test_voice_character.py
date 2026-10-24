"""Voice character: presets, settings, audio processing, caching, cues, consistency.

No network and no real TTS: the Realtime connection is a fake that returns a synthetic
voiced signal. Processing tests need FFmpeg (skipped without it, except the fallback tests,
which simulate a missing FFmpeg / rubberband).
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from is_this_us.config import Settings
from is_this_us.speech import cues as cues_mod
from is_this_us.speech import fx as fx_mod
from is_this_us.speech.character import (
    CHARACTER_PRESETS,
    PRESETS,
    FxParams,
    get_preset,
    live_character_note,
    resolve_preset,
    scene_direction,
    voice_for,
)
from is_this_us.speech.synthesizer import (
    AudioCache,
    OpenAIRealtimeSynthesizer,
    make_synthesizer,
)

RATE = 24_000
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def settings(**kw) -> Settings:
    base = {"voice_provider": "openai", "azure_openai_api_key": "", "openai_voice": "marin"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def voiced(seconds: float = 2.0, f0: float = 150.0, amp: float = 0.25) -> np.ndarray:
    """Speech-like test signal: harmonic tone with syllable-rate amplitude envelope and pauses."""
    t = np.arange(int(seconds * RATE)) / RATE
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 14))
    env = np.clip(np.sin(2 * np.pi * 2.5 * t), 0, None) ** 0.5  # 2.5 "syllables" / s with gaps
    return (amp * x * env / np.max(np.abs(x))).astype(np.float32)


def f0_of(y: np.ndarray) -> float:
    seg = y[RATE // 10: RATE // 10 + 4096]
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size), 1 << 18))
    f = np.fft.rfftfreq(1 << 18, 1 / RATE)
    m = (f > 80) & (f < 600)
    return float(f[m][np.argmax(spec[m])])


def write(path: Path, x: np.ndarray) -> Path:
    fx_mod.write_wav(path, x)
    return path


def plain(fx: FxParams) -> FxParams:
    """Only pitch/formant/rate/EQ/normalisation (makes pitch measurement exact)."""
    return replace(fx, chorus_mix=0, ring_mix=0, resonance_mix=0, wobble_semitones=0, chirp_probability=0)


# ---------------------------------------------------------------- presets + settings
def test_three_presets_plus_off():
    assert set(CHARACTER_PRESETS) == {"hertz_deadpan", "hertz_buzzy", "hertz_tin_can"}
    assert get_preset(None).is_off  # default: the current voice until the team picks a preset
    assert Settings(_env_file=None).voice_preset == "off"
    assert get_preset("Hertz-Buzzy").name == "hertz_buzzy"
    assert get_preset("off").is_off and get_preset("none").is_off
    with pytest.raises(ValueError):
        get_preset("helium")
    for name in CHARACTER_PRESETS:
        p = PRESETS[name]
        assert p.base_voice in ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")
        # low-mid, not booming: a few semitones down at most, formants (body size) nearly untouched
        assert p.character and -4.5 <= p.fx.pitch_semitones < 0 and 0.95 <= p.fx.rate <= 1.05
        assert abs(p.fx.formant_semitones) <= 1.0
        assert p.fx.ring_mix <= 0.1 and p.fx.vocoder_mix <= 0.7 and p.fx.resonance_mix <= 0.35  # no sludge
        assert p.fx.chirp_probability == 0  # no cute chirps for a deadpan robot
    d, b, t = (PRESETS[n].fx for n in ("hertz_deadpan", "hertz_buzzy", "hertz_tin_can"))
    assert b.vocoder_mix > d.vocoder_mix > t.vocoder_mix  # buzzy is the most electronic
    assert t.highpass_hz >= 200 and 3000 <= t.lowpass_hz <= 5000  # tin can is band-limited
    assert 1000 <= d.nasal_hz <= 2000 and d.nasal_db > 0


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("VOICE_PRESET", "hertz_tin_can")
    monkeypatch.setenv("VOICE_PITCH_SEMITONES", "")  # empty = preset value
    monkeypatch.setenv("VOICE_RATE", "1.08")
    monkeypatch.setenv("VOICE_CUES", "false")
    s = Settings(_env_file=None)
    assert s.voice_preset == "hertz_tin_can" and s.voice_pitch_semitones is None and s.voice_rate == 1.08
    p = resolve_preset(s)
    assert p.name == "hertz_tin_can" and p.fx.pitch_semitones == -1.5 and p.fx.rate == 1.08
    assert p.cues.enabled is False
    assert s.character_voice == "verse"


def test_overrides_and_off():
    s = settings(voice_preset="hertz_deadpan", voice_base_voice="cedar", voice_pitch_semitones=-2,
                 voice_coloration=0, voice_chirps=False)
    p = resolve_preset(s)
    assert p.base_voice == "cedar" and voice_for(s, p) == "cedar"
    assert p.fx.pitch_semitones == -2
    assert p.fx.chorus_mix == p.fx.ring_mix == p.fx.resonance_mix == p.fx.vocoder_mix == 0
    assert p.fx.chirp_probability == 0
    assert resolve_preset(settings(voice_preset="hertz_buzzy", voice_fx_enabled=False)).fx.is_identity()
    off = settings(voice_preset="off")
    assert resolve_preset(off).is_off and off.character_voice == "marin"  # previous behaviour
    assert resolve_preset(settings(voice_preset="nonsense")).is_off  # warns, no crash, current voice


def test_make_synthesizer_uses_preset_voice_and_character(tmp_path):
    s = settings(audio_cache_dir=tmp_path, voice_preset="hertz_buzzy")
    synth = make_synthesizer(s, purpose="narration")
    assert synth.voice == "echo" and synth.character.name == "hertz_buzzy"
    off = make_synthesizer(settings(audio_cache_dir=tmp_path, voice_preset="off"), purpose="narration")
    assert off.voice == "marin" and off.character is None and off.fx is None


# ---------------------------------------------------------------- instructions (no SSML, scene + identity)
def test_instructions_combine_character_and_scene():
    p = PRESETS["hertz_deadpan"]
    synth = OpenAIRealtimeSynthesizer("k", "gpt-realtime-2.1-mini", p.base_voice, AudioCache(Path("/tmp/x")),
                                      character=p)
    ins = synth.instructions_for("en", "scene:internet")
    assert p.character in ins and "EXACTLY as written" in ins and "NOT a high cartoon voice" in ins
    assert "suddenly very excited" in ins and "Delivery for this passage" in ins
    assert "stay exactly the same" in ins  # emotion varies, identity does not
    assert "<speak" not in ins and "<prosody" not in ins  # never SSML to an OpenAI endpoint
    # a free-text @voice direction still works
    assert "observational" in synth.instructions_for("en", "observational, quietly amazed")
    assert scene_direction("scene:ending").startswith("quiet, gentle")
    assert scene_direction("shy") == "shy"


def test_preset_off_is_exactly_the_old_voice(tmp_path):
    legacy = OpenAIRealtimeSynthesizer("k", "m", "marin", AudioCache(tmp_path))
    off = OpenAIRealtimeSynthesizer("k", "m", "marin", AudioCache(tmp_path), character=PRESETS["off"])
    assert off.key_for("Hello.", "en", "shy") == legacy.key_for("Hello.", "en", "shy")
    assert off.instructions_for("en", "shy") == legacy.instructions_for("en", "shy")


# ---------------------------------------------------------------- cache keys
def test_cache_key_covers_preset_and_instructions_but_not_fx(tmp_path):
    a = PRESETS["hertz_deadpan"]
    s1 = OpenAIRealtimeSynthesizer("k", "m", "ash", AudioCache(tmp_path), character=a)
    s2 = OpenAIRealtimeSynthesizer("k", "m", "ash", AudioCache(tmp_path), character=replace(a, name="other"))
    s3 = OpenAIRealtimeSynthesizer("k", "m", "ash", AudioCache(tmp_path), character=replace(a, character="x"))
    s4 = OpenAIRealtimeSynthesizer("k", "m", "ash", AudioCache(tmp_path),
                                   character=replace(a, fx=replace(a.fx, pitch_semitones=7)))
    k1 = s1.key_for("Hello.")
    assert k1 != s2.key_for("Hello.") and k1 != s3.key_for("Hello.")
    assert k1 == s4.key_for("Hello.")  # FX only → same raw audio (no new API call)...
    assert s1.processed_paths(k1)[0] != s4.processed_paths(k1)[0]  # ...but a new processed clip
    assert s1.key_for("Hello.", "en", "scene:internet") != s1.key_for("Hello.", "en", "scene:ending")


# ---------------------------------------------------------------- audio processing
@needs_ffmpeg
@pytest.mark.parametrize("name", CHARACTER_PRESETS)
def test_processing_preserves_timing_and_normalises(tmp_path, name):
    p = PRESETS[name]
    src = write(tmp_path / "in.wav", voiced(2.0))
    res = fx_mod.process_file(src, tmp_path / "out.wav", replace(p.fx, chirp_probability=0))
    assert res is not None and res.path.exists()
    y, rate = fx_mod.read_wav(res.path)
    m = fx_mod.measure(y, rate)
    assert rate == RATE
    assert m["seconds"] == pytest.approx(2.0 / p.fx.rate, rel=0.03)  # pitch shift keeps timing
    assert m["rms_dbfs"] == pytest.approx(p.fx.target_rms_dbfs, abs=1.0)
    assert m["peak_dbfs"] <= p.fx.peak_dbfs + 0.1 and m["clipped"] == 0.0


@needs_ffmpeg
def test_pitch_shift_is_accurate_and_formant_independent(tmp_path):
    x = voiced(1.5)
    for st, fm in ((-4, -0.5), (-2, 0.0), (3, 1.5), (5, 2.0)):
        y, method = fx_mod.process_array(x, RATE, plain(FxParams(pitch_semitones=st, formant_semitones=fm)))
        assert 12 * np.log2(f0_of(y) / 150.0) == pytest.approx(st, abs=0.25)
        if fx_mod.has_filter(shutil.which("ffmpeg"), "rubberband"):
            assert method == "rubberband"


@needs_ffmpeg
def test_fallback_without_rubberband(monkeypatch, tmp_path):
    monkeypatch.setattr(fx_mod, "has_filter", lambda ffmpeg, name: False)
    y, method = fx_mod.process_array(voiced(1.0), RATE, plain(FxParams(pitch_semitones=5, rate=1.05)))
    assert method == "asetrate"
    assert y.size / RATE == pytest.approx(1.0 / 1.05, rel=0.03)
    assert 12 * np.log2(f0_of(y) / 150.0) == pytest.approx(5, abs=0.3)


def test_graceful_fallback_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(fx_mod, "find_ffmpeg", lambda: None)
    src = write(tmp_path / "in.wav", voiced(0.5))
    assert fx_mod.process_file(src, tmp_path / "out.wav", PRESETS["hertz_deadpan"].fx) is None
    assert not (tmp_path / "out.wav").exists()


def test_numpy_coloration_is_subtle_and_finite():
    x = voiced(1.0)
    for f in (fx_mod.chorus(x, 0.1), fx_mod.ring_mod(x, 0.05, 70), fx_mod.resonance(x, 0.08, 420)):
        assert f.shape == x.shape and np.all(np.isfinite(f))
        assert np.corrcoef(x[2000:], f[2000:])[0, 1] > 0.9  # still essentially the original voice
    w = fx_mod.wobble(x, 0.25, 3.5)  # a modulated delay: pitch moves a little, never far
    assert w.shape == x.shape and np.all(np.isfinite(w))
    assert abs(12 * np.log2(f0_of(w) / 150.0)) < 0.5
    n = fx_mod.normalize(x * 3.0)
    assert np.max(np.abs(n)) < 10 ** (-1.5 / 20) + 1e-3


def test_chirps_are_occasional_and_deterministic():
    fx = FxParams(chirp_probability=0.15)
    x = voiced(0.5)
    hits = [fx_mod.maybe_chirp(x, fx, f"clip{i}").size > x.size for i in range(400)]
    assert 0.07 < sum(hits) / 400 < 0.25
    assert fx_mod.maybe_chirp(x, fx, "clip3").size == fx_mod.maybe_chirp(x, fx, "clip3").size
    assert fx_mod.maybe_chirp(x, replace(fx, chirp_probability=0), "clip3").size == x.size


# ---------------------------------------------------------------- synthesizer integration (fake Realtime)
class FakeRealtime:
    """Minimal fake of `client.realtime.connect`: returns a voiced signal + a faithful transcript."""

    def __init__(self, fail: bool = False):
        self.fail, self.connects, self.sessions = fail, 0, []
        self.realtime = NS(connect=self._connect)

    def _connect(self, model, **kw):
        outer = self

        class Conn:
            def __init__(self):
                self.text = ""
                self.events = []
                self.session = NS(update=self._session)
                self.conversation = NS(item=NS(create=self._item))
                self.response = NS(create=self._create)

            async def _session(self, session):
                outer.sessions.append(session)

            async def _item(self, item):
                self.text = item["content"][0]["text"]

            async def _create(self, **kw):
                pcm = (voiced(0.6) * 32767).astype("<i2").tobytes()
                self.events = [NS(type="response.output_audio.delta", delta=base64.b64encode(pcm).decode()),
                               NS(type="response.output_audio_transcript.done", transcript=self.text),
                               NS(type="response.done")]

            async def recv(self):
                return self.events.pop(0)

        class Manager:
            async def __aenter__(self):
                outer.connects += 1
                if outer.fail:
                    raise ConnectionError("network down")
                return Conn()

            async def __aexit__(self, *a):
                return False
        return Manager()


def _synth(tmp_path, preset, client=None, key="k"):
    return OpenAIRealtimeSynthesizer(key, "gpt-realtime-2.1-mini", preset.base_voice, AudioCache(tmp_path),
                                     client=client, voice_notes=False, character=preset)


@needs_ffmpeg
def test_synthesize_processes_and_reuses_raw_when_only_fx_change(tmp_path):
    a = PRESETS["hertz_deadpan"]
    client = FakeRealtime()
    res = _synth(tmp_path, a, client).synthesize("Where is my tail?", "en", "scene:internet")
    assert res is not None and res.audio_path.parent.name == "fx" and client.connects == 1
    sess = client.sessions[0]
    assert sess["audio"]["output"]["voice"] == "ash" and a.character in sess["instructions"]
    assert set(sess) == {"type", "instructions", "output_modalities", "audio"}  # no unsupported params
    # change only the FX, even without credentials: raw is reused, processed anew, no API call
    b = replace(a, fx=replace(a.fx, pitch_semitones=3))
    offline = _synth(tmp_path, b, FakeRealtime(), key="")
    assert offline.cached("Where is my tail?", "en", "scene:internet") is None  # pure lookup
    again = offline.synthesize("Where is my tail?", "en", "scene:internet")
    assert again is not None and again.audio_path != res.audio_path and again.audio_path.exists()
    assert offline.cached("Where is my tail?", "en", "scene:internet") == again.audio_path


def test_processing_failure_plays_raw_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(fx_mod, "find_ffmpeg", lambda: None)
    res = _synth(tmp_path, PRESETS["hertz_buzzy"], FakeRealtime()).synthesize("Hello! I am a dog.")
    assert res is not None and res.audio_path.exists() and res.audio_path.parent == tmp_path  # raw clip


def test_synthesis_failure_degrades_to_text(tmp_path):
    from is_this_us.performance.voice import ShowVoice, Speaker, reading_seconds

    synth = _synth(tmp_path, PRESETS["hertz_deadpan"], FakeRealtime(fail=True))
    assert synth.synthesize("Hello! I am a dog.") is None
    voice = ShowVoice(synth, synth, Speaker(enabled=False), time_scale=0)
    spoken = asyncio.run(voice.speak("Hello! I am a dog."))
    assert spoken.audio is None and spoken.seconds == reading_seconds("Hello! I am a dog.")


@needs_ffmpeg
def test_show_voice_processes_cached_raw_without_generation(tmp_path):
    from is_this_us.performance.voice import ShowVoice, Speaker

    a = PRESETS["hertz_deadpan"]
    _synth(tmp_path, a, FakeRealtime()).synthesize("Oh no.")
    b = replace(a, fx=replace(a.fx, rate=1.1))
    synth = _synth(tmp_path, b, None, key="")  # no credentials at all
    voice = ShowVoice(synth, synth, Speaker(enabled=False), allow_generation=False, time_scale=0)
    path = asyncio.run(voice.audio_for("Oh no."))
    assert path is not None and path.parent.name == "fx" and path == synth.cached("Oh no.")


@needs_ffmpeg
def test_prune_removes_stale_processed_clips(tmp_path):
    from is_this_us.performance.voice import CueAudio, ShowVoice, Speaker, prune

    a = PRESETS["hertz_deadpan"]
    s_old = _synth(tmp_path, replace(a, fx=replace(a.fx, pitch_semitones=4)), FakeRealtime())
    s_old.synthesize("Hello.")
    s_new = _synth(tmp_path, a, FakeRealtime())
    s_new.synthesize("Hello.")
    voice = ShowVoice(s_new, s_new, Speaker(enabled=False))
    removed = prune([CueAudio("x", "narration", "Hello.", "", None)], voice)
    assert removed == 1  # the old-FX clip; raw + current processed stay
    assert s_new.cached("Hello.") is not None and s_new.cache.paths(s_new.key_for("Hello."))[0].exists()


# ---------------------------------------------------------------- sound cues
def test_cues_are_short_soft_and_cached(tmp_path):
    for name in cues_mod.NAMES + ("tiny_chirp",):
        y = cues_mod.synth_cue(name)
        assert 0.05 < y.size / RATE < 0.8 and np.max(np.abs(y)) == pytest.approx(1.0, abs=1e-4)
        assert abs(float(y[0])) < 0.05 and abs(float(y[-1])) < 0.05  # no clicks
    bank = cues_mod.CueBank(tmp_path, PRESETS["hertz_deadpan"].cues)
    paths = bank.generate_all()
    assert all(p and p.exists() for p in paths.values())
    x, _ = fx_mod.read_wav(paths["chirp"])
    assert np.max(np.abs(x)) <= 0.6 * PRESETS["hertz_deadpan"].cues.volume + 1e-3
    assert bank.path("delight") == paths["chirp"]  # alias
    assert bank.path("unknown") is None
    assert cues_mod.CueBank(tmp_path, PRESETS["off"].cues).path("chirp") is None


def test_show_voice_cue_api(tmp_path):
    from is_this_us.performance.voice import ShowVoice

    class FakeSpeaker:
        available = True

        def __init__(self):
            self.played = []

        async def play(self, path, time_scale=1.0):
            self.played.append(path)

    synth = _synth(tmp_path / "audio" / "narration", PRESETS["hertz_deadpan"])
    spk = FakeSpeaker()
    voice = ShowVoice(synth, synth, spk)
    assert voice.cues is not None and voice.cues.enabled
    p = asyncio.run(voice.cue("chirp"))
    assert p is not None and spk.played == [p] and p.parent == tmp_path / "audio" / "cues"
    # disabled with VOICE_CUES=false / preset off → silent no-op
    quiet = _synth(tmp_path / "q" / "narration", resolve_preset(settings(voice_cues=False)))
    v2 = ShowVoice(quiet, quiet, spk)
    assert asyncio.run(v2.cue("chirp")) is None and len(spk.played) == 1


# ---------------------------------------------------------------- live conversation consistency
def test_live_conversation_gets_the_same_character(tmp_path):
    from is_this_us.performance.conversation import (
        ConversationService,
        build_show_instructions,
    )
    from is_this_us.talk.persona import build_instructions

    p = PRESETS["hertz_tin_can"]
    note = live_character_note(p)
    assert p.character in note and live_character_note(PRESETS["off"]) == ""
    assert p.character in build_show_instructions({"chapter": "x"}, voice_note=note)
    assert p.character in build_instructions("story", "English", voice_note=note)
    assert build_instructions("story") == build_instructions("story", voice_note=None)

    captured = {}

    class FakeWindow:
        def __init__(self, *a, **kw):
            captured.update(kw)

        async def start(self):
            return None

    import is_this_us.performance.conversation as conv

    svc = ConversationService(enabled=True, client_factory=lambda: object(), audio_factory=lambda: object(),
                              voice=voice_for(settings(voice_preset="hertz_tin_can")), character=p)
    orig = conv.GptLiveWindow
    conv.GptLiveWindow = FakeWindow
    try:
        asyncio.run(svc.open({"chapter": "internet"}))
    finally:
        conv.GptLiveWindow = orig
    assert captured["voice"] == "verse" and p.character in captured["instructions"]


# ---------------------------------------------------------------- CLI
@needs_ffmpeg
def test_voice_demo_offline(tmp_path, monkeypatch):
    from is_this_us import cli
    from is_this_us.config import get_settings

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()
    src = write(tmp_path / "rec.wav", voiced(1.2))
    out = tmp_path / "previews"
    try:
        cli.main(["voice-demo", "--offline-input", str(src), "--out", str(out), "--formats", ""])
        for name in CHARACTER_PRESETS:
            y, _ = fx_mod.read_wav(out / f"{name}__input.wav")
            assert y.size > 0.8 * RATE and np.max(np.abs(y)) < 1.0
        assert (out / "base__input.wav").exists()
        assert all((out / f"hertz_deadpan__pitch{s}__input.wav").exists() for s in ("-2", "-3", "-4"))
        assert (out / "cues" / "hertz_deadpan" / "chime.wav").exists()
        with pytest.raises(SystemExit) as exc:  # Azure without a key: honest exit, nothing faked
            cli.main(["voice-demo", "--engine", "azure", "--out", str(out)])
        assert exc.value.code == 2
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------- Hertz FX + demo helpers
def test_vocoder_imposes_a_fixed_low_carrier():
    x = voiced(1.0, f0=170.0)
    y = fx_mod.vocoder(x, 1.0, carrier_hz=100.0)
    assert y.shape == x.shape and np.all(np.isfinite(y))
    # monotone robot pitch, whatever the voice did: the energy sits on multiples of 100 Hz, not 170 Hz
    seg = y[RATE // 4: RATE // 4 + 8192]
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2
    f = np.fft.rfftfreq(seg.size, 1 / RATE)
    band = (f > 80) & (f < 2000)
    on_100 = band & (np.abs(f - 100 * np.round(f / 100)) < 8)
    on_170 = band & (np.abs(f - 170 * np.round(f / 170)) < 8) & ~on_100
    assert spec[on_100].sum() > 5 * spec[on_170].sum()
    blend = fx_mod.vocoder(x, 0.2, carrier_hz=100.0)
    assert np.corrcoef(x, blend)[0, 1] > 0.75  # light blend keeps the voice
    assert np.array_equal(fx_mod.vocoder(x, 0.0), x)


def test_saw_ring_mod_is_finite_and_blended():
    x = voiced(0.5)
    y = fx_mod.ring_mod(x, 0.05, 100.0, shape="saw")
    assert np.all(np.isfinite(y)) and np.corrcoef(x, y)[0, 1] > 0.9


def test_pause_markers_give_fair_timing():
    from is_this_us.speech.demo import SAMPLE_TEXT, assemble, parse_script

    seg = parse_script(SAMPLE_TEXT)
    assert seg == ["Hello. I am a dog.", 0.7, "I have four legs.", 0.7, "I do not have a tail.", 1.8,
                   "This is concerning."]
    assert parse_script("a\nb\n[pause 1.2]\nc") == ["a", 0.35, "b", 1.2, "c"]
    # each line's own leading/trailing silence is trimmed: only the markers set the pauses
    line = np.concatenate([np.zeros(RATE), voiced(0.5), np.zeros(RATE)]).astype(np.float32)
    out = assemble(["x", 1.0, "y"], lambda text: line)
    assert out.size / RATE == pytest.approx(0.25 + 2 * 0.5 + 1.0 + 0.5, abs=0.15)


@pytest.mark.skipif(shutil.which("espeak-ng") is None or shutil.which("ffmpeg") is None,
                    reason="espeak-ng / ffmpeg not installed")
def test_espeak_engine_makes_real_previews(tmp_path, monkeypatch):
    from is_this_us.speech.demo import run_demo

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "")
    s = settings(audio_cache_dir=tmp_path / "cache")
    lines: list[str] = []
    code = run_demo(s, out=tmp_path / "p", engines="espeak", formats="", pitch_compare=False, cues=False,
                    printer=lines.append)
    assert code == 0, lines
    base, _ = fx_mod.read_wav(tmp_path / "p" / "base__espeak.wav")
    for name in CHARACTER_PRESETS:
        y, _ = fx_mod.read_wav(tmp_path / "p" / f"{name}__espeak.wav")
        m = fx_mod.measure(y)
        assert m["clipped"] == 0 and m["rms_dbfs"] == pytest.approx(-19, abs=1.0)
        assert m["seconds"] == pytest.approx(base.size / RATE / PRESETS[name].fx.rate, rel=0.05)
