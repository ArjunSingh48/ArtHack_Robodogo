"""Talk mode with a FAKE Realtime connection and FAKE audio (no network, no microphone).

Payloads are also validated against the official SDK's pydantic event schemas, so a
wrong field name in session.update or function_call_output fails here, not in the gallery."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from is_this_us.domain import RobotBehavior
from is_this_us.matching.matcher import SceneMatcher
from is_this_us.memes.repository import MemeRepository
from is_this_us.scenes import SceneLibrary
from is_this_us.talk.persona import build_instructions, load_story
from is_this_us.talk.session import LiveConversation, TalkSettings
from is_this_us.talk.tools import TOOL_SPECS, TalkTools
from is_this_us.vision.frame_sources import Frame
from tests.conftest import FakeEstimator

ROOT = Path(__file__).resolve().parents[1]
PCM = b"\x02\x00" * 480


class FakeAudio:
    def __init__(self):
        self.played, self.cleared, self.closed = bytearray(), 0, False
        self.on_mic = None
        self.playing = False

    def start(self, on_mic):
        self.on_mic = on_mic

    def play(self, pcm):
        self.played.extend(pcm)

    def clear(self):
        self.cleared += 1
        self.playing = False

    def is_playing(self):
        return self.playing

    def close(self):
        self.closed = True


class FakeConn:
    """Replays a scripted list of server events; records everything the client sends."""

    def __init__(self, events, audio: FakeAudio):
        self.events, self.audio, self.sent = list(events), audio, []
        rec = self._rec
        self.session = NS(update=rec("session.update"))
        self.conversation = NS(item=NS(create=rec("conversation.item.create")))
        self.response = NS(create=rec("response.create"))
        self.input_audio_buffer = NS(append=rec("input_audio_buffer.append"))

    def _rec(self, name):
        async def f(**kw):
            self.sent.append((name, kw))
        return f

    async def recv(self):
        await asyncio.sleep(0.01)
        if not self.events:
            await asyncio.sleep(3600)
        ev = self.events.pop(0)
        if callable(ev):  # a step that acts (e.g. the visitor speaks into the mic)
            return ev()
        return ev


class FakeClient:
    def __init__(self, conn):
        self.conn = conn
        self.realtime = NS(connect=lambda model, **kw: self._mgr(model))

    def _mgr(self, model):
        conn = self.conn

        class M:
            async def __aenter__(self):
                conn.model = model
                return conn

            async def __aexit__(self, *a):
                return False
        return M()


def audio_delta():
    return NS(type="response.output_audio.delta", delta=base64.b64encode(PCM).decode())


class StaticSource:
    name = "test:static"

    def read(self):
        return Frame(np.zeros((48, 64, 3), dtype=np.uint8), self.name)

    def close(self):
        pass


@pytest.fixture
def orch(make_orchestrator, repo):
    from is_this_us.notifier import ConsoleNotifier
    from is_this_us.speech.synthesizer import NullSynthesizer

    o = make_orchestrator(estimator=FakeEstimator("sitting", "holding"), notifier=ConsoleNotifier(),
                          synth=NullSynthesizer())
    lib = SceneLibrary(repo, MemeRepository(ROOT / "data" / "memes"))
    o.repo, o.matcher = lib, SceneMatcher(lib)
    o.storage.touch_visitor(0)
    return o


def convo(events, orch, *, half_duplex=True, source=StaticSource(), clock=None, **settings):
    audio = FakeAudio()
    conn = FakeConn(events, audio)
    c = LiveConversation(client=FakeClient(conn), audio=audio, tools=TalkTools(orch, source),
                         instructions=build_instructions(load_story(ROOT / "data" / "persona" / "story.md")),
                         settings=TalkSettings(half_duplex=half_duplex, idle_timeout_s=settings.get("idle", 60),
                                               max_session_s=settings.get("max", 600)),
                         **({"clock": clock} if clock else {}))
    return c, conn, audio


# ------------------------------------------------------------------ persona

def test_story_and_character():
    story = load_story(ROOT / "data" / "persona" / "story.md")
    assert "<!--" not in story and "real dogs behave" in story and "memes" in story.lower()
    text = build_instructions(story, "German")
    assert "shy and cute" in text and "German" in text
    assert "ONLY from the text under YOUR STORY" in text
    assert "cannot move by talking" in text


# ------------------------------------------------------------------ payloads vs the SDK schema

def test_session_config_matches_sdk_schema(orch):
    from openai.types.realtime import SessionUpdateEvent

    c, _, _ = convo([], orch)
    ev = SessionUpdateEvent.model_validate({"type": "session.update", "session": c.session_config()})
    s = ev.session
    assert s.type == "realtime" and s.output_modalities == ["audio"]
    assert s.audio.input.turn_detection.type == "semantic_vad"
    assert s.audio.output.voice == "marin" and s.audio.input.format.rate == 24000
    assert {t.name for t in s.tools} == {"look_and_remember", "recall_memories", "describe_memory", "stop_moving"}


def test_no_tool_can_command_the_motors():
    blob = json.dumps(TOOL_SPECS).lower()
    for word in ("behavior", "motor", "euler", "sit", "walk", "move_to", "velocity"):
        assert f'"{word}"' not in blob  # no parameter lets the model pick a movement
    params = {t["name"]: set(t["parameters"].get("properties", {})) for t in TOOL_SPECS}
    assert params == {"look_and_remember": {"archive"}, "recall_memories": {"archive"},
                      "describe_memory": {"id"}, "stop_moving": set()}


# ------------------------------------------------------------------ conversation flow

async def test_opening_audio_and_goodbye(orch):
    t = [0.0]
    events = [
        NS(type="response.created"), audio_delta(), audio_delta(),
        NS(type="response.output_audio_transcript.done", transcript="Um… hello. Would you like to hear my story?"),
        NS(type="response.done"),
        lambda: (t.__setitem__(0, 100.0), NS(type="noop"))[1],  # time passes, nobody speaks
        NS(type="response.created"), audio_delta(), NS(type="response.done"),  # the goodbye
    ]
    c, conn, audio = convo(events, orch, clock=lambda: t[0], idle=45)
    stats = await asyncio.wait_for(c.run(), 5)
    names = [n for n, _ in conn.sent]
    assert names[:2] == ["session.update", "response.create"]
    assert "offer to tell your story" in conn.sent[1][1]["response"]["instructions"]
    assert "goodbye" in conn.sent[-1][1]["response"]["instructions"]
    assert stats.ended_by == "idle" and stats.responses == 2
    assert bytes(audio.played) == PCM * 3 and audio.closed
    assert stats.robot_lines == ["Um… hello. Would you like to hear my story?"]


async def test_tool_call_runs_pipeline_then_answers(orch):
    events = [
        NS(type="response.created"),
        NS(type="response.function_call_arguments.done", call_id="c1", name="look_and_remember",
           arguments='{"archive": "meme"}'),
        NS(type="response.done"),
        NS(type="response.created"), audio_delta(), NS(type="response.done"),
    ]
    c, conn, audio = convo(events, orch)
    task = asyncio.create_task(c.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if c.stats.responses >= 2:
            break
    c.stop()
    await asyncio.wait_for(task, 5)
    out = next(kw for n, kw in conn.sent if n == "conversation.item.create")
    from openai.types.realtime import ConversationItemCreateEvent

    ConversationItemCreateEvent.model_validate({"type": "conversation.item.create", **out})
    result = json.loads(out["item"]["output"])
    assert out["item"]["call_id"] == "c1"
    assert result["found"] and result["memory"]["id"] == "meme:this-is-fine"  # sitting + holding a cup
    assert result["body"]["tried"] == "sit" and result["body"]["status"] == "simulated"
    names = [n for n, _ in conn.sent]
    assert names.index("conversation.item.create") < len(names) - 1 and names[-1] == "response.create"
    assert c.stats.tool_calls == ["look_and_remember"]
    assert orch.guard.robot.calls == ["Sit"]  # curated behavior, executed only via MotionGuard (mock robot here)
    assert orch.storage.count_interactions(0) == 1


async def test_half_duplex_mutes_mic_while_dog_speaks(orch):
    def visitor_speaks():
        c.audio.on_mic(PCM)  # arrives while the dog is still talking
        return NS(type="response.output_audio.delta", delta=base64.b64encode(PCM).decode())

    events = [NS(type="response.created"), visitor_speaks, NS(type="response.done"),
              lambda: (c.audio.on_mic(PCM), NS(type="noop"))[1]]
    c, conn, audio = convo(events, orch)
    task = asyncio.create_task(c.run())
    await asyncio.sleep(0.4)
    c.stop()
    await asyncio.wait_for(task, 5)
    appended = [kw for n, kw in conn.sent if n == "input_audio_buffer.append"]
    assert c.stats.mic_chunks_muted == 1 and c.stats.mic_chunks_sent == 1 and len(appended) == 1
    assert base64.b64decode(appended[0]["audio"]) == PCM


async def test_full_duplex_barge_in_clears_playback(orch):
    def speaking():
        c.audio.playing = True
        return audio_delta()

    events = [NS(type="response.created"), speaking, NS(type="input_audio_buffer.speech_started", audio_start_ms=0)]
    c, conn, audio = convo(events, orch, half_duplex=False)
    task = asyncio.create_task(c.run())
    await asyncio.sleep(0.3)
    c.stop()
    await asyncio.wait_for(task, 5)
    assert audio.cleared == 1
    td = c.session_config()["audio"]["input"]["turn_detection"]
    assert td["interrupt_response"] is True


async def test_invalid_key_error_event_ends_session(orch):
    events = [NS(type="error", error=NS(code="invalid_api_key", message="bad key"))]
    c, _, _ = convo(events, orch)
    stats = await asyncio.wait_for(c.run(), 5)
    assert stats.ended_by == "error:invalid_api_key"


# ------------------------------------------------------------------ tools

async def test_tools_recall_describe_stop_and_bad_input(orch):
    tools = TalkTools(orch, None)
    mem = await tools.call("recall_memories", '{"archive": "meme"}')
    assert mem["count"] == 5 and all(m["kind"] == "meme" for m in mem["memories"])
    d = await tools.call("describe_memory", {"id": "met:437173"})
    assert d["artist"].startswith("Bartolom") and d["kind"] == "painting"
    assert "error" in await tools.call("describe_memory", {"id": "met:000"})
    assert (await tools.call("look_and_remember", "{}"))["found"] is False  # no camera
    assert "error" in await tools.call("do_a_backflip", "{}")
    assert "error" in await tools.call("describe_memory", '{"id": "x", "speed": 9}')
    stopped = await tools.call("stop_moving", "")
    assert stopped["stopped"] and "StopMove" in orch.guard.robot.calls


async def test_look_and_remember_respects_unapproved_poses(orch):
    """Even in conversation, look_aside stays off until an operator approves it."""
    orch.interpreter = NS(interpret=lambda people, source="": __import__(
        "is_this_us.domain", fromlist=["x"]).HumanObservation(
        posture="standing", posture_confidence=0.9, gestures={"turned_away": 0.9}))
    r = await TalkTools(orch, StaticSource()).call("look_and_remember", '{"archive": "meme"}')
    assert r["memory"]["id"] == "meme:side-eye-dog"
    assert r["body"]["tried"] == "stand" and r["body"]["fidelity"] == "approximate"
    assert RobotBehavior.LOOK_ASIDE not in orch.planner.allowed
