import shutil
import subprocess

import pytest

from is_this_us.speech.synthesizer import AudioCache, ElevenLabsSynthesizer, to_telegram_voice


class ApiErr(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


class FakeTTS:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    def convert(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise ApiErr(self.errors.pop(0))
        return iter([b"ID3", b"fake-mp3"])


class FakeClient:
    def __init__(self, errors=()):
        self.text_to_speech = FakeTTS(errors)


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr("is_this_us.speech.synthesizer.to_telegram_voice", lambda mp3, ogg: None)


def synth(tmp_path, errors=()):
    client = FakeClient(errors)
    return ElevenLabsSynthesizer("key", "voice123", "eleven_multilingual_v2", AudioCache(tmp_path), client=client), client


def test_only_text_is_sent_and_cache_avoids_repeat(tmp_path):
    s, client = synth(tmp_path)
    a = s.synthesize("Is this us?")
    b = s.synthesize("Is this us?")
    assert a and b and not a.cached and b.cached
    assert len(client.text_to_speech.calls) == 1
    sent = client.text_to_speech.calls[0]
    assert set(sent) <= {"voice_id", "text", "model_id", "output_format", "voice_settings", "language_code"}
    assert sent["text"] == "Is this us?"


def test_invalid_credentials_disable_voice(tmp_path):
    s, _ = synth(tmp_path, errors=[401])
    assert s.synthesize("hello") is None
    assert not s.available
    assert s.synthesize("hello again") is None


def test_rate_limit_retries_once(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    s, client = synth(tmp_path, errors=[429])
    assert s.synthesize("Wait…") is not None
    assert len(client.text_to_speech.calls) == 2


def test_other_errors_fall_back_to_text(tmp_path):
    s, _ = synth(tmp_path, errors=[500])
    assert s.synthesize("Wait…") is None
    assert s.available  # transient: keep trying next time


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_makes_telegram_voice(tmp_path):
    mp3 = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=0.5", str(mp3)], check=True)
    ogg = to_telegram_voice(mp3, tmp_path / "tone.ogg")
    assert ogg and ogg.read_bytes()[:4] == b"OggS"
