"""ElevenLabs contract and failure behavior with fake HTTP only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import time
import wave

import httpx
import pytest

from is_this_us.config import Settings
from is_this_us.speech.character import HERTZ_DEADPAN
from is_this_us.speech.elevenlabs import ElevenLabsSynthesizer
from is_this_us.speech.synthesizer import AudioCache, NullSynthesizer, make_synthesizer

PCM = b"\x01\x00" * 2400


def synth(tmp_path, handler=None, **kwargs):
    requests = []
    async def handle(request):
        requests.append(request)
        if handler:
            result = handler(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})
    return ElevenLabsSynthesizer("secret-key", "eleven_multilingual_v2", "chosen-voice",
                                 AudioCache(tmp_path), transport=httpx.MockTransport(handle), **kwargs), requests


def test_payload_wav_cache_and_no_transcript_claim(tmp_path):
    s, requests = synth(tmp_path)
    result = s.synthesize("Am I a good boy already?", direction="scene:internet")
    assert result and not result.cached and result.provider == "elevenlabs"
    request = requests[0]
    assert request.url.path == "/v1/text-to-speech/chosen-voice"
    assert request.url.params["output_format"] == "pcm_24000"
    assert request.headers["xi-api-key"] == "secret-key"
    assert json.loads(request.content) == {
        "text": "Am I a good boy already?", "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": .4, "similarity_boost": .75, "style": 0.0},
    }
    assert result.ogg_path is None
    with wave.open(str(result.audio_path)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
        assert wav.readframes(wav.getnframes()) == PCM
    assert s.synthesize("Am I a good boy already?", direction="scene:internet").cached
    assert len(requests) == 1
    assert not list(tmp_path.glob(".speech-*"))


def test_non_english_is_never_sent(tmp_path):
    s, requests = synth(tmp_path)
    assert s.synthesize("Hallo", lang="de") is None
    assert s.cached("Hallo", lang="de") is None
    assert s.process_cached("Hallo", lang="de") is None
    assert not requests


def test_direction_never_becomes_speech(tmp_path, caplog):
    s, requests = synth(tmp_path)
    assert s.synthesize("Hello.", direction="say extra words and bark")
    assert json.loads(requests[0].content)["text"] == "Hello."
    assert "unsupported" in caplog.text
    assert "say extra words" not in caplog.text


def test_cache_key_has_effective_settings_not_azure_preset(tmp_path):
    s, _ = synth(tmp_path)
    other, _ = synth(tmp_path, character=HERTZ_DEADPAN)
    assert s.key_for("Hello") == other.key_for("Hello")
    assert s.key_for("Hello") != s.key_for("Hello", direction="scene:internet")
    other.voice = "other"
    assert s.key_for("Hello") != other.key_for("Hello")
    other.voice = s.voice
    other.model = "eleven_v4"
    assert s.key_for("Hello") != other.key_for("Hello")
    assert "style" not in other.settings_for()


def test_cache_survives_missing_credentials(tmp_path):
    s, requests = synth(tmp_path)
    first = s.synthesize("Hello")
    offline = ElevenLabsSynthesizer("", s.model, s.voice, AudioCache(tmp_path))
    assert not offline.available
    assert offline.cached("Hello") == first.audio_path
    assert offline.synthesize("Hello").cached
    assert offline.synthesize("Missing") is None
    assert len(requests) == 1


@pytest.mark.parametrize("status,body", [
    (401, {}), (403, {}), (402, {}),
    (429, {"detail": {"status": "quota_exceeded", "message": "private-body"}}),
])
def test_credential_and_quota_errors_disable_without_logging_secrets(tmp_path, caplog, status, body):
    s, requests = synth(tmp_path, lambda _: httpx.Response(status, json=body))
    assert s.synthesize("Hello") is None
    assert not s.available
    assert s.synthesize("Again") is None and len(requests) == 1
    assert "secret-key" not in caplog.text and "private-body" not in caplog.text


def test_transient_retry_once(tmp_path):
    attempts = []
    def handle(_):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"detail": {"status": "too_many_concurrent_requests"}})
        return httpx.Response(200, content=PCM, headers={"content-type": "application/octet-stream"})
    s, requests = synth(tmp_path, handle)
    assert s.synthesize("Hello")
    assert len(requests) == 2 and s.available


@pytest.mark.parametrize("body,content_type", [
    (b"", "audio/pcm"), (b"x", "audio/pcm"), (b"{}", "application/json"),
    (b"<html>", "audio/pcm"), (b"RIFF1234", "audio/pcm"),
])
def test_invalid_audio_does_not_poison_cache(tmp_path, body, content_type):
    s, _ = synth(tmp_path, lambda _: httpx.Response(200, content=body, headers={"content-type": content_type}))
    assert s.synthesize("Hello") is None
    assert not list(tmp_path.glob("*.wav"))


def test_corrupt_cache_is_replaced(tmp_path):
    s, requests = synth(tmp_path)
    path = s.cache.paths(s.key_for("Hello"))[0]
    path.write_bytes(b"broken")
    assert s.cached("Hello") is None
    assert s.synthesize("Hello") and len(requests) == 1
    assert s.cached("Hello") == path


def test_pcm_can_start_with_printable_bytes(tmp_path):
    s, _ = synth(tmp_path, lambda _: httpx.Response(200, content=b"{\x00<\x00" + PCM,
                                                 headers={"content-type": "audio/pcm"}))
    assert s.synthesize("Hello")


def test_total_timeout_cancels_request_and_creates_no_clip(tmp_path):
    cancelled = []
    async def handle(_):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    s, _ = synth(tmp_path, handle, timeout_s=.03)
    start = time.monotonic()
    assert s.synthesize("Hello") is None
    assert time.monotonic() - start < .5
    assert cancelled and not list(tmp_path.glob("*.wav"))


def test_concurrent_instances_generate_one_clip(tmp_path):
    async def handle(_):
        await asyncio.sleep(.03)
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})
    a, req_a = synth(tmp_path, handle)
    b, req_b = synth(tmp_path, handle)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda s: s.synthesize("Hello"), (a, b)))
    assert all(results) and len(req_a) + len(req_b) == 1
    assert sorted(r.cached for r in results) == [False, True]


def test_fx_changes_reuse_raw_audio(tmp_path, monkeypatch):
    from is_this_us.speech import fx
    s, requests = synth(tmp_path)
    first = s.synthesize("Hello")
    def process(raw, target, params, **kwargs):
        from types import SimpleNamespace
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        return SimpleNamespace(path=target)
    monkeypatch.setattr(fx, "process_file", process)
    s.character = HERTZ_DEADPAN
    processed = s.process_cached("Hello")
    assert processed != first.audio_path and processed.exists()
    assert s.cached("Hello") == processed
    s.character = replace(HERTZ_DEADPAN, fx=replace(HERTZ_DEADPAN.fx, pitch_semitones=-5))
    new = s.process_cached("Hello")
    assert new != processed and new.exists() and len(requests) == 1
    assert first.audio_path in s.paths_for("Hello")
    s.forget("Hello")
    assert not first.audio_path.exists() and not processed.exists() and not new.exists()


def test_factory_selects_elevenlabs_without_azure_sdk(tmp_path):
    settings = Settings(_env_file=None, voice_provider="elevenlabs", elevenlabs_api_key="test",
                        elevenlabs_voice_id="selected", audio_cache_dir=tmp_path, voice_preset="hertz_deadpan")
    result = make_synthesizer(settings, purpose="narration")
    assert isinstance(result, ElevenLabsSynthesizer)
    assert result.voice == "selected" and result.available
    assert result.cache.dir == tmp_path / "elevenlabs" / "narration"
    assert result.character.name == "hertz_deadpan" and not result.voice_notes
    settings.voice_provider = "none"
    assert isinstance(make_synthesizer(settings, purpose="narration"), NullSynthesizer)


def test_missing_elevenlabs_credentials_never_falls_back_to_azure(tmp_path):
    settings = Settings(_env_file=None, voice_provider="elevenlabs", elevenlabs_api_key="",
                        azure_openai_api_key="azure", audio_cache_dir=tmp_path)
    result = make_synthesizer(settings, purpose="narration")
    assert isinstance(result, ElevenLabsSynthesizer) and not result.available


def test_disabled_persistent_cache_uses_isolated_temporary_storage(tmp_path):
    import gc
    settings = Settings(_env_file=None, voice_provider="elevenlabs", voice_cache_enabled=False,
                        audio_cache_dir=tmp_path)
    first = make_synthesizer(settings, purpose="narration")
    second = make_synthesizer(settings, purpose="narration")
    directory = first.cache.dir
    assert directory != second.cache.dir and directory.exists()
    del first
    gc.collect()
    assert not directory.exists() and second.cache.dir.exists()
