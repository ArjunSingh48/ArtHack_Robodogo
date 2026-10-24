"""Tools the dog can use while talking.

The voice model never controls motors. Its only route to the body is
`look_and_remember`, which runs the existing installation pipeline:
camera → pose → curated scene match → curated behavior → MotionGuard (operator arming,
cooldown, battery, stop latch…). The model decides *when* to look, never *how* to move.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..domain import ArchiveSource
from ..orchestrator import BusyError, InteractionOrchestrator
from ..vision.frame_sources import FrameSource

log = logging.getLogger(__name__)

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "name": "look_and_remember",
        "description": (
            "Look at the visitor through the camera, find a painting or meme with a similar person–dog "
            "relationship, and let your body try that dog's pose (only if movement is allowed). "
            "Ask the visitor to take and hold a pose before calling. Returns what you found and what your body did."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "archive": {"type": "string", "enum": ["art", "meme", "both", "surprise"],
                            "description": "Which memory to search. Default: both."},
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "recall_memories",
        "description": "List the paintings and memes you know (titles and who made them). Use before talking about them.",
        "parameters": {
            "type": "object",
            "properties": {"archive": {"type": "string", "enum": ["art", "meme", "both"]}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "describe_memory",
        "description": "Get the facts about one painting or meme by its id (from recall_memories).",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "stop_moving",
        "description": "Immediately stop any body movement. Use whenever someone says stop, wait, too close, or seems afraid.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

NAMES = {t["name"] for t in TOOL_SPECS}


def scene_facts(scene) -> dict:
    if getattr(scene, "kind", "art") == "meme":
        return {"id": scene.id, "kind": "meme", "name": scene.name, "subject": scene.subject, "year": scene.year,
                "attribution": scene.attribution, "meaning": scene.cultural_meaning,
                "your_reflection": scene.reflection.get("en", ""),
                "dog": {"posture": scene.dog.posture.value, "interaction": scene.dog.interaction.value}}
    return {"id": scene.id, "kind": "painting", "title": scene.title, "artist": scene.artist, "date": scene.date,
            "museum": scene.museum, "your_note": scene.poem,
            "human": scene.human.posture.value,
            "dog": {"posture": scene.dog.posture.value, "relation": scene.dog.relation.value,
                    "interaction": scene.dog.interaction.value}}


class TalkTools:
    def __init__(self, orchestrator: InteractionOrchestrator, frame_source: FrameSource | None, chat_id: int = 0):
        self.orch = orchestrator
        self.source = frame_source
        self.chat_id = chat_id  # a local, anonymous session id — talk mode stores no identity
        self.calls: list[str] = []

    async def call(self, name: str, arguments: str | dict | None) -> dict:
        self.calls.append(name)
        if name not in NAMES:
            return {"error": f"unknown tool {name}"}
        try:
            args: dict[str, Any] = json.loads(arguments) if isinstance(arguments, str) and arguments else dict(arguments or {})
        except json.JSONDecodeError:
            args = {}
        try:
            return await getattr(self, f"_{name}")(**args)
        except TypeError as exc:  # unexpected arguments from the model
            return {"error": f"bad arguments: {exc}"}

    async def _look_and_remember(self, archive: str = "both") -> dict:
        if self.source is None:
            return {"found": False, "note": "No camera is connected, so you can't look right now. Say so gently."}
        try:
            archive = ArchiveSource(archive).value
        except ValueError:
            archive = "both"
        try:
            out = await self.orch.observe_installation(self.chat_id, self.source, archive=archive)
        except BusyError:
            return {"found": False, "note": "You are still finishing the last pose. Ask them to wait a moment."}
        o = out.observation
        result = {
            "you_saw": {"posture": o.posture.value, "confidence": o.posture_confidence,
                        "people": o.people_detected, "gestures": [g.value for g in o.gestures]},
            "found": out.artwork is not None,
            "body": {"tried": out.plan.behavior.value, "fidelity": out.plan.fidelity.value,
                     "status": out.result.status.value},
            "body_note": {
                "success": "You did the pose.", "simulated": "You did the pose (in simulation).",
                "skipped": "Movement is not allowed right now, so you only imagined the pose.",
                "rejected": "Your body could not move just now (safety); you only imagined it.",
                "failed": "Your body didn't manage it.", "stopped": "You stopped.",
            }.get(out.result.status.value, ""),
        }
        if out.artwork is not None:
            result["memory"] = scene_facts(out.artwork)
        else:
            result["note"] = "No picture matched. Say you haven't seen this gesture before."
        return result

    async def _recall_memories(self, archive: str = "both") -> dict:
        try:
            src = ArchiveSource(archive)
        except ValueError:
            src = ArchiveSource.BOTH
        items = [{"id": s.id, "kind": s.kind, "title": s.title,
                  "by": s.attribution if s.kind == "meme" else s.artist, "date": s.date}
                 for s in self.orch.repo.all(src)]
        return {"count": len(items), "memories": items[:40]}

    async def _describe_memory(self, id: str) -> dict:
        scene = self.orch.repo.get(id)
        return scene_facts(scene) if scene else {"error": "You don't have that memory."}

    async def _stop_moving(self) -> dict:
        self.orch.stop("talk mode: stop_moving")
        return {"stopped": True, "note": "Your body is still now."}
