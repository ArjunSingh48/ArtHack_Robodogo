"""A live spoken conversation with the dog (OpenAI Realtime, speech-to-speech).

Flow: mic → `input_audio_buffer.append` → semantic VAD detects the end of the visitor's
turn → the model answers in the dog's voice (`response.output_audio.delta` → speaker).
Tool calls are collected during a response and executed after `response.done`; their
results go back as `function_call_output` items, then a new response is requested.

Echo: with an open speaker the dog would hear itself. Default is HALF-DUPLEX: mic audio
is not sent while the dog is speaking (+ a short tail). With a headset or an
echo-cancelling speakerphone, use full duplex and visitors can interrupt (barge-in).

Privacy: while a session runs, microphone audio is streamed to OpenAI. Nothing is
recorded locally; visitor speech is not transcribed or logged by the installation.
Sessions end after an idle timeout and a maximum length.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field

from .audio import RATE, AudioIO
from .tools import TOOL_SPECS, TalkTools

log = logging.getLogger(__name__)

OPENING = ("A visitor has just come close. Say hello very briefly and shyly, in one or two short sentences, "
           "and offer to tell your story. Do not tell the story yet.")
GOODBYE = "The visitor has gone quiet. Say a very short, shy goodbye in one sentence."


@dataclass
class TalkSettings:
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    half_duplex: bool = True
    idle_timeout_s: float = 45.0
    max_session_s: float = 300.0
    vad_eagerness: str = "low"  # let shy visitors finish their sentence
    noise_reduction: str | None = "far_field"  # gallery: mic not at the mouth


@dataclass
class TalkStats:
    responses: int = 0
    tool_calls: list[str] = field(default_factory=list)
    mic_chunks_sent: int = 0
    mic_chunks_muted: int = 0
    ended_by: str = ""
    robot_lines: list[str] = field(default_factory=list)  # what the DOG said (never the visitor)


class LiveConversation:
    def __init__(self, *, client, audio: AudioIO, tools: TalkTools, instructions: str,
                 settings: TalkSettings | None = None, clock=time.monotonic):
        self.client = client
        self.audio = audio
        self.tools = tools
        self.instructions = instructions
        self.s = settings or TalkSettings()
        self.clock = clock
        self.stats = TalkStats()
        self._mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._responding = False
        self._in_tool = False
        self._pending_calls: list[tuple[str, str, str]] = []  # (call_id, name, arguments)
        self._ending = False
        self._stop = asyncio.Event()
        self._started = self._last_activity = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    def session_config(self) -> dict:
        audio_in: dict = {
            "format": {"type": "audio/pcm", "rate": RATE},
            "turn_detection": {"type": "semantic_vad", "eagerness": self.s.vad_eagerness,
                               "create_response": True, "interrupt_response": not self.s.half_duplex},
        }
        if self.s.noise_reduction:
            audio_in["noise_reduction"] = {"type": self.s.noise_reduction}
        return {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {"input": audio_in,
                      "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.s.voice}},
            "tools": TOOL_SPECS,
            "tool_choice": "auto",
        }

    # ------------------------------------------------------------------ mic (PortAudio thread → asyncio)
    def _on_mic(self, pcm: bytes) -> None:
        if self._loop is None:
            return
        def put():
            if not self._mic.full():
                self._mic.put_nowait(pcm)
        self._loop.call_soon_threadsafe(put)

    def _muted(self) -> bool:
        if self._in_tool:  # the body is busy (pose held); never start a new turn meanwhile
            return True
        return self.s.half_duplex and (self._responding or self.audio.is_playing())

    async def _send_mic(self, conn) -> None:
        while not self._stop.is_set():
            pcm = await self._mic.get()
            if self._muted() or self._ending:
                self.stats.mic_chunks_muted += 1
                continue
            await conn.input_audio_buffer.append(audio=base64.b64encode(pcm).decode())
            self.stats.mic_chunks_sent += 1

    # ------------------------------------------------------------------ server events
    async def _handle(self, conn, event) -> None:
        t = getattr(event, "type", "")
        if t == "response.created":
            self._responding = True
        elif t == "response.output_audio.delta":
            self.audio.play(base64.b64decode(event.delta))
        elif t == "response.output_audio_transcript.done":
            line = (event.transcript or "").strip()
            if line:
                self.stats.robot_lines.append(line)
                log.info("dog: %s", line)
        elif t == "input_audio_buffer.speech_started":
            self._last_activity = self.clock()
            if not self.s.half_duplex and self.audio.is_playing():
                self.audio.clear()  # barge-in: the server cancels the response (interrupt_response)
        elif t == "response.function_call_arguments.done":
            self._pending_calls.append((event.call_id, event.name, event.arguments))
        elif t == "response.done":
            self._responding = False
            self.stats.responses += 1
            self._last_activity = self.clock()
            if self._pending_calls:
                await self._run_tools(conn)
            elif self._ending:
                self.stats.ended_by = self.stats.ended_by or "goodbye"
                self._stop.set()
        elif t == "error":
            err = getattr(event, "error", None)
            code = getattr(err, "code", "") or ""
            log.warning("realtime error: %s %s", code, getattr(err, "message", ""))
            if code in ("invalid_api_key", "authentication_error", "session_expired"):
                self.stats.ended_by = f"error:{code}"
                self._stop.set()

    async def _run_tools(self, conn) -> None:
        calls, self._pending_calls = self._pending_calls, []
        self._in_tool = True
        try:
            await self._exec_calls(conn, calls)
        finally:
            self._in_tool = False
        await conn.response.create()

    async def _exec_calls(self, conn, calls) -> None:
        for call_id, name, arguments in calls:
            self.stats.tool_calls.append(name)
            log.info("tool: %s %s", name, arguments)
            result = await self.tools.call(name, arguments)
            await conn.conversation.item.create(item={
                "type": "function_call_output", "call_id": call_id, "output": json.dumps(result, ensure_ascii=False),
            })

    async def _say(self, conn, instructions: str) -> None:
        await conn.response.create(response={"instructions": instructions})

    # ------------------------------------------------------------------ main loop
    async def run(self) -> TalkStats:
        self._loop = asyncio.get_running_loop()
        self._started = self._last_activity = self.clock()
        async with self.client.realtime.connect(model=self.s.model) as conn:
            await conn.session.update(session=self.session_config())
            await self._say(conn, OPENING)
            self.audio.start(self._on_mic)
            sender = asyncio.create_task(self._send_mic(conn))
            try:
                while not self._stop.is_set():
                    try:
                        event = await asyncio.wait_for(conn.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        event = None
                    if event is not None:
                        await self._handle(conn, event)
                    await self._check_time(conn)
            finally:
                sender.cancel()
                self.audio.close()
        return self.stats

    async def _check_time(self, conn) -> None:
        if self._ending or self._responding or self.audio.is_playing():
            return
        now = self.clock()
        if now - self._started > self.s.max_session_s:
            self.stats.ended_by = "max_session"
        elif now - self._last_activity > self.s.idle_timeout_s:
            self.stats.ended_by = "idle"
        else:
            return
        self._ending = True
        await self._say(conn, GOODBYE)

    def stop(self) -> None:
        self.stats.ended_by = self.stats.ended_by or "operator"
        self._stop.set()
