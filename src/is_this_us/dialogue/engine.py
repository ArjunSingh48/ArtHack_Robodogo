"""Deterministic dialogue templates for the robot's character.

Combines: observed posture confidence, the painting's relationship, the motion
outcome, and session history. Variant choice is a stable hash of (session, turn), so
a given moment always reads the same way, but successive moments vary.

The engine only produces *text*. Its output is never parsed into motion commands.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..domain import (
    Artwork, ExecutionResult, ExecutionStatus, HumanObservation, MotionPlan, Reproducibility,
)

LOCALE_DIR = Path(__file__).parent / "locales"
SUPPORTED = ("en", "ru", "de")


@lru_cache(maxsize=None)
def load_locale(lang: str) -> dict:
    path = LOCALE_DIR / f"{lang}.yaml"
    if not path.exists():
        path = LOCALE_DIR / "en.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class Texts:
    """Locale lookup with English fallback for missing keys."""

    def __init__(self, lang: str = "en"):
        self.lang = lang if lang in SUPPORTED else "en"
        self._data = load_locale(self.lang)
        self._en = load_locale("en")

    def get(self, section: str, key: str):
        value = self._data.get(section, {}).get(key)
        return value if value is not None else self._en[section][key]

    def bot(self, key: str, **kw) -> str:
        text = self.get("bot", key)
        return text.format(**kw) if kw else text


@dataclass
class Utterance:
    lines: list[str]  # shown in Telegram
    spoken: str  # sent to TTS; short, with breathing pauses
    outcome_key: str = ""
    extras: dict = field(default_factory=dict)


def _pick(options: list[str] | str, seed: str) -> str:
    if isinstance(options, str):
        return options
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


class DialogueEngine:
    def __init__(self, lang: str = "en"):
        self.texts = Texts(lang)

    def r(self, key: str):
        return self.texts.get("robot", key)

    @staticmethod
    def outcome_key(plan: MotionPlan, result: ExecutionResult) -> str:
        if result.status is ExecutionStatus.STOPPED:
            return "stopped"
        if result.status is ExecutionStatus.FAILED:
            return "failed"
        if result.status in (ExecutionStatus.SKIPPED, ExecutionStatus.REJECTED) and plan.behavior.value != "stillness":
            return "imagined"
        if plan.fidelity is Reproducibility.NO or plan.behavior.value == "stillness":
            return "still"
        if plan.fidelity is Reproducibility.APPROXIMATE:
            return "partial"
        return "success"

    def compose(self, *, seed: str, observation: HumanObservation, artwork: Artwork | None,
                plan: MotionPlan | None = None, result: ExecutionResult | None = None,
                turn: int = 0, is_retry: bool = False, first_meme: bool = False) -> Utterance:
        if artwork is not None and getattr(artwork, "kind", "art") == "meme":
            return self._compose_meme(seed=seed, observation=observation, meme=artwork, plan=plan,
                                      result=result, is_retry=is_retry, first_meme=first_meme)
        if artwork is None:
            key = "nobody" if observation.people_detected == 0 else "no_match"
            line = _pick(self.r(key), f"{seed}:{key}")
            return Utterance(lines=[line], spoken=line, outcome_key=key)

        lines: list[str] = []
        if is_retry:
            lines.append(_pick(self.r("response_another"), f"{seed}:retry"))
        elif turn > 0:
            lines.append(_pick(self.r("repeat_visit"), f"{seed}:repeat"))
        else:
            lines.append(_pick(self.r("opening"), f"{seed}:open"))

        found_tpl = self.r("found")[artwork.dog.relation.value]
        posture = self.r("dog_posture")[artwork.dog.posture.value]
        found = found_tpl.format(posture=posture)
        if observation.posture_confidence < 0.45:
            found = f"{_pick(self.r('hesitant_prefix'), f'{seed}:hes')} {found}"
        lines.append(_capitalize(found))

        outcome = ""
        if plan is not None and result is not None:
            outcome = self.outcome_key(plan, result)
            if outcome in ("success", "partial"):
                lines.append(_pick(self.r("attempt"), f"{seed}:try"))
            lines.append(_pick(self.r("outcome")[outcome], f"{seed}:{outcome}"))
        question = self.r("question")
        spoken = self.join_spoken([lines[0], lines[1], lines[-1] if len(lines) > 2 else "", question])
        return Utterance(lines=lines + [question], spoken=spoken, outcome_key=outcome)

    def m(self, key: str):
        return self.texts.get("meme", key)

    def _compose_meme(self, *, seed, observation, meme, plan, result, is_retry, first_meme) -> Utterance:
        """Memes get exactly the same sincerity as paintings — only the vocabulary changes."""
        if is_retry:
            opening = _pick(self.r("response_another"), f"{seed}:retry")
        elif first_meme:
            opening = _pick(self.m("opening_first"), f"{seed}:mfirst")
        else:
            opening = _pick(self.m("opening"), f"{seed}:mopen")
        reflection = meme.reflection_for(self.texts.lang) or _pick(self.m("fallback_reflection"), f"{seed}:mref")
        found = f"{self.m('found').format(name=meme.name)} {reflection}"
        if observation.posture_confidence < 0.45:
            found = f"{_pick(self.r('hesitant_prefix'), f'{seed}:hes')} {found[0].lower()}{found[1:]}"
        lines = [opening, found]
        outcome = ""
        if plan is not None and result is not None:
            outcome = self.outcome_key(plan, result)
            if outcome in ("success", "partial"):
                lines.append(_pick(self.m("attempt"), f"{seed}:mtry"))
            lines.append(_pick(self.r("outcome")[outcome], f"{seed}:{outcome}"))
        meme_q, question = self.m("question"), self.r("question")
        lines += [meme_q, question]
        spoken = self.join_spoken([opening, found, lines[-3] if len(lines) > 4 else "", meme_q, question])
        return Utterance(lines=lines, spoken=spoken, outcome_key=outcome)

    @staticmethod
    def join_spoken(parts: list[str]) -> str:
        """Join lines with a breath ("…") between them, never doubling an existing pause."""
        out = ""
        for p in (x.strip() for x in parts if x and x.strip()):
            if not out:
                out = p
            elif out.endswith("…"):
                out += " " + p
            else:
                out += " … " + p
        return out

    def response(self, kind: str, seed: str) -> str:
        """kind: yes | keep | another"""
        return _pick(self.r(f"response_{kind}"), f"{seed}:{kind}")
