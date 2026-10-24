"""Short runtime lines (`data/performance/lines.<lang>.yaml`), separate from the narration."""

from __future__ import annotations

import random
from pathlib import Path

import yaml

REQUIRED = {
    "pose": ("waiting", "not_visible", "partly_visible", "too_close", "move_left", "move_right", "sit", "stand",
             "tilt_head", "got_it", "ready_prompt", "manual"),
    "consent": ("ask", "explain", "thanks", "declined", "capture", "failed"),
    "compare": ("shared", "differ", "okay", "understand"),
    "delivery": ("ask_send", "sent", "not_sent", "failed", "caption"),
    "decline": ("ok",),
    "conversation": ("opening", "closing"),
}


class Lines:
    def __init__(self, data: dict, lang: str = "en", rng: random.Random | None = None):
        self.data = data
        self.lang = lang
        self.rng = rng or random.Random()

    @classmethod
    def load(cls, data_dir: Path, lang: str = "en", rng: random.Random | None = None) -> "Lines":
        path = Path(data_dir) / f"lines.{lang}.yaml"
        if not path.exists():
            path, lang = Path(data_dir) / "lines.en.yaml", "en"
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, lang, rng)

    def variants(self, section: str, key: str) -> list[str]:
        v = (self.data.get(section) or {}).get(key)
        if v is None:
            raise KeyError(f"{section}.{key}")
        return [v] if isinstance(v, str) else list(v)

    def pick(self, section: str, key: str) -> str:
        return self.rng.choice(self.variants(section, key))

    def text(self, section: str, key: str) -> str:
        return self.variants(section, key)[0]

    def keys(self, section: str) -> set[str]:
        return set((self.data.get(section) or {}).keys())

    def fixed_lines(self) -> list[tuple[str, str]]:
        """(id, text) for every spoken variant — used to pre-generate audio."""
        out = []
        for section, entries in self.data.items():
            for key, v in (entries or {}).items():
                if section == "delivery" and key == "caption":
                    continue  # written, not spoken
                for i, text in enumerate([v] if isinstance(v, str) else v):
                    out.append((f"{section}.{key}.{i + 1}", text))
        return out

    def validate(self) -> list[str]:
        problems = []
        for section, keys in REQUIRED.items():
            for k in keys:
                try:
                    if not all(t.strip() for t in self.variants(section, k)):
                        problems.append(f"lines: {section}.{k} has an empty variant")
                except KeyError:
                    problems.append(f"lines: missing {section}.{k}")
        return problems
