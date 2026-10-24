"""`itu show …` (performance) and `itu voice audition`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)



def audition_text(script) -> str:
    """A sample from the script itself: shy opening, meme confusion, intimate ending."""
    intro = [c.text for c in script.chapter("last_dog").cues if c.kind == "speech"][:3]
    meme = [c.text for c in script.chapter("internet").cues if c.kind == "speech"][2:5]
    end = [c.text for c in script.chapter("present").cues_for("together") if c.kind == "speech"][-1:]
    return "\n".join(intro + meme + end)
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")


def _parts(s, lang: str):
    from ..bot.app import build_library
    from .catalog import PerformanceCatalog
    from .lines import Lines
    from .script import load_script

    script = load_script(Path(s.show_data_dir), lang)
    lib = build_library(s)
    catalog = PerformanceCatalog(Path(s.show_data_dir), lib.art, lib.memes)
    lines = Lines.load(Path(s.show_data_dir), script.lang)
    return script, catalog, lines


# ---------------------------------------------------------------------- validate / catalog
def cmd_validate(args) -> int:
    s = get_settings()
    script, catalog, lines = _parts(s, args.lang)
    problems = lines.validate() + catalog.validate(script, pose_lines=lines.keys("pose"))
    speech = script.speech_cues()
    print(f"script.{script.lang}.md: {len(script.chapters)} chapters, {len(speech)} spoken cues, "
          f"fingerprint {script.fingerprint()}")
    for ch in script.chapters:
        print(f"  {ch.id:<12} {ch.title:<26} {sum(c.kind == 'speech' for c in ch.cues):>3} cues"
              + ("  [interactive]" if ch.interactive else ""))
    print(f"catalog: {len(catalog.items)} artifacts; interactive: "
          + ", ".join(f"{a.id} ({a.interactive.posture}, {a.interactive.pose_subject})" for a in catalog.interactive()))
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  - " + p)
        return 1
    print("OK")
    return 0


def cmd_catalog(args) -> None:
    s = get_settings()
    if args.action == "build":
        from .catalog import build_records

        for line in build_records(Path(s.show_data_dir), refresh=args.force):
            print(line)
        return
    script, catalog, _ = _parts(s, "en")
    for ch in script.chapters:
        items = catalog.for_chapter(ch.id)
        if not items:
            continue
        print(f"\n{ch.title}")
        for a in items:
            mark = " ★" if a.interactive else ""
            print(f"  {a.id:<22} {a.title[:60]} — {a.artist}, {a.date} [{a.rights_status}]{mark}")
    for k, why in catalog.missing.items():
        print(f"  ! {k}: {why}")


# ---------------------------------------------------------------------- voice cache
def _show_voice(s, lang: str):
    from ..speech.synthesizer import make_synthesizer
    from .voice import ShowVoice, Speaker

    return ShowVoice(make_synthesizer(s, purpose="narration"), make_synthesizer(s, purpose="lines"),
                     Speaker(enabled=False), lang=lang)


def cmd_voice(args) -> int:
    s = get_settings()
    from .voice import all_utterances, cache_status, prune

    script, catalog, lines = _parts(s, args.lang)
    voice = _show_voice(s, script.lang)
    items = cache_status(all_utterances(script, catalog, lines), voice)
    if args.action == "status":
        missing = [i for i in items if not i.cached]
        print(f"narration model {s.openai_narration_model}, voice {s.primary_voice}, language {script.lang}")
        print(f"{len(items) - len(missing)}/{len(items)} clips cached")
        for i in missing[: args.limit]:
            print(f"  missing  {i.cue_id:<22} {i.text.splitlines()[0][:60]}")
        return 0
    if args.action == "prune":
        print(f"removed {prune(items, voice)} unused clip(s)")
        return 0
    if not s.voice_configured:
        print("OPENAI_API_KEY is not set (or OPENAI_VOICE_PROVIDER is not 'openai'): cannot generate audio.")
        return 2
    targets = items
    if args.action == "regenerate":
        if not args.cues:
            print("give one or more cue ids (see `itu show voice status`; e.g. paintings.07 or pose.got_it.1)")
            return 2
        targets = [i for i in items if i.cue_id in args.cues or i.cue_id.split("~")[0] in args.cues]
        for i in targets:
            if i.cached:
                i.cached.unlink(missing_ok=True)
                i.cached.with_suffix(".ogg").unlink(missing_ok=True)
                i.cached = None
    todo = [i for i in targets if not i.cached]
    print(f"generating {len(todo)} clip(s) with {s.openai_narration_model} / {s.primary_voice} …")
    failed = 0
    for n, i in enumerate(todo, 1):
        synth = voice._synth(i.kind)
        res = synth.synthesize(i.text, script.lang, i.direction)
        ok = res is not None
        failed += not ok
        print(f"  [{n}/{len(todo)}] {'ok  ' if ok else 'FAIL'} {i.cue_id:<22} {i.text.splitlines()[0][:50]}")
        if not getattr(synth, "available", True):
            print("voice disabled (credentials rejected) — stopping")
            return 1
    print("done" + (f" — {failed} failed (they will show as text, or retry later)" if failed else ""))
    return 1 if failed else 0


def cmd_audition(args) -> int:
    """Same sample in several voices, so the team can choose ONE voice for the whole show."""
    s = get_settings()
    if not s.voice_configured:
        print("Set OPENAI_API_KEY first.")
        return 2
    from ..speech.synthesizer import AudioCache, OpenAIRealtimeSynthesizer
    from .voice import Speaker

    voices = [v.strip() for v in (args.voices or ",".join(REALTIME_VOICES)).split(",") if v.strip()]
    out = Path(s.audio_cache_dir) / "audition"
    speaker = Speaker(enabled=not args.no_play)
    from .script import load_script

    text = args.text or audition_text(load_script(Path(s.show_data_dir), args.lang))
    print(f"model {args.model or s.openai_narration_model}; voices usable for BOTH narration (Realtime) and GPT-Live: "
          + ", ".join(REALTIME_VOICES))
    for v in voices:
        if v not in REALTIME_VOICES:
            print(f"  {v}: not a Realtime voice — skipped")
            continue
        synth = OpenAIRealtimeSynthesizer(s.openai_api_key, args.model or s.openai_narration_model, v,
                                          AudioCache(out / v), voice_notes=False)
        res = synth.synthesize(text, args.lang, "shy curiosity; soft, young in spirit but not childish")
        if res is None:
            print(f"  {v}: failed")
            continue
        print(f"  {v}: {res.audio_path}")
        if speaker.available:
            asyncio.run(speaker.play(res.audio_path))
    print("Set the chosen one as OPENAI_VOICE in .env (used by narration, free-play lines and conversation).")
    return 0


def cmd_purge(args) -> None:
    from .delivery import PhotoStore

    s = get_settings()
    n = PhotoStore(Path(s.show_output_dir)).purge_all()
    m = PhotoStore(Path("./data/runtime/rehearsal")).purge_all()
    print(f"deleted {n + m} performance session folder(s) (participant photos and compositions)")


# ---------------------------------------------------------------------- run
def cmd_show(args) -> int:
    action = getattr(args, "show_action", None)
    if action == "validate":
        return cmd_validate(args)
    if action == "catalog":
        return cmd_catalog(args)
    if action == "voice":
        return cmd_voice(args)
    if action == "purge":
        return cmd_purge(args)

    s = get_settings()
    from .runtime import ShowOptions

    if args.motion and not args.i_have_physical_estop:
        sys.exit("Refusing motion: pass --i-have-physical-estop and keep the remote / e-stop in your hand.")
    source = args.source
    if source is None and args.dry_run:
        source = "synthetic:sitting"
    opts = ShowOptions(lang=args.lang, dry_run=args.dry_run, mode="automatic" if args.auto else args.mode,
                       display=args.display, source=source, voice=not args.no_voice,
                       fast=args.fast if args.fast else 1.0, motion=args.motion,
                       out_dir=Path(args.out) if args.out else (Path("./data/runtime/rehearsal") if args.dry_run else None),
                       seed=args.seed)
    if args.telegram:
        if args.dry_run:
            sys.exit("--telegram cannot be combined with --dry-run (rehearsals never contact Telegram).")
        from ..bot.app import run_bot

        asyncio.run(run_bot(s, show_options=opts))
        return 0
    return asyncio.run(_run_local(s, opts, args))


async def _run_local(s, opts, args) -> int:
    from .runtime import HELP, ParticipantScript, build_director, keyboard_or_cmd, start_keyboard

    guard = planner = robot = None
    if opts.motion:
        from ..bot.app import build_planner, build_robot
        from ..motion.guard import MotionGuard

        if not (s.show_motion_enabled and s.real_robot_possible):
            sys.exit("Motion needs SHOW_MOTION_ENABLED=true and the real-robot gates "
                     "(APP_MODE=installation, ROBOT_MODEL=go2|zhaw_bridge, ROBOT_MOTION_ENABLED=true).")
        robot = build_robot(s)
        guard = MotionGuard(robot, hardware_gates_open=True, cooldown_s=s.motion_cooldown_seconds,
                            timeout_s=s.motion_timeout_seconds, min_battery=s.min_battery_percent)
        planner = build_planner(s, robot)
        ok, why = guard.arm(operator_id=0)
        print(("ARMED for show cues: " if ok else "NOT armed: ") + why)
    director = build_director(s, opts, guard=guard, planner=planner)
    sim = None
    if args.auto:
        sim = ParticipantScript(takes_part=args.participant != "none", consent=args.consent == "yes",
                                send=args.send == "yes", delay_s=max(0.05, 1.0 * opts.fast))
        sim.attach(director)
    if not args.auto:
        print(HELP)
        start_keyboard(director, asyncio.get_running_loop())
    print(f"{'REHEARSAL (dry run) — ' if opts.dry_run else ''}mode {director.mode}; "
          f"voice {'cached/generated' if opts.voice else 'off (text only)'}; "
          f"camera {opts.source or s.show_camera_source or s.camera_source}; robot motion "
          f"{'ON (armed cues only)' if director.motion.enabled else 'off'}")
    web = getattr(director.stage, "web", None)
    if web is not None:
        loop = asyncio.get_running_loop()
        web.enable_controls(lambda cmd: keyboard_or_cmd(director, cmd), loop)
        print(f"Operator page (buttons): {web.control_url}")
    start_at = "invitation" if args.interactive else args.start_at
    try:
        result = await director.run(start_at=start_at)
    finally:
        director.stage.close()
        if guard is not None:
            guard.software_stop("show ended")
            guard.shutdown()
        if robot is not None:
            robot.close()
    print("\n── result ──")
    print(f"session {result.session_id}: {result.final_state.value}; ending '{result.outcome}'; "
          f"picture {result.artifact or '—'}")
    if result.composition:
        print(f"composition: {result.composition}  (deleted after {s.show_photo_retention_minutes} min "
              "or with `itu show purge`)")
    if result.delivery_detail:
        print(f"delivery: {result.delivery_detail}")
    if director.motion.log:
        print("motion cues: " + "; ".join(director.motion.log))
    if sim:
        print("simulated participant answered: " + ", ".join(sim.answered))
    return 0 if result.final_state.value in ("finished", "cancelled") else 1


def register(sub) -> None:
    sh = sub.add_parser("show", help="the performance: seven chapters, an invitation, a picture made together")
    sh.add_argument("--lang", default="en")
    sh.add_argument("--dry-run", action="store_true",
                    help="rehearsal: no Telegram, no robot motion, no new voice generation (cached clips still play)")
    sh.add_argument("--mode", choices=["guided", "automatic"], help="default SHOW_MODE (guided)")
    sh.add_argument("--auto", action="store_true",
                    help="automatic mode + a simulated participant (for unattended rehearsals and CI)")
    sh.add_argument("--participant", choices=["takes_part", "none"], default="takes_part")
    sh.add_argument("--consent", choices=["yes", "no"], default="yes", help="simulated photo consent (--auto)")
    sh.add_argument("--send", choices=["yes", "no"], default="yes", help="simulated send confirmation (--auto)")
    sh.add_argument("--source", help="camera: webcam[:i] | unitree | zhaw_bridge | image:<path> | "
                                     "synthetic:<sitting|standing> | none (default SHOW_CAMERA_SOURCE/CAMERA_SOURCE)")
    sh.add_argument("--display", choices=["terminal", "web"], help="default SHOW_DISPLAY")
    sh.add_argument("--no-voice", action="store_true", help="text only (subtitles)")
    sh.add_argument("--fast", type=float, default=None, help="time scale for rehearsal, e.g. 0.05")
    sh.add_argument("--out", help="where rehearsal pictures go (default data/runtime/rehearsal)")
    sh.add_argument("--seed", type=int)
    sh.add_argument("--interactive", action="store_true",
                    help="skip the history chapters: start at the invitation (pose, photo, ending)")
    sh.add_argument("--start-at", choices=["last_dog", "prehistory", "middle_ages", "paintings", "photography",
                                           "internet", "invitation", "present"],
                    help="begin with this chapter (rehearsing one part)")
    sh.add_argument("--telegram", action="store_true", help="also run the Telegram bot in this process (/show …)")
    sh.add_argument("--motion", action="store_true", help="allow the script's robot cues (needs all gates + e-stop)")
    sh.add_argument("--i-have-physical-estop", action="store_true")
    sh.set_defaults(fn=cmd_show, show_action=None)
    ssub = sh.add_subparsers(dest="show_action")
    v = ssub.add_parser("validate", help="check script, lines and catalog")
    v.add_argument("--lang", default="en")
    c = ssub.add_parser("catalog", help="build (fetch verified museum records + images) or list")
    c.add_argument("action", choices=["build", "list"])
    c.add_argument("--force", action="store_true")
    vo = ssub.add_parser("voice", help="narration audio cache: status | pregenerate | regenerate CUE… | prune")
    vo.add_argument("action", choices=["status", "pregenerate", "regenerate", "prune"])
    vo.add_argument("cues", nargs="*")
    vo.add_argument("--lang", default="en")
    vo.add_argument("--limit", type=int, default=40)
    ssub.add_parser("purge", help="delete all participant photos and compositions now")

    va = sub.add_parser("voice", help="voice tools")
    vsub = va.add_subparsers(dest="voice_action", required=True)
    au = vsub.add_parser("audition", help="play the same sample in several OpenAI voices")
    au.add_argument("--voices", help=f"comma-separated (default: {','.join(REALTIME_VOICES)})")
    au.add_argument("--text")
    au.add_argument("--model", help="default OPENAI_NARRATION_MODEL")
    au.add_argument("--lang", default="en")
    au.add_argument("--no-play", action="store_true")
    au.set_defaults(fn=cmd_audition)
