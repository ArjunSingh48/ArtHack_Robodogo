"""Browser message metadata stays aligned with the curated Python source."""

import importlib.util
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from is_this_us.artworks.repository import YamlArtworkRepository
from is_this_us.memes.repository import MemeRepository
from is_this_us.performance.catalog import PerformanceCatalog
from is_this_us.performance.encounter import attribution
from is_this_us.performance.lines import Lines
from is_this_us.config import Settings
from is_this_us.speech.synthesizer import SpeechAudio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("export_web", ROOT / "scripts" / "export_web.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def test_exported_painting_credits_match_canonical_telegram_metadata():
    show = json.loads((ROOT.parent / "web" / "data" / "show.json").read_text())
    catalog = PerformanceCatalog(ROOT / "data" / "performance",
                                 YamlArtworkRepository(ROOT / "data" / "artworks"),
                                 MemeRepository(ROOT / "data" / "memes"))
    for key, exported in show["art"].items():
        artifact = catalog.get(key)
        fields = exporter.painting_message_fields(artifact)
        if artifact.kind == "art":
            assert exported["attribution"] == fields["attribution"] == attribution(artifact)
            assert exported["page_url"] == fields["page_url"] == artifact.page_url
            assert exported["rights_statement"] == artifact.rights_statement
        else:
            assert fields == {}  # Original meme provenance must not label a stand-in.
            assert "attribution" not in exported


def test_written_captions_and_shy_variants_are_exported_without_rewording():
    show = json.loads((ROOT.parent / "web" / "data" / "show.json").read_text())
    lines = Lines.load(ROOT / "data" / "performance", "en")
    assert show["telegram_caption"] == lines.captions()
    assert [v["text"] for v in show["lines"]["round"]["hold"]] == lines.variants("round", "hold")
    for variant in show["lines"]["round"]["hold"]:
        assert (ROOT.parent / "web" / variant["audio"]).is_file()


def test_web_audio_reuses_only_verified_same_voice_clips(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, voice_provider="openai", elevenlabs_voice_id="our-voice",
                        audio_cache_dir=tmp_path / "cache")
    from is_this_us.speech import synthesizer

    calls = []
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake wav")

    def factory(config, *, purpose):
        assert config.voice_provider == "elevenlabs"
        def speak(*args):
            calls.append(args)
            return SpeechAudio(source, None, False, "elevenlabs")
        return SimpleNamespace(key_for=lambda *args: config.elevenlabs_voice_id, fx=None, synthesize=speak)

    monkeypatch.setattr(synthesizer, "make_synthesizer", factory)
    monkeypatch.setattr(exporter.subprocess, "run", lambda command, **kw: Path(command[-1]).write_bytes(b"mp3"))
    audio = exporter.WebAudioExporter(settings, tmp_path / "web")
    first = audio("Hello", "lines")
    audio.finish()
    again = exporter.WebAudioExporter(settings, tmp_path / "web")
    assert again("Hello", "lines") == first
    assert len(calls) == 1
    (tmp_path / "web" / first).write_bytes(b"unverified replacement")
    again("Hello", "lines")
    assert len(calls) == 2
    changed = exporter.WebAudioExporter(settings.model_copy(update={"elevenlabs_voice_id": "new-voice"}), tmp_path / "web")
    assert changed("Hello", "lines") != first
    assert len(calls) == 3


def test_web_audio_refuses_non_elevenlabs_result(tmp_path, monkeypatch):
    from is_this_us.speech import synthesizer
    monkeypatch.setattr(synthesizer, "make_synthesizer", lambda *a, **kw: SimpleNamespace(
        key_for=lambda *args: "key", fx=None,
        synthesize=lambda *args: SpeechAudio(tmp_path / "unused.wav", None, False, "openai-realtime")))
    audio = exporter.WebAudioExporter(Settings(_env_file=None, elevenlabs_voice_id="our-voice"), tmp_path)
    with pytest.raises(RuntimeError, match="ElevenLabs clip unavailable"):
        audio("Hello", "lines")


def test_every_browser_clip_has_elevenlabs_provenance():
    web = ROOT.parent / "web"
    show = json.loads((web / "data/show.json").read_text())
    assert show["voice"]["provider"] == "elevenlabs"
    manifest = json.loads((web / show["voice"]["manifest"]).read_text())
    cues = [cue for chapter in show["chapters"] for cue in chapter["cues"]]
    cues += [v for section in show["lines"].values() for variants in section.values() for v in variants]
    for cue in cues:
        if not cue["text"].strip() or cue.get("kind") == "pause":
            continue
        url = cue["audio"]
        assert url.startswith("audio/elevenlabs/")
        entry = manifest["clips"][url]
        assert entry["identity"]["provider"] == "elevenlabs"
        assert entry["identity"]["voice_id"] == show["voice"]["voice_id"]
        assert entry["sha256"] == hashlib.sha256((web / url).read_bytes()).hexdigest()
        assert entry["text_sha256"] == hashlib.sha256(cue["text"].encode()).hexdigest()
