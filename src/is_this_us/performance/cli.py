"""`itu show …` (performance) and `itu voice audition`."""

from __future__ import annotations

import asyncio
import logging
import re
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


def _voice_language(s, lang: str) -> str:
    if s.voice_provider == "elevenlabs" and lang != "en":
        log.info("ElevenLabs presentation speech uses English; ignoring requested language")
        return "en"
    return lang


def _voice_description(s) -> str:
    if s.voice_provider == "none":
        return "provider none (speech disabled)"
    model = s.elevenlabs_model_id if s.voice_provider == "elevenlabs" else s.openai_narration_model
    voice = s.elevenlabs_voice_id if s.voice_provider == "elevenlabs" else s.character_voice
    return f"provider {s.voice_provider}, model {model}, voice {voice or '(not configured)'} (preset {s.voice_preset})"


def _voice_setup_message(s) -> str:
    if s.voice_provider == "elevenlabs":
        return "Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID to generate audio."
    if s.voice_provider == "none":
        return "VOICE_PROVIDER=none: speech and generation are disabled."
    return "Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT to generate Azure audio."


def _parts(s, lang: str, programme: str | None = None):
    from ..bot.app import build_library
    from .catalog import PerformanceCatalog
    from .lines import Lines
    from .script import load_script

    script = load_script(Path(s.show_data_dir), _voice_language(s, lang), programme or s.show_programme)
    lib = build_library(s)
    catalog = PerformanceCatalog(Path(s.show_data_dir), lib.art, lib.memes)
    lines = Lines.load(Path(s.show_data_dir), script.lang)
    return script, catalog, lines


# ---------------------------------------------------------------------- validate / catalog
def _print_rounds(script, catalog) -> None:
    from .rounds import behavior_for, candidates, human_pose, side_for

    print(f"presentation.{script.lang}.md: " + " → ".join(c.id for c in script.chapters))
    for era in ("prehistory", "middle_ages", "paintings"):
        cands = candidates(catalog, era)
        poses = sorted({human_pose(a, catalog.art_repo) or "?" for a in cands})
        print(f"  round {era:<12} {len(cands):>2} pictures; person poses: {', '.join(poses)}")
        for a in cands:
            print(f"      {a.id:<14} person {human_pose(a, catalog.art_repo) or '?':<9} robot "
                  f"{behavior_for(a, catalog.art_repo):<9} {side_for(a, catalog.art_repo):<6} {a.title[:48]}")
    print("  round internet     the robot makes its own meme from the consented photo")


def cmd_validate(args) -> int:
    s = get_settings()
    problems_all = []
    for programme in ("presentation", "show"):
        script, catalog, lines = _parts(s, args.lang, programme)
        problems_all += [f"[{programme}] {p}" for p in lines.validate() + catalog.validate(script, pose_lines=lines.keys("pose"))]
        if programme == "presentation":
            _print_rounds(script, catalog)
    script, catalog, lines = _parts(s, args.lang, "show")
    problems = sorted(set(problems_all))
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
                     Speaker(enabled=False), lang=_voice_language(s, lang))


def cmd_voice(args) -> int:
    s = get_settings()
    from .voice import all_utterances, cache_status, prune

    if args.action in {"pregenerate", "regenerate"} and not s.voice_cache_enabled:
        print("VOICE_CACHE_ENABLED=false: pregeneration would be discarded when this command exits. "
              "Set VOICE_CACHE_ENABLED=true before generating presentation clips.")
        return 2
    script, catalog, lines = _parts(s, args.lang, "show")
    other, _, _ = _parts(s, args.lang, "presentation")
    voice = _show_voice(s, script.lang)
    # both programmes share one cache: status/pregenerate/prune cover the presentation AND the narrated show
    utterances = all_utterances(other, catalog, lines) + all_utterances(script, catalog, lines)
    seen = set()
    utterances = [u for u in utterances if not ((u.kind, u.text, u.direction) in seen
                                                or seen.add((u.kind, u.text, u.direction)))]
    items = cache_status(utterances, voice)
    if args.action == "status":
        missing = [i for i in items if not i.cached]
        print(f"{_voice_description(s)}, language {script.lang}")
        print(f"{len(items) - len(missing)}/{len(items)} clips cached")
        print(f"missing {len(missing)} clip(s), {sum(len(i.text) for i in missing)} text characters")
        for i in missing[: args.limit]:
            print(f"  missing  {i.cue_id:<22} {i.text.splitlines()[0][:60]}")
        return 0
    if args.action == "prune":
        print(f"removed {prune(items, voice)} unused clip(s)")
        return 0
    if not s.voice_configured:
        print(_voice_setup_message(s))
        return 2
    targets = items
    if args.action == "regenerate":
        if not args.cues:
            print("give one or more cue ids (see `itu show voice status`; e.g. paintings.07 or pose.got_it.1)")
            return 2
        targets = [i for i in items if i.cue_id in args.cues or i.cue_id.split("~")[0] in args.cues]
        if not targets:
            print("No matching cue ids; see `itu show voice status`.")
            return 2
        for i in targets:
            synth = voice._synth(i.kind)
            getattr(synth, "forget", lambda *a: None)(i.text, script.lang, i.direction)  # raw take + processed
            if i.cached:
                i.cached.unlink(missing_ok=True)
                i.cached.with_suffix(".ogg").unlink(missing_ok=True)
                i.cached = None
    todo = [i for i in targets if not i.cached]
    print(f"generating {len(todo)} clip(s), {sum(len(i.text) for i in todo)} text characters; "
          f"{_voice_description(s)} …")
    failed = 0
    for n, i in enumerate(todo, 1):
        synth = voice._synth(i.kind)
        res = synth.synthesize(i.text, script.lang, i.direction)
        ok = res is not None
        failed += not ok
        print(f"  [{n}/{len(todo)}] {'ok  ' if ok else 'FAIL'} {i.cue_id:<22} {i.text.splitlines()[0][:50]}")
        if not getattr(synth, "available", True):
            print("voice generation unavailable (check provider credentials/quota) — stopping")
            return 1
    print("done" + (f" — {failed} failed (they will show as text, or retry later)" if failed else ""))
    return 1 if failed else 0


def cmd_audition(args) -> int:
    """Same sample in several voices, so the team can choose ONE voice for the whole show."""
    s = get_settings()
    if s.voice_provider == "elevenlabs" and args.voices:
        s = s.model_copy(update={"elevenlabs_voice_id": args.voices.split(",")[0].strip()})
    if args.model:
        field = "elevenlabs_model_id" if s.voice_provider == "elevenlabs" else "openai_narration_model"
        s = s.model_copy(update={field: args.model})
    if not s.voice_configured:
        print(_voice_setup_message(s))
        return 2
    from ..speech.synthesizer import AudioCache, OpenAIRealtimeSynthesizer, make_synthesizer
    from .voice import Speaker

    default_voices = s.elevenlabs_voice_id if s.voice_provider == "elevenlabs" else ",".join(REALTIME_VOICES)
    voices = [v.strip() for v in (args.voices or default_voices).split(",") if v.strip()]
    out = Path(s.audio_cache_dir) / "audition"
    speaker = Speaker(enabled=not args.no_play)
    from .script import load_script

    lang = _voice_language(s, args.lang)
    text = args.text or audition_text(load_script(Path(s.show_data_dir), lang, "show"))
    print(f"{_voice_description(s)}; language {lang}; {len(voices)} audition(s), {len(text) * len(voices)} text characters")
    failed = 0
    for v in voices:
        if s.voice_provider == "elevenlabs":
            if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
                print("Invalid ElevenLabs voice ID — skipped")
                failed += 1
                continue
            selection = s.model_copy(update={"elevenlabs_voice_id": v,
                                             "elevenlabs_model_id": args.model or s.elevenlabs_model_id,
                                             "audio_cache_dir": out, "voice_preset": "off"})
            synth = make_synthesizer(selection, purpose="narration")
        elif v not in REALTIME_VOICES:
            print(f"  {v}: not a Realtime voice — skipped")
            failed += 1
            continue
        else:
            synth = OpenAIRealtimeSynthesizer(s.azure_openai_api_key, args.model or s.openai_narration_model, v,
                                              AudioCache(out / v), voice_notes=False)
        res = synth.synthesize(text, lang, "" if s.voice_provider == "elevenlabs" else
                               "shy curiosity; soft, young in spirit but not childish")
        if res is None:
            print(f"  {v}: failed")
            failed += 1
            if not synth.available:
                break
            continue
        print(f"  {v}: {res.audio_path}")
        if speaker.available:
            asyncio.run(speaker.play(res.audio_path))
    setting = "ELEVENLABS_VOICE_ID" if s.voice_provider == "elevenlabs" else "OPENAI_VOICE"
    print(f"Set the chosen voice as {setting} in .env.")
    return 1 if failed else 0


def cmd_purge(args) -> None:
    from .delivery import PhotoStore

    s = get_settings()
    n = PhotoStore(Path(s.show_output_dir)).purge_all()
    m = PhotoStore(Path("./data/runtime/rehearsal")).purge_all()
    print(f"deleted {n + m} performance session folder(s) (participant photos and compositions)")


# ---------------------------------------------------------------------- walking
def cmd_walk_test(args) -> int:
    s = get_settings()
    from .runtime import approach_settings, simulated_approacher

    if args.sim:
        a = simulated_approacher(s, fast=0.05)
        res = a.approach(side=args.side, posture=args.posture)
        w = a.world
        print(f"{res.status}: {res.detail}; walked {res.travelled_m:.1f} m; now {w.distance():.2f} m from the "
              f"person, on their {w.side_of_person()} (closest {w.min_distance:.2f} m)")
        if not args.no_home:
            print("back home:", a.go_home())
        return 0 if res.arrived else 1
    if not args.i_have_physical_estop:
        sys.exit("Refusing: pass --i-have-physical-estop and keep the remote / e-stop in your hand.")
    from ..bot.app import build_estimator, build_robot
    from ..motion.guard import MotionGuard
    from ..vision.frame_sources import make_frame_source
    from ..vision.pose import select_primary_person
    from .approach import Approacher

    est = build_estimator(s)
    if est is None:
        sys.exit("Pose model missing: pip install -e '.[vision]' && itu setup")
    if args.calibrate:
        cam = make_frame_source(s.show_walk_camera, network_interface=s.robot_network_interface)
        heights = []
        for _ in range(20):
            f = cam.read()
            if f is None:
                continue
            p, _ = select_primary_person(est.estimate(f.image))
            if p is not None:
                x0, y0, x1, y1 = p.bbox()
                h = p.image_size[1]
                if y0 / h > 0.02 and y1 / h < 0.98:
                    heights.append((y1 - y0) / h)
        cam.close()
        if len(heights) < 5:
            sys.exit("Could not see the whole person (head to feet) often enough — move them back a little.")
        heights.sort()
        k = heights[len(heights) // 2] * args.calibrate
        name = "SHOW_WALK_K_STANDING" if args.posture == "standing" else "SHOW_WALK_K_SITTING"
        print(f"{name}={k:.2f}    # put this in .env ({len(heights)} readings)")
        print("When both SHOW_WALK_K_SITTING and SHOW_WALK_K_STANDING are calibrated, also set SHOW_WALK_CALIBRATED=true.")
        return 0
    if not (s.real_robot_possible and s.show_walk_enabled):
        sys.exit("Needs APP_MODE=installation, ROBOT_MODEL=zhaw_bridge, ROBOT_MOTION_ENABLED=true, SHOW_WALK_ENABLED=true.")
    robot = build_robot(s)
    guard = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0, timeout_s=s.motion_timeout_seconds)
    ok, why = guard.arm(operator_id=0)
    print(("ARMED: " if ok else "NOT armed: ") + why)
    if not ok:
        return 1
    from ..domain import MotionPlan, Reproducibility, RobotBehavior

    guard.execute(MotionPlan(requested=RobotBehavior.STAND, behavior=RobotBehavior.STAND,
                             fidelity=Reproducibility.YES, explanation="walk test"))
    a = Approacher(robot, guard, lambda: make_frame_source(s.show_walk_camera,
                                                           network_interface=s.robot_network_interface),
                   est, approach_settings(s))
    input(f"Person in place? Robot will walk to their {args.side}. Remote in hand. Enter to start, Ctrl+C to abort… ")
    try:
        res = a.approach(side=args.side, posture=args.posture)
        print(f"{res.status}: {res.detail}; walked {res.travelled_m:.1f} m; person estimated at {res.person_at}")
        if not args.no_home:
            input("Enter to walk back home… ")
            guard.execute(MotionPlan(requested=RobotBehavior.STAND, behavior=RobotBehavior.STAND,
                                     fidelity=Reproducibility.YES, explanation="walk test home"))
            print("home:", a.go_home())
    finally:
        guard.software_stop("walk test ended")
        guard.shutdown()
        robot.close()
    return 0


# ---------------------------------------------------------------------- run
def capability_lines(s, opts, director) -> list[str]:
    """Poses and walking are SEPARATE capabilities; say exactly which are on and why not."""
    if getattr(opts, "sim_robot", False):
        return ["poses:   SIMULATED — the drawn dog takes them (nothing real moves)",
                "walking: SIMULATED — in a drawn room, with the same approach logic as the real robot"]
    poses = ("ON — supported poses in place (sit, lie down, stand, paw, stretch), armed guard"
             if director.motion.enabled else "off — add --motion --i-have-physical-estop (SHOW_MOTION_ENABLED=true)")
    if director.approacher is not None:
        walk = "ON — " + ("simulated room" if opts.dry_run else "camera-guided approach with the robot's camera")
    else:
        missing = [why for ok, why in (
            (opts.walk, "--walk"), (opts.motion or opts.dry_run, "--motion"),
            (s.show_walk_enabled or opts.dry_run, "SHOW_WALK_ENABLED=true"),
            (s.show_walk_calibrated or opts.dry_run, "calibration (walk-test --calibrate, SHOW_WALK_CALIBRATED=true)"))
            if not ok]
        walk = "off" + (f" — needs {', '.join(missing)}" if missing else "")
    return [f"poses:   {poses}", f"walking: {walk}  (a successful pose never enables walking by itself)"]


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
    if action == "walk-test":
        return cmd_walk_test(args)

    s = get_settings()
    from .runtime import ShowOptions

    if getattr(args, "sim_robot", False) and (args.motion or args.walk):
        sys.exit("--sim-robot replaces the real robot: leave out --motion/--walk (the simulated dog does both).")
    if args.motion and not args.i_have_physical_estop:
        sys.exit("Refusing motion: pass --i-have-physical-estop and keep the remote / e-stop in your hand.")
    if args.walk and not args.dry_run and not args.motion:
        sys.exit("--walk on the real robot needs --motion --i-have-physical-estop (and SHOW_WALK_ENABLED=true).")
    source = args.source
    if source is None and args.dry_run:
        source = "synthetic:sitting"
    opts = ShowOptions(lang=_voice_language(s, args.lang), dry_run=args.dry_run, mode="automatic" if args.auto else args.mode,
                       display=args.display, source=source, voice=not args.no_voice,
                       fast=args.fast if args.fast else 1.0, motion=args.motion,
                       out_dir=Path(args.out) if args.out else (Path("./data/runtime/rehearsal") if args.dry_run else None),
                       seed=args.seed, walk=args.walk, finale=args.finale, programme=args.programme,
                       sim_robot=getattr(args, "sim_robot", False))
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
          f"camera {opts.source or s.show_camera_source or s.camera_source}")
    for line in capability_lines(s, opts, director):
        print("  " + line)
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
    if director.walk_log:
        print("walking: " + "; ".join(director.walk_log))
    if director.encounter_log:
        print("encounter: " + " | ".join(director.encounter_log))
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
                    help="show: start at the invitation; presentation: skip the narration, straight to each round")
    sh.add_argument("--programme", choices=["presentation", "show"],
                    help="presentation (default SHOW_PROGRAMME): intro → a pose round per era → rescue finale; "
                         "show: the long narrated show")
    sh.add_argument("--start-at", choices=["last_dog", "prehistory", "middle_ages", "paintings", "photography",
                                           "internet", "invitation", "present", "rescue"],
                    help="begin with this chapter (rehearsing one part)")
    sh.add_argument("--telegram", action="store_true", help="also run the Telegram bot in this process (/show …)")
    sh.add_argument("--motion", action="store_true", help="allow the script's robot cues (needs all gates + e-stop)")
    sh.add_argument("--finale", choices=["encounter", "imitate"],
                    help="encounter (default): the good boy encounter — any pose → matched picture → Telegram → "
                         "the dog's pose held 10 s; imitate: copy a picture the robot chose")
    sh.add_argument("--sim-robot", action="store_true",
                    help="no robot: a simulated dog takes the poses and walks in a simulated room, drawn on the page")
    sh.add_argument("--walk", action="store_true",
                    help="the robot finds the participant with its camera and walks to their side "
                         "(real robot: needs --motion and SHOW_WALK_ENABLED; --dry-run: simulated room)")
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
    wt = ssub.add_parser("walk-test", help="try the camera-guided walk alone (simulated with --sim)")
    wt.add_argument("--side", choices=["left", "right", "front"], default="left")
    wt.add_argument("--posture", choices=["sitting", "standing"], default="sitting")
    wt.add_argument("--sim", action="store_true", help="simulated room and robot, nothing real moves")
    wt.add_argument("--calibrate", type=float, metavar="METERS",
                    help="no walking: a person stands/sits this far in front of the robot; prints the k value")
    wt.add_argument("--no-home", action="store_true", help="stay there instead of walking back")
    wt.add_argument("--i-have-physical-estop", action="store_true")

    va = sub.add_parser("voice", help="voice tools")
    vsub = va.add_subparsers(dest="voice_action", required=True)
    au = vsub.add_parser("audition", help="play the same sample using the selected voice provider")
    au.add_argument("--voices", help="comma-separated ElevenLabs voice IDs (default: configured ID), "
                    "or Azure voice names (default: all Realtime voices)")
    au.add_argument("--text")
    au.add_argument("--model", help="default ELEVENLABS_MODEL_ID or OPENAI_NARRATION_MODEL")
    au.add_argument("--lang", default="en")
    au.add_argument("--no-play", action="store_true")
    au.set_defaults(fn=cmd_audition)
