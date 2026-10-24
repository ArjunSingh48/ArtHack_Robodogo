"""OpenAI Realtime voice, against a FAKE connection that mirrors the SDK surface
(`client.realtime.connect(model=...)`, `session.update`, `conversation.item.create`,
`response.create`, `recv()`) and the documented server events."""

import base64
import shutil
import wave
from types import SimpleNamespace as NS

import pytest

from is_this_us.speech.synthesizer import (
    AudioCache, OpenAIRealtimeSynthesizer, SAMPLE_RATE, faithful, to_telegram_voice,
)

PCM = b"\x01\x00" * 2400  # 0.1 s of 24 kHz PCM16


class FakeConn:
    def __init__(self, script):
        self.script, self.sent, self._events = script, [], []
        self.session = NS(update=self._record("session.update"))
        self.conversation = NS(item=NS(create=self._record("conversation.item.create")))
        self.response = NS(create=self._record("response.create"))

    def _record(self, name):
        async def f(**kw):
            self.sent.append((name, kw))
            if name == "response.create":
                self._events = list(self.script(self._text()))
        return f

    def _text(self):
        item = next(kw["item"] for n, kw in self.sent if n == "conversation.item.create")
        return item["content"][0]["text"]

    async def recv(self):
        return self._events.pop(0)


class FakeClient:
    def __init__(self, script=None, errors=()):
        self.errors = list(errors)
        self.conns = []
        self.script = script or (lambda text: [
            NS(type="response.output_audio.delta", delta=base64.b64encode(PCM[:2400]).decode()),
            NS(type="response.output_audio.delta", delta=base64.b64encode(PCM[2400:]).decode()),
            NS(type="response.output_audio_transcript.done", transcript=text),
            NS(type="response.done"),
        ])
        self.realtime = NS(connect=self._connect)

    def _connect(self, model, **kw):
        client = self

        class Manager:
            async def __aenter__(self):
                if client.errors:
                    raise client.errors.pop(0)
                conn = FakeConn(client.script)
                client.conns.append((model, conn))
                return conn

            async def __aexit__(self, *a):
                return False
        return Manager()


class ApiErr(Exception):
    def __init__(self, status_code=None, code=None):
        self.status_code, self.code = status_code, code


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr("is_this_us.speech.synthesizer.to_telegram_voice", lambda src, ogg: None)
    monkeypatch.setattr("time.sleep", lambda *_: None)


def synth(tmp_path, **kw):
    client = FakeClient(**kw)
    return OpenAIRealtimeSynthesizer("sk-test", "gpt-realtime-2.1", "marin", AudioCache(tmp_path), client=client), client


def test_session_setup_sends_only_text_and_caches(tmp_path):
    s, client = synth(tmp_path)
    a = s.synthesize("Wait… Is this us?", "ru")
    b = s.synthesize("Wait… Is this us?", "ru")
    assert a and b and not a.cached and b.cached
    assert len(client.conns) == 1  # cache hit: no second session
    model, conn = client.conns[0]
    assert model == "gpt-realtime-2.1"
    names = [n for n, _ in conn.sent]
    assert names == ["session.update", "conversation.item.create", "response.create"]
    session = conn.sent[0][1]["session"]
    assert session["type"] == "realtime" and session["output_modalities"] == ["audio"]
    assert session["audio"]["output"] == {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}
    assert "EXACTLY" in session["instructions"] and "Russian" in session["instructions"]
    item = conn.sent[1][1]["item"]
    assert item["content"] == [{"type": "input_text", "text": "Wait… Is this us?"}]  # nothing else leaves
    with wave.open(str(a.audio_path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2)
        assert w.readframes(w.getnframes()) == PCM


def test_improvised_speech_is_discarded(tmp_path):
    s, client = synth(tmp_path, script=lambda text: [
        NS(type="response.output_audio.delta", delta=base64.b64encode(PCM).decode()),
        NS(type="response.output_audio_transcript.done", transcript="Sure! Here is a fun fact about dogs."),
        NS(type="response.done"),
    ])
    assert s.synthesize("Is this us?") is None
    assert len(client.conns) == 2  # retried once, then text-only
    assert s.available and list(tmp_path.glob("*.wav")) == []


def test_invalid_credentials_disable_voice(tmp_path):
    s, client = synth(tmp_path, errors=[ApiErr(status_code=401)])
    assert s.synthesize("hello") is None and not s.available
    assert s.synthesize("hello again") is None and client.conns == []


def test_websocket_close_reason_invalid_key_disables_voice(tmp_path):
    class Closed(Exception):  # shape of websockets.ConnectionClosedError seen from the real API
        code, reason = 3000, "invalid_request_error.invalid_api_key"

    s, client = synth(tmp_path, errors=[Closed()])
    assert s.synthesize("hello") is None and not s.available


def test_rate_limit_retries_once(tmp_path):
    s, client = synth(tmp_path, errors=[ApiErr(status_code=429)])
    assert s.synthesize("Wait…") is not None and len(client.conns) == 1


def test_server_error_event_and_network_failures_fall_back(tmp_path):
    s, _ = synth(tmp_path, script=lambda text: [NS(type="error", error=NS(code="server_error", message="boom"))])
    assert s.synthesize("Wait…") is None and s.available
    s2, _ = synth(tmp_path, errors=[ConnectionError("no network")])
    assert s2.synthesize("Wait…") is None and s2.available


def test_faithfulness_check():
    assert faithful("Wait… I remember a painting.", "Wait, I remember a painting")
    assert faithful("Это мы?", "Это мы")
    assert not faithful("Is this us?", "Of course it is you and me, my friend!")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_makes_telegram_voice(tmp_path):
    from is_this_us.speech.synthesizer import write_wav

    src = tmp_path / "tone.wav"
    write_wav(src, PCM)
    ogg = to_telegram_voice(src, tmp_path / "tone.ogg")
    assert ogg and ogg.read_bytes()[:4] == b"OggS"
