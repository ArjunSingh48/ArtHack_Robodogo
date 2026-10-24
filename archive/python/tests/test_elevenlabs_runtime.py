"""English presentation wiring and cancellable speech without network or audio devices."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from is_this_us.config import Settings
from is_this_us.performance import runtime
from is_this_us.performance.stage import RecordingStage
from is_this_us.performance.voice import ShowVoice, Speaker
from is_this_us.speech.synthesizer import NullSynthesizer

ROOT = Path(__file__).resolve().parents[1]


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, show_data_dir=ROOT / "data/performance",
                    artwork_data_dir=ROOT / "data/artworks", meme_data_dir=ROOT / "data/memes",
                    show_output_dir=tmp_path / "photos", audio_cache_dir=tmp_path / "audio", **kwargs)


def build(settings, **kwargs):
    return runtime.build_director(settings, runtime.ShowOptions(source="none", **kwargs),
                                  stage=RecordingStage())


def test_elevenlabs_loads_english_sources_even_when_other_language_requested(tmp_path, monkeypatch):
    requested = []
    original = runtime.load_script

    def load(data_dir, lang, programme):
        requested.append(lang)
        return original(data_dir, lang, programme)

    monkeypatch.setattr(runtime, "load_script", load)
    monkeypatch.setattr(runtime, "make_synthesizer", lambda *a, **kw: NullSynthesizer())
    director = build(settings(tmp_path, voice_provider="elevenlabs"), lang="de", dry_run=True)
    assert requested == ["en"]
    assert director.script.lang == director.lines.lang == director.voice.lang == "en"
    assert director.script.source.name == "presentation.en.md"


@pytest.mark.parametrize("provider,enabled,dry_run,voice,expected", [
    ("elevenlabs", True, False, True, False),
    ("none", True, False, True, False),
    ("openai", True, False, True, True),
    ("openai", False, False, True, False),
    ("openai", True, True, True, False),
    ("openai", True, False, False, False),
])
def test_live_conversation_only_for_explicit_azure_voice(tmp_path, monkeypatch, provider, enabled,
                                                       dry_run, voice, expected):
    monkeypatch.setattr(runtime, "make_synthesizer", lambda *a, **kw: NullSynthesizer())
    cfg = settings(tmp_path, voice_provider=provider, azure_openai_api_key="fake",
                   openai_enable_live_conversation=enabled)
    director = build(cfg, dry_run=dry_run, voice=voice)
    assert (director.conversation is not None) is expected


@pytest.mark.parametrize("provider,voice", [("none", True), ("elevenlabs", False)])
def test_disabled_voice_cannot_access_cache_generate_or_create_cues(tmp_path, monkeypatch, provider, voice):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled voice must not create a synthesizer")

    monkeypatch.setattr(runtime, "make_synthesizer", forbidden)
    director = build(settings(tmp_path, voice_provider=provider, voice_preset="hertz_deadpan"), voice=voice)
    assert not director.voice.allow_generation
    assert not director.voice.speaker.available
    assert director.voice.cues is None
    assert director.voice.narration.cached("Hello.") is None


class RecordingSpeaker:
    available = True

    def __init__(self):
        self.played = []

    async def play(self, path, time_scale=1):
        self.played.append(path)


class DelayedSynth:
    available = True

    def __init__(self, path):
        self.path = path
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def cached(self, *args):
        return None

    def synthesize(self, *args):
        self.started.set()
        self.release.wait(timeout=2)
        self.finished.set()
        return SimpleNamespace(audio_path=self.path)


async def test_cancelled_generation_never_plays_its_late_result(tmp_path):
    synth, speaker = DelayedSynth(tmp_path / "late.wav"), RecordingSpeaker()
    voice = ShowVoice(synth, synth, speaker, time_scale=0)
    speech = asyncio.create_task(voice.speak("Hello."))
    try:
        assert await asyncio.to_thread(synth.started.wait, 1)
        speech.cancel()
        with pytest.raises(asyncio.CancelledError):
            await speech
    finally:
        synth.release.set()
        assert await asyncio.to_thread(synth.finished.wait, 1)
    await asyncio.sleep(0)
    assert speaker.played == []
    assert voice.log == []


async def test_generation_timeout_never_plays_its_late_result(tmp_path):
    synth, speaker = DelayedSynth(tmp_path / "late.wav"), RecordingSpeaker()
    voice = ShowVoice(synth, synth, speaker, time_scale=0, generation_timeout_s=0.02)
    try:
        spoken = await voice.speak("Hello.")
        assert spoken.audio is None
    finally:
        synth.release.set()
        assert await asyncio.to_thread(synth.finished.wait, 1)
    await asyncio.sleep(0)
    assert speaker.played == []


async def test_generation_exception_continues_silently_without_logging_sensitive_details(caplog):
    class Failing:
        available = True

        def synthesize(self, *args):
            raise RuntimeError("private-request-detail")

    speaker = RecordingSpeaker()
    voice = ShowVoice(Failing(), Failing(), speaker, time_scale=0)
    assert (await voice.speak("Hello.")).audio is None
    assert speaker.played == []
    assert "RuntimeError" in caplog.text
    assert "private-request-detail" not in caplog.text


async def test_cache_only_playback_uses_clip_without_generation(tmp_path):
    path = tmp_path / "cached.wav"

    class Cached:
        available = False

        def cached(self, *args):
            return path

        def synthesize(self, *args):
            pytest.fail("cache-only playback must not synthesize")

    speaker = RecordingSpeaker()
    voice = ShowVoice(Cached(), Cached(), speaker, allow_generation=False)
    assert (await voice.speak("Hello.")).audio == path
    assert speaker.played == [path]


async def test_cancel_stops_active_laptop_player(tmp_path, monkeypatch):
    started, exited = asyncio.Event(), asyncio.Event()

    class Process:
        returncode = None
        terminated = False

        async def wait(self):
            await exited.wait()

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            exited.set()

    proc = Process()

    async def launch(*args, **kwargs):
        started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    playback = asyncio.create_task(Speaker(command=["fake-player"]).play(tmp_path / "clip.wav"))
    await started.wait()
    playback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await playback
    assert proc.terminated
