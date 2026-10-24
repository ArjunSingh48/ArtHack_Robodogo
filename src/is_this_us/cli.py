"""Command-line entry point: `itu <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import get_settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "aiogram.event", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------- setup
def cmd_setup(args) -> None:
    s = get_settings()
    from .artworks.curation import cache_images, validate
    from .artworks.repository import YamlArtworkRepository
    from .storage import Storage

    if not s.pose_model_path.exists():
        from .vision.pose import download_pose_model

        print(f"Downloading MediaPipe pose model → {s.pose_model_path}")
        download_pose_model(s.pose_model_path)
    repo = YamlArtworkRepository(s.artwork_data_dir)
    problems = validate(repo)
    print(f"Artworks: {len(repo.all())} curated; validation: {'OK' if not problems else problems}")
    for line in cache_images(repo):
        print("  " + line)
    Storage(s.database_path).close()
    print("Database ready:", s.database_path)
    print("Voice:", f"OpenAI Realtime ({s.openai_realtime_model}, voice {s.openai_realtime_voice})"
          if s.voice_configured else "text-only (no OPENAI_API_KEY)")


# ---------------------------------------------------------------------- simulate
def _build_sim(s, seed, *, use_estimator: bool):
    from .bot.app import build_estimator, build_library, build_planner
    from .motion.guard import MotionGuard
    from .notifier import ConsoleNotifier
    from .orchestrator import InteractionOrchestrator
    from .robot.mock import MockRobotAdapter
    from .speech.synthesizer import LocalSpeakerPlayer, make_synthesizer
    from .storage import Storage

    robot = MockRobotAdapter()
    robot.connect()
    guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=s.motion_timeout_seconds)
    storage = Storage(":memory:")
    notifier = ConsoleNotifier()
    orch = InteractionOrchestrator(
        repo=build_library(s),
        estimator=build_estimator(s) if use_estimator else None,
        storage=storage, guard=guard, notifier=notifier, synthesizer=make_synthesizer(s),
        player=LocalSpeakerPlayer(s.local_audio_playback), planner=build_planner(s),
        default_lang=s.default_language, default_archive=s.default_archive, seed=seed,
    )
    return orch, storage, robot


def cmd_simulate(args) -> None:
    """One complete interaction with the mock robot, printed to the console."""
    if args.photographing and (args.image or args.webcam):
        sys.exit("--photographing injects a synthetic event; use it without --image or --webcam")
    s = get_settings()
    if args.lang:
        s.default_language = args.lang
    if args.archive:
        s.default_archive = args.archive
    from .vision.frame_sources import ImageFileSource, WebcamSource

    use_est = bool(args.image or args.webcam)
    orch, storage, robot = _build_sim(s, args.seed, use_estimator=use_est)
    chat_id = 1
    storage.touch_visitor(chat_id, s.default_language)

    async def run():
        if args.image:
            outcome = await orch.observe_installation(chat_id, ImageFileSource(args.image))
        elif args.webcam:
            outcome = await orch.observe_installation(chat_id, WebcamSource(args.camera_index))
        else:
            from .vision.synthetic import synthetic_person

            people = [synthetic_person(args.posture, args.gesture, scale=args.scale)]
            obs = orch.interpreter.interpret(people, source=f"SYNTHETIC:{args.posture}")
            if args.photographing:
                from .domain import InteractionIntent

                obs.intent = InteractionIntent.PHOTOGRAPHING_ROBOT
            outcome = await orch.observe_given(chat_id, obs, origin="installation")
        print("\nDEBUG")
        for line in outcome.debug_lines():
            print("  " + line)
        print(f"  mock robot SDK calls: {robot.calls}  body={robot.body}")
        if args.another and outcome.artwork:
            print("\n[visitor taps ↻ Try another painting]")
            again = await orch.another(chat_id, outcome.interaction_id)
            for line in again.debug_lines():
                print("  " + line)
        if args.card and outcome.artwork:
            from PIL import Image

            from .cards import render_memory_card

            local = orch.repo.local_image(outcome.artwork)  # None for reference-only memes
            data = render_memory_card(Image.open(local) if local else None, outcome.artwork,
                                      caption=outcome.artwork.poem, session_id=outcome.interaction_id)
            Path(args.card).write_bytes(data)
            print(f"\nmemory card → {args.card}")

    try:
        asyncio.run(run())
    finally:
        if orch.estimator is not None:
            orch.estimator.close()


# ---------------------------------------------------------------------- webcam debug display
def cmd_webcam(args) -> None:
    """Live debug window: landmarks + posture; SPACE runs an interaction, Q quits."""
    import cv2

    s = get_settings()
    orch, storage, robot = _build_sim(s, args.seed, use_estimator=True)
    if orch.estimator is None:
        sys.exit("Pose model missing — run `itu setup`.")
    from .vision.frame_sources import WebcamSource

    cam = WebcamSource(args.camera_index)
    storage.touch_visitor(1, s.default_language)
    last = ""
    try:
        while True:
            frame = cam.read()
            if frame is None:
                continue
            people = orch.estimator.estimate(frame.image)
            obs = orch.interpreter.interpret(people, source="webcam")
            bgr = cv2.cvtColor(frame.image, cv2.COLOR_RGB2BGR)
            for p in people:
                for (x, y), v in zip(p.xy, p.visibility):
                    if v > 0.5:
                        cv2.circle(bgr, (int(x), int(y)), 3, (201, 150, 167), -1)
            hud = [f"{obs.posture} {obs.posture_confidence:.2f}  people={obs.people_detected}  {obs.proximity}",
                   ", ".join(f"{g}={v:.2f}" for g, v in obs.gestures.items()), last, "SPACE: interact   Q: quit"]
            for i, t in enumerate(hud):
                cv2.putText(bgr, t, (12, 28 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 50, 70), 2)
            cv2.imshow("Is This Us? — debug", bgr)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord(" "):
                outcome = asyncio.run(orch.observe_given(1, obs, origin="installation"))
                last = (f"{outcome.artwork.title[:40] if outcome.artwork else 'no match'} → "
                        f"{outcome.plan.behavior} ({outcome.result.status})")
                for line in outcome.debug_lines():
                    print("  " + line)
    finally:
        cam.close()
        cv2.destroyAllWindows()


# ---------------------------------------------------------------------- bot
def cmd_bot(args) -> None:
    from .bot.app import run_bot

    asyncio.run(run_bot(get_settings()))


def cmd_qr(args) -> None:
    from .bot.flows import make_qr_png
    from .storage import Storage

    s = get_settings()
    st = Storage(s.database_path, pairing_ttl_min=args.minutes or s.pairing_token_ttl_minutes)
    token = st.create_pairing_token()
    link = f"https://t.me/{s.telegram_bot_username}?start=pair_{token}" if s.telegram_bot_username else token
    Path(args.out).write_bytes(make_qr_png(link))
    print(f"QR → {args.out}\n{link}")


# ---------------------------------------------------------------------- artworks
def cmd_artworks(args) -> None:
    s = get_settings()
    from .artworks import curation
    from .artworks.repository import YamlArtworkRepository

    if args.action == "build":
        built, problems = curation.build(s.artwork_data_dir, offline=args.offline)
        print("Build failed; previous annotations retained" if problems else f"built {len(built)} artworks")
        for p in problems:
            print("  ! " + p)
        if problems:
            sys.exit(1)
    elif args.action == "validate":
        problems = curation.validate(YamlArtworkRepository(s.artwork_data_dir))
        print("OK" if not problems else "\n".join(problems))
        sys.exit(1 if problems else 0)
    elif args.action == "cache":
        for line in curation.cache_images(YamlArtworkRepository(s.artwork_data_dir), force=args.force):
            print(line)
    elif args.action == "search":
        for line in curation.search(args.query or "dog", museum=args.museum, limit=args.limit):
            print(line)
    elif args.action == "draft":
        print(curation.draft(args.query))
    elif args.action == "list":
        for a in YamlArtworkRepository(s.artwork_data_dir).all():
            print(f"{a.id:12} {a.human.posture:10} | dog {a.dog.posture:15} {a.dog.relation:8} "
                  f"{a.dog.interaction:16} → {a.dog.robot_behavior:9} | {a.artist}: {a.title[:40]}")


# ---------------------------------------------------------------------- memes
def cmd_memes(args) -> None:
    s = get_settings()
    from .memes import curation
    from .memes.repository import MemeRepository

    if args.action == "discover":
        print("Imgflip is used for DISCOVERY only — listing there is not permission to reproduce an image.")
        for p in curation.discover(s.meme_data_dir):
            print(f"  draft → {p}")
        print("Drafts are never loaded. Verify, annotate, decide rights, then move to data/memes/annotations/.")
    elif args.action == "validate":
        problems = curation.validate(s.meme_data_dir)
        print("OK" if not problems else "\n".join(problems))
        sys.exit(1 if problems else 0)
    elif args.action == "list":
        for m in MemeRepository(s.meme_data_dir, include_drafts=args.drafts).all_records():
            print(curation.describe(m))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="itu", description="Is This Us? — installation tools")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="download pose model, cache artworks, init database").set_defaults(fn=cmd_setup)

    sim = sub.add_parser("simulate", help="run one complete interaction in the console (mock robot)")
    src = sim.add_mutually_exclusive_group()
    src.add_argument("--image", help="photo of a person (Mode A)")
    src.add_argument("--webcam", action="store_true", help="one webcam frame (Mode B)")
    sim.add_argument("--posture", default="sitting",
                     choices=["standing", "sitting", "crouching", "kneeling", "lying", "leaning_forward"],
                     help="SYNTHETIC skeleton when no image/webcam is given")
    sim.add_argument("--gesture", choices=["holding", "extending"], default=None)
    sim.add_argument("--photographing", action="store_true",
                     help="inject a SYNTHETIC photographing-robot event (no intent detector)")
    sim.add_argument("--scale", type=float, default=1.0, help="synthetic skeleton scale (proximity)")
    sim.add_argument("--camera-index", type=int, default=0)
    sim.add_argument("--lang", choices=["en", "ru", "de"])
    sim.add_argument("--archive", choices=["art", "meme", "both", "surprise"], help="which memory to search")
    sim.add_argument("--seed", type=int, default=None)
    sim.add_argument("--another", action="store_true", help="also simulate 'Try another painting'")
    sim.add_argument("--card", help="write a memory card JPEG to this path")
    sim.set_defaults(fn=cmd_simulate)

    wc = sub.add_parser("webcam", help="live debug display with webcam")
    wc.add_argument("--camera-index", type=int, default=0)
    wc.add_argument("--seed", type=int, default=None)
    wc.set_defaults(fn=cmd_webcam)

    sub.add_parser("bot", help="run the Telegram bot").set_defaults(fn=cmd_bot)

    qr = sub.add_parser("qr", help="create a pairing QR code PNG")
    qr.add_argument("--out", default="pairing_qr.png")
    qr.add_argument("--minutes", type=int, default=None)
    qr.set_defaults(fn=cmd_qr)

    art = sub.add_parser("artworks", help="curation tools")
    art.add_argument("action", choices=["build", "validate", "cache", "search", "draft", "list"])
    art.add_argument("query", nargs="?", help="search text, or museum key for draft (e.g. met:437173)")
    art.add_argument("--museum", choices=["met", "cma"], default="met")
    art.add_argument("--limit", type=int, default=20)
    art.add_argument("--force", action="store_true")
    art.add_argument("--offline", action="store_true", help="build from saved museum metadata without network")
    art.set_defaults(fn=cmd_artworks)

    mm = sub.add_parser("memes", help="meme curation (Imgflip discovery, validate, list)")
    mm.add_argument("action", choices=["discover", "validate", "list"])
    mm.add_argument("--drafts", action="store_true", help="include drafts in list")
    mm.set_defaults(fn=cmd_memes)

    from .hardware_cli import register

    register(sub)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
