"""Export the presentation for the static web version (web/, deployed on Vercel).

Python stays the single source of truth: the script, the catalog (with each picture's person/dog
placement), the runtime lines and the sample photos are written to web/data/show.json, the pictures
are copied, every spoken line is pre-generated once with the configured voice (MP3), and the sample
photos' poses are read here with the same MediaPipe pipeline — so the browser needs no API keys.

    cd python && .venv/bin/python scripts/export_web.py   # everything (voice: cached clips are reused)
    cd python && .venv/bin/python scripts/export_web.py --no-audio  # data + pictures only (subtitles, no voice)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web"  # the site lives at the repo root; this is python/
SPOKEN = ("round", "rescue")  # runtime line sections the web show says
# the internet round's memes, online: free Unsplash photos of similar dogs (web/memes/<slug>.jpg) — photographer
MEME_STANDINS = {"meme:doge": "mpetrucho", "meme:cheems": "kobi_kadosh", "meme:long-nose-borzoi": "karolinawv",
                 "meme:side-eye-dog": "evgeny_karp97", "meme:this-is-fine": "ciabattespugnose"}


class WebAudioExporter:
    """Provider/voice/settings-aware exports; never reuse the old text-only MP3s."""

    def __init__(self, settings, web: Path = WEB):
        from is_this_us.speech.synthesizer import make_synthesizer

        if not settings.elevenlabs_voice_id:
            raise ValueError("Set ELEVENLABS_VOICE_ID for the browser presentation")
        settings = settings.model_copy(update={"voice_provider": "elevenlabs"})
        self.synth = {k: make_synthesizer(settings, purpose=k) for k in ("narration", "lines")}
        self.web = web
        self.directory = web / "audio" / "elevenlabs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity = {"provider": "elevenlabs", "voice_id": settings.elevenlabs_voice_id,
                         "model_id": settings.elevenlabs_model_id}
        self.previous = {}
        manifest = self.directory / "manifest.json"
        if manifest.exists():
            self.previous = json.loads(manifest.read_text()).get("clips", {})
        self.clips = {}

    def __call__(self, text: str, kind: str, direction: str = "") -> str | None:
        if not text.strip():
            return None
        synth = self.synth[kind]
        from is_this_us.speech.fx import fx_key

        identity = {**self.identity, "key": synth.key_for(text, "en", direction),
                    "fx": fx_key(synth.fx) if synth.fx else "off", "encoding": "mp3-mono-64k-v1"}
        name = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24] + ".mp3"
        out = self.directory / name
        url = f"audio/elevenlabs/{name}"
        previous = self.previous.get(url, {})
        if not (out.exists() and previous.get("identity") == identity
                and previous.get("sha256") == hashlib.sha256(out.read_bytes()).hexdigest()):
            res = synth.synthesize(text, "en", direction)
            if res is None or res.provider != "elevenlabs":
                raise RuntimeError(f"ElevenLabs clip unavailable: {kind}, {len(text)} characters")
            temporary = out.with_suffix(".tmp.mp3")
            try:
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(res.audio_path),
                                "-ac", "1", "-b:a", "64k", str(temporary)], check=True)
                temporary.replace(out)
            finally:
                temporary.unlink(missing_ok=True)
        self.clips[url] = {"identity": identity, "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                           "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "purpose": kind}
        print(f"  ElevenLabs: {kind} ({len(text)} characters)", flush=True)
        return url

    def finish(self) -> dict:
        manifest = self.directory / "manifest.json"
        temporary = manifest.with_suffix(".tmp.json")
        temporary.write_text(json.dumps({**self.identity, "clips": self.clips}, indent=2) + "\n")
        temporary.replace(manifest)
        return {**self.identity, "manifest": "audio/elevenlabs/manifest.json"}


def refresh_audio(show: dict, script, audio: WebAudioExporter) -> None:
    directions = {cue.id: chapter.voice for chapter in script.chapters for cue in chapter.cues}
    for chapter in show["chapters"]:
        for cue in chapter["cues"]:
            cue["audio"] = (audio(cue["text"], "narration", directions[cue["id"]])
                            if cue["kind"] != "pause" else None)
    for section in show["lines"].values():
        for variants in section.values():
            for variant in variants:
                variant["audio"] = audio(variant["text"], "lines")
    show["voice"] = audio.finish()


def painting_message_fields(artifact) -> dict[str, str]:
    """The browser preview uses the same museum attribution as Telegram.

    Meme stand-ins have different provenance, so never attach an original
    meme's credits to their exported photographs.
    """
    if artifact.kind != "art":
        return {}
    from is_this_us.performance.encounter import attribution

    return {"attribution": attribution(artifact), "page_url": artifact.page_url,
            "rights_statement": artifact.rights_statement}


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--no-audio", action="store_true")
    mode.add_argument("--audio-only", action="store_true", help="refresh ElevenLabs clips without changing images/poses")
    args = ap.parse_args()

    from is_this_us.bot.app import build_estimator, build_library
    from is_this_us.config import Settings
    from is_this_us.performance.catalog import PerformanceCatalog
    from is_this_us.performance.lines import Lines
    from is_this_us.performance.pose_session import PoseReader
    from is_this_us.performance.rounds import behavior_for, candidates, facing_for, human_pose, side_for
    from is_this_us.performance.runtime import SAMPLE_ORDER, STANDIN_DIR
    from is_this_us.performance.script import ROUND_ERAS, load_script
    from is_this_us.performance.sim_robot import SWAP_LR
    env = ROOT / ".env" if (ROOT / ".env").exists() else ROOT.parent / ".env"
    s = Settings(_env_file=env)
    if not s.audio_cache_dir.is_absolute():
        s = s.model_copy(update={"audio_cache_dir": env.parent / s.audio_cache_dir})
    data_dir = ROOT / s.show_data_dir
    script = load_script(data_dir, "en", "presentation")
    audio = (lambda *args: None) if args.no_audio else WebAudioExporter(s)
    if args.audio_only:
        path = WEB / "data" / "show.json"
        show = json.loads(path.read_text())
        refresh_audio(show, script, audio)
        temporary = path.with_suffix(".tmp.json")
        temporary.write_text(json.dumps(show, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)
        print(f"Exported {len(audio.clips)} verified ElevenLabs clips", flush=True)
        return
    lib = build_library(s)
    art_repo, meme_repo = lib.art, lib.memes
    catalog = PerformanceCatalog(data_dir, art_repo, meme_repo)
    lines = Lines.load(data_dir, "en")
    for d in ("img", "audio", "samples", "data"):
        (WEB / d).mkdir(parents=True, exist_ok=True)

    def picture(a) -> str | None:
        if a.kind == "meme":  # never the presentation-use originals online: a free Unsplash stand-in instead
            slug = a.id.split(":", 1)[1]
            return f"memes/{slug}.jpg" if (WEB / "memes" / f"{slug}.jpg").exists() else None
        if not a.displayable:
            return None
        name = a.id.replace(":", "_") + ".jpg"
        im = Image.open(a.image_path).convert("RGB")
        im.thumbnail((1400, 1400))
        im.save(WEB / "img" / name, quality=85)
        return f"img/{name}"

    # --- pictures
    art = {}
    for a in catalog.items.values():
        img = picture(a)
        if img is None:
            continue
        meme = a.kind == "meme"
        art[a.id] = {"kind": a.kind, "title": a.title,
                     "artist": f"stand-in photo by @{MEME_STANDINS[a.id]}" if meme else a.artist, "date": a.date,
                     "museum": "Unsplash" if meme else a.museum,
                     "rights": "Unsplash License — not the original meme" if meme else a.rights_status, "image": img, "chapter": a.chapter, "subject": a.subject,
                     "human": human_pose(a, catalog.art_repo), "dog": behavior_for(a, catalog.art_repo),
                     "side": side_for(a, catalog.art_repo), "facing": facing_for(a),
                     **painting_message_fields(a)}
    eras = {era: [a.id for a in candidates(catalog, era)] for era in ROUND_ERAS}
    memes = [a.id for a in catalog.for_chapter("internet") if a.kind == "meme" and a.id in art]

    # --- the script (presentation programme)
    chapters = []
    for ch in script.chapters:
        cues = []
        for c in ch.cues:
            text = c.render(where="on the screen", pose_request="")
            cues.append({"id": c.id, "kind": c.kind, "text": text, "show": c.show, "seconds": c.seconds,
                         "target": c.target, "branch": c.branch,
                         "audio": audio(text, "narration", ch.voice) if c.kind != "pause" else None})
        chapters.append({"id": ch.id, "title": ch.title, "cues": cues})
        print(f"  {ch.id}: {len(cues)} cues")

    said = {sec: {key: [{"text": t, "audio": audio(t, "lines")} for t in lines.variants(sec, key)]
                  for key in (lines.data.get(sec) or {})} for sec in SPOKEN}

    # --- sample photos, their pose read here (posture + a 3-D skeleton for the avatar)
    reader = PoseReader(build_estimator(s))
    samples = []
    for name in SAMPLE_ORDER:
        path = STANDIN_DIR / f"{name}.jpg"
        if not path.exists():
            continue
        im = Image.open(path).convert("RGB")
        im.thumbnail((1000, 1000))
        im.save(WEB / "samples" / f"{name}.jpg", quality=85)
        r = reader.read(np.asarray(im), "photo")
        lm = r.landmarks
        skel = None
        if lm is not None and lm.world is not None:  # same convention as SimRobot.skeleton (not mirrored)
            p = lm.world * [1, -1, -1]
            skel = [[round(float(x), 3), round(float(y), 3), round(float(z), 3), round(float(v), 2)]
                    for (x, y, z), v in zip(p, lm.visibility)]
        xy = None if lm is None else [[round(float(x) / im.width, 4), round(float(y) / im.height, 4),
                                       round(float(v), 2)] for (x, y), v in zip(lm.xy, lm.visibility)]
        samples.append({"name": name, "image": f"samples/{name}.jpg", "posture": r.posture,
                        "skeleton": skel, "points": xy})
        print(f"  sample {name}: {r.posture}")

    show = {"chapters": chapters, "art": art, "eras": eras, "memes": memes, "lines": said,
            "telegram_caption": lines.captions(),
            "meme_phrases": lines.data.get("meme_phrases") or {}, "samples": samples, "swap_lr": SWAP_LR,
            "credits": (STANDIN_DIR / "README.md").read_text(encoding="utf-8")}
    show["voice"] = {"provider": "none"} if args.no_audio else audio.finish()
    (WEB / "data" / "show.json").write_text(json.dumps(show, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy(ROOT / "src" / "is_this_us" / "performance" / "sim3d.js", WEB / "sim3d.js")
    print(f"web/data/show.json: {len(art)} pictures, {sum(len(c['cues']) for c in chapters)} cues, "
          f"{len(samples)} samples, {len(list((WEB / 'audio').glob('*.mp3')))} voice clips")


if __name__ == "__main__":
    main()
