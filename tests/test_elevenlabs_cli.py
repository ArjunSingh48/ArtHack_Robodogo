"""Provider-aware operator tools; no network or real credentials."""

from types import SimpleNamespace

import numpy as np
import pytest

from is_this_us.config import Settings
from is_this_us.performance import cli
from is_this_us.performance.voice import CueAudio, ShowVoice, Speaker
from is_this_us.speech import demo, synthesizer
from is_this_us.speech.synthesizer import NullSynthesizer, SpeechAudio


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, voice_provider="elevenlabs", elevenlabs_api_key="test-key",
                    elevenlabs_voice_id="test-voice", audio_cache_dir=tmp_path, **kwargs)


def test_parts_selects_english_sources(tmp_path, monkeypatch):
    from is_this_us.performance import script as script_module

    original = script_module.load_script
    languages = []

    def load(directory, lang, programme):
        languages.append(lang)
        return original(directory, lang, programme)

    monkeypatch.setattr(script_module, "load_script", load)
    script, _, lines = cli._parts(settings(tmp_path), "de", "presentation")
    assert languages == ["en"]
    assert script.lang == lines.lang == "en"


def test_status_reports_provider_and_character_budget_without_generation(tmp_path, monkeypatch, capsys):
    s = settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    monkeypatch.setattr(cli, "_parts", lambda *_: (SimpleNamespace(lang="en"), None, None))
    from is_this_us.performance import voice as voice_module

    monkeypatch.setattr(voice_module, "all_utterances", lambda *_: [CueAudio("intro.1", "narration", "Hello.", "", None)])
    monkeypatch.setattr(cli, "_show_voice", lambda *_: ShowVoice(NullSynthesizer(), NullSynthesizer(), Speaker(False)))
    assert cli.cmd_voice(SimpleNamespace(action="status", lang="de", limit=10)) == 0
    output = capsys.readouterr().out
    assert "provider elevenlabs" in output
    assert "missing 1 clip(s), 6 text characters" in output
    assert "test-key" not in output


def test_audition_uses_elevenlabs_ids_and_english(tmp_path, monkeypatch, capsys):
    s = settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    captures = []

    class FakeSynth:
        available = True

        def synthesize(self, text, lang, direction):
            captures.append((text, lang, direction))
            return SpeechAudio(tmp_path / "sample.wav", None, False, "elevenlabs")

    def factory(selected, *, purpose):
        assert selected.elevenlabs_voice_id == "chosen-voice"
        assert selected.elevenlabs_model_id == "eleven_multilingual_v2"
        assert selected.voice_preset == "off"
        assert purpose == "narration"
        return FakeSynth()

    monkeypatch.setattr(synthesizer, "make_synthesizer", factory)
    args = SimpleNamespace(voices="chosen-voice", model=None, text="Hello. I am a dog.", lang="ru", no_play=True)
    assert cli.cmd_audition(args) == 0
    assert captures == [(args.text, "en", "")]
    assert "ELEVENLABS_VOICE_ID" in capsys.readouterr().out


def test_failed_audition_returns_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: settings(tmp_path))
    monkeypatch.setattr(synthesizer, "make_synthesizer", lambda *_args, **_kwargs: NullSynthesizer())
    args = SimpleNamespace(voices=None, model=None, text="Hello.", lang="en", no_play=True)
    assert cli.cmd_audition(args) == 1


def test_elevenlabs_demo_keeps_selected_identity_and_raw_audio(tmp_path, monkeypatch):
    s = settings(tmp_path, voice_preset="hertz_buzzy")
    captures = []
    raw = tmp_path / "raw.wav"
    demo.write_wav(raw, np.ones(2400, dtype=np.float32) * 0.1)

    class FakeSynth:
        def synthesize(self, text, lang, direction):
            captures.append((text, lang, direction))
            return SpeechAudio(raw, None, False, "elevenlabs")

    def factory(selected, *, purpose):
        assert selected.voice_provider == "elevenlabs"
        assert selected.voice_preset == "off"
        assert selected.elevenlabs_voice_id == s.elevenlabs_voice_id
        assert selected.audio_cache_dir == tmp_path / "voice_demo"
        return FakeSynth()

    monkeypatch.setattr(synthesizer, "make_synthesizer", factory)
    assert demo.elevenlabs_engine(s)("Hello.").shape == (2400,)
    assert captures == [("Hello.", "en", "")]


def test_demo_selects_configured_provider_without_azure_fallback(tmp_path, monkeypatch):
    s = settings(tmp_path, azure_openai_api_key="legacy-key", azure_openai_endpoint="https://example.invalid")
    assert demo._engine_names(s, "auto", None) == ["elevenlabs"]
    monkeypatch.setattr(demo.shutil, "which", lambda _: None)
    missing = s.model_copy(update={"elevenlabs_api_key": ""})
    assert isinstance(demo._engine_names(missing, "auto", None), str)
    assert "ELEVENLABS_API_KEY" in demo._engine_names(missing, "elevenlabs", None)


def test_disabled_provider_cli_never_generates(tmp_path, monkeypatch, capsys):
    s = settings(tmp_path).model_copy(update={"voice_provider": "none"})
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    args = SimpleNamespace(voices=None, model=None, text="Hello.", lang="en", no_play=True)
    assert cli.cmd_audition(args) == 2
    assert "VOICE_PROVIDER=none" in capsys.readouterr().out
    assert "VOICE_PROVIDER=none" in demo._engine_names(s, "auto", None)


@pytest.mark.parametrize("action", ["pregenerate", "regenerate"])
def test_pregeneration_requires_persistent_cache(tmp_path, monkeypatch, capsys, action):
    s = settings(tmp_path, voice_cache_enabled=False)
    monkeypatch.setattr(cli, "get_settings", lambda: s)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Disabled persistent cache must reject generation before preparing audio")

    monkeypatch.setattr(cli, "_parts", unexpected)
    monkeypatch.setattr(cli, "_show_voice", unexpected)
    assert cli.cmd_voice(SimpleNamespace(action=action)) == 2
    assert "Set VOICE_CACHE_ENABLED=true" in capsys.readouterr().out
