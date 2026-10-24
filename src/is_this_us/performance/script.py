"""The performance script: editable text → chapters and cues.

The narration lives in `data/performance/script.<lang>.md` (see the format notes at the
top of that file). Python never contains the narration itself.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

CHAPTER_ORDER = ("last_dog", "prehistory", "middle_ages", "paintings", "photography", "internet",
                 "invitation", "present")
BRANCHES = ("always", "together", "alone")
SPECIAL_SHOWS = {"title", "blank", "memes", "composition", "chosen"}
TRY_TARGETS = ("any", "standing", "sitting", "head_tilt")

_PAUSE = re.compile(r"^\[pause\s+([0-9.]+)\]$", re.I)
_CHAPTER = re.compile(r"^##\s+([a-z_]+)\s*\|\s*(.+)$")
_DIRECTIVE = re.compile(r"^@([a-z]+)\s*(?::\s*(.*))?$")


class ScriptError(ValueError):
    pass


@dataclass(frozen=True)
class Cue:
    id: str  # "<chapter>.<nn>" — stable while the chapter's order is unchanged
    chapter: str
    kind: str  # "speech" | "pause" | "try" (a small moment for the participant, inside the story)
    text: str = ""  # lines joined with "\n"
    seconds: float = 0.0  # pauses
    show: str | None = None  # display change when this cue starts
    motion: str | None = None  # optional robot behavior with this cue
    hold_after: bool = False  # guided mode: wait for the operator after this cue
    branch: str = "always"
    target: str = ""  # try cues: any | standing | sitting | head_tilt
    yes: str = ""  # try cues: said when the camera sees it
    no: str = ""  # try cues: said when it doesn't (or nobody is there)

    @property
    def needs_pose_request(self) -> bool:
        return "{pose_request}" in self.text

    def render(self, **values: str) -> str:
        text = self.text
        for k, v in values.items():
            text = text.replace("{" + k + "}", v)
        return text


@dataclass
class Chapter:
    id: str
    title: str
    voice: str = ""
    interactive: bool = False
    cues: list[Cue] = field(default_factory=list)

    def cues_for(self, outcome: str) -> list[Cue]:
        return [c for c in self.cues if c.branch in ("always", outcome)]


@dataclass
class Script:
    lang: str
    chapters: list[Chapter]
    source: Path | None = None

    def chapter(self, cid: str) -> Chapter:
        for c in self.chapters:
            if c.id == cid:
                return c
        raise KeyError(cid)

    def cue(self, cue_id: str) -> Cue:
        for ch in self.chapters:
            for c in ch.cues:
                if c.id == cue_id:
                    return c
        raise KeyError(cue_id)

    def speech_cues(self) -> list[Cue]:
        """Everything with fixed words: narration and the requests/reactions of try cues."""
        out = []
        for ch in self.chapters:
            for c in ch.cues:
                if c.kind == "speech":
                    out.append(c)
                elif c.kind == "try":
                    out.append(c)
        return out

    def try_cues(self) -> list[Cue]:
        return [c for ch in self.chapters for c in ch.cues if c.kind == "try"]

    def shown_ids(self) -> set[str]:
        return {c.show for ch in self.chapters for c in ch.cues if c.show and c.show not in SPECIAL_SHOWS}

    def fingerprint(self) -> str:
        blob = "\n".join(f"{c.id}|{c.text}" for c in self.speech_cues())
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def parse_script(text: str, lang: str = "en", source: Path | None = None) -> Script:
    chapters: list[Chapter] = []
    cur: Chapter | None = None
    buf: list[str] = []
    pending: dict = {}
    branch = "always"

    def flush() -> None:
        nonlocal buf, pending
        if not buf:
            return
        assert cur is not None
        cur.cues.append(Cue(id=f"{cur.id}.{len(cur.cues) + 1:02d}", chapter=cur.id,
                            kind="try" if "try" in pending else "speech", text="\n".join(buf),
                            show=pending.get("show"), motion=pending.get("motion"), branch=branch,
                            target=pending.get("try", "")))
        buf, pending = [], {}

    for n, raw in enumerate(_strip_comments(text).splitlines(), 1):
        line = raw.strip()
        if not line:
            flush()
            continue
        m = _CHAPTER.match(line)
        if m:
            flush()
            if pending:
                raise ScriptError(f"line {n}: directives {sorted(pending)} before a chapter have no cue")
            cur = Chapter(id=m.group(1), title=m.group(2).strip())
            chapters.append(cur)
            branch = "always"
            continue
        if cur is None:
            raise ScriptError(f"line {n}: text before the first '## chapter' heading")
        m = _DIRECTIVE.match(line)
        if m:
            name, value = m.group(1), (m.group(2) or "").strip()
            if name == "hold":
                flush()
                if not cur.cues:
                    raise ScriptError(f"line {n}: @hold before any cue")
                last = cur.cues[-1]
                cur.cues[-1] = Cue(**{**last.__dict__, "hold_after": True})
            elif name == "voice":
                cur.voice = value
            elif name == "interactive":
                cur.interactive = True
            elif name == "branch":
                flush()
                if value not in BRANCHES:
                    raise ScriptError(f"line {n}: @branch must be one of {BRANCHES}")
                branch = value
            elif name == "try":
                flush()
                if value not in TRY_TARGETS:
                    raise ScriptError(f"line {n}: @try must be one of {TRY_TARGETS}")
                pending["try"] = value
            elif name in ("yes", "no"):
                flush()
                if not cur.cues or cur.cues[-1].kind != "try":
                    raise ScriptError(f"line {n}: @{name} must follow a @try cue")
                if not value:
                    raise ScriptError(f"line {n}: @{name} needs a line")
                last = cur.cues[-1]
                cur.cues[-1] = Cue(**{**last.__dict__, name: value})
            elif name in ("show", "motion"):
                flush()
                if not value:
                    raise ScriptError(f"line {n}: @{name} needs a value")
                pending[name] = value
            else:
                raise ScriptError(f"line {n}: unknown directive @{name}")
            continue
        m = _PAUSE.match(line)
        if m:
            flush()
            cur.cues.append(Cue(id=f"{cur.id}.{len(cur.cues) + 1:02d}", chapter=cur.id, kind="pause",
                                seconds=float(m.group(1)), show=pending.pop("show", None),
                                motion=pending.pop("motion", None), branch=branch))
            continue
        if line.startswith("[") and line.endswith("]"):
            raise ScriptError(f"line {n}: stage directions must use @directives or [pause N]: {line}")
        buf.append(line)
    flush()
    script = Script(lang=lang, chapters=chapters, source=source)
    validate_structure(script)
    return script


def validate_structure(script: Script) -> None:
    ids = [c.id for c in script.chapters]
    if tuple(ids) != CHAPTER_ORDER:
        raise ScriptError(f"chapters must be, in order: {', '.join(CHAPTER_ORDER)} (found: {', '.join(ids)})")
    for ch in script.chapters:
        if not any(c.kind == "speech" for c in ch.cues):
            raise ScriptError(f"chapter {ch.id} has no spoken lines")
        for c in ch.cues:
            if c.kind == "try" and not (c.text and c.yes and c.no):
                raise ScriptError(f"{c.id}: a @try needs the request text, @yes and @no")
    inter = [c for c in script.chapters if c.interactive]
    if [c.id for c in inter] != ["invitation"]:
        raise ScriptError("exactly the 'invitation' chapter must be @interactive")
    if not any(c.needs_pose_request for c in script.chapter("invitation").cues):
        raise ScriptError("the invitation must contain {pose_request}")
    present = script.chapter("present")
    for outcome in ("together", "alone"):
        if not present.cues_for(outcome):
            raise ScriptError(f"chapter 'present' has no lines for the '{outcome}' ending")


def script_path(data_dir: Path, lang: str) -> Path:
    return Path(data_dir) / f"script.{lang}.md"


def load_script(data_dir: Path, lang: str = "en") -> Script:
    path = script_path(data_dir, lang)
    if not path.exists():
        fallback = script_path(data_dir, "en")
        if lang != "en" and fallback.exists():
            path, lang = fallback, "en"
        else:
            raise FileNotFoundError(path)
    return parse_script(path.read_text(encoding="utf-8"), lang=lang, source=path)
