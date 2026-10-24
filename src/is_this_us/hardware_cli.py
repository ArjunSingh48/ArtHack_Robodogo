"""Real-robot tools: diagnosis, supervised motion tests, and the live terminal experience.

All of these talk to a physical Unitree Go2 and are HARDWARE-UNVERIFIED until run on
your unit. Only `robot-check` is safe to run with people nearby: it sends no motion.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path

from .config import get_settings

ESTOP_REMINDER = (
    "\n  ⚠  Area clear (≥1.5 m)? Unitree remote / physical stop in your hand?\n"
    "     Telegram /stop and Ctrl+C are SOFTWARE stops only.\n"
)


def _ok(label: str, value: str = "") -> None:
    print(f"  ✓ {label}{(': ' + value) if value else ''}")


def _bad(label: str, hint: str = "") -> None:
    print(f"  ✗ {label}" + (f"\n      → {hint}" if hint else ""))


def _interfaces() -> list[str]:
    try:
        return [name for _, name in socket.if_nameindex()]
    except OSError:
        return []


# ---------------------------------------------------------------------- robot-check
def cmd_robot_check(args) -> None:
    """Step-by-step read-only diagnosis. Sends NO motion commands."""
    s = get_settings()
    iface = args.interface or s.robot_network_interface
    print("\nIs This Us? — robot check (read-only, no motion)\n")
    if s.robot_model.value == "zhaw_bridge":
        return _check_bridge(s, args)

    try:
        import unitree_sdk2py  # type: ignore  # noqa: F401
        _ok("unitree_sdk2py installed")
    except ImportError:
        _bad("unitree_sdk2py not installed", "see docs/HARDWARE_SETUP.md step 2")
        sys.exit(1)

    names = _interfaces()
    if iface and iface not in names:
        _bad(f"network interface {iface!r} not found", f"available: {', '.join(names)}")
        sys.exit(1)
    _ok("network interface", iface or "<default> (set ROBOT_NETWORK_INTERFACE for Ethernet)")

    from .robot.unitree_go2 import UnitreeGo2Adapter

    robot = UnitreeGo2Adapter(iface, state_topic=s.go2_state_topic, lowstate_topic=s.go2_lowstate_topic)
    try:
        robot.connect()
    except Exception as exc:
        _bad("DDS / SportClient init failed", f"{exc} — check cable, static IP, interface name")
        sys.exit(1)
    _ok("DDS initialised and SportClient created")

    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and robot.live_state_topic is None:
        time.sleep(0.1)
    st = robot.status()
    if robot.live_state_topic:
        _ok("receiving sport state", robot.live_state_topic)
        if robot.live_state_topic != s.go2_state_topic:
            print(f"      → set GO2_STATE_TOPIC={robot.live_state_topic} in .env")
    else:
        _bad("no sport state received in 4 s",
             "robot on? same subnet (often 192.168.123.x)? Go2 EDU? firewall off for this interface?")
    time.sleep(0.5)
    st = robot.status()
    if st.battery_percent is not None:
        (_ok if st.battery_percent >= s.min_battery_percent else _bad)(
            f"battery {st.battery_percent}%", "" if st.battery_percent >= s.min_battery_percent else "charge first")
    else:
        _bad("battery unknown (no rt/lowstate)", "motion will be refused until battery is readable")
    (_ok if st.error_code in (0, None) else _bad)(f"error code {st.error_code}")

    try:
        code, mode = robot.check_mode()
        if code == 0:
            _ok("motion switcher mode", str(mode))
            if mode and not mode.get("name"):
                print("      → no high-level motion service active; enable normal/sport mode in the Unitree app")
        else:
            _bad(f"motion switcher not answering (code {code})", "older firmware may not have it — not fatal")
    except Exception as exc:
        _bad(f"motion switcher check failed: {exc}", "not fatal")

    if not args.no_camera:
        try:
            from .vision.frame_sources import UnitreeFrontCameraSource

            frame = UnitreeFrontCameraSource(iface).read()
            if frame is None:
                _bad("front camera returned no image")
            else:
                h, w = frame.image.shape[:2]
                _ok("front camera", f"{w}×{h}")
                if args.save_frame:
                    from PIL import Image

                    Image.fromarray(frame.image).save(args.save_frame)
                    print(f"      saved one test frame → {args.save_frame} (delete it after checking framing)")
        except Exception as exc:
            _bad(f"camera failed: {exc}", "Mode C optional; you can use CAMERA_SOURCE=webcam")

    robot.close()
    print("\nNext (area clear, remote in hand):  itu robot-test --sequence --i-have-physical-estop\n")


def _check_bridge(s, args) -> None:
    from .robot.zhaw_bridge import STEP_TO_ACTION, BridgeError, ZhawBridgeAdapter, ZhawBridgeCameraSource

    print(f"  connection: ZHAW go2_main bridge at {s.zhaw_bridge_host}:{s.zhaw_bridge_cmd_port}")
    robot = ZhawBridgeAdapter(s.zhaw_bridge_host, s.zhaw_bridge_cmd_port, timeout_ms=3000)
    try:
        robot.connect()
    except (BridgeError, Exception) as exc:
        _bad("bridge not answering", "start it first: GO2_NETWORK_INTERFACE=<iface> python -m go2_bridge "
             f"(in the go2_main repo) — {exc}")
        sys.exit(1)
    _ok("bridge answers", str(robot._last_status))
    actions = set(robot.list_actions())
    needed = set(STEP_TO_ACTION.values()) | {"stop_move"}
    missing = needed - actions
    (_ok if not missing else _bad)("bridge actions we need", ", ".join(sorted(needed)) if not missing
                                   else f"missing: {', '.join(sorted(missing))}")
    print("  ℹ  battery is not reported through the bridge — watch it in the Unitree app")
    print("  ℹ  look_aside / peer are not available through the bridge (no Euler); the robot stands instead")
    if not args.no_camera:
        cam = ZhawBridgeCameraSource(s.zhaw_bridge_host, s.zhaw_bridge_pub_port)
        frame = cam.read()
        if frame is None:
            _bad("no camera frame from the bridge (port %d)" % s.zhaw_bridge_pub_port)
        else:
            h, w = frame.image.shape[:2]
            _ok("bridge camera", f"{w}×{h}")
            if args.save_frame:
                from PIL import Image

                Image.fromarray(frame.image).save(args.save_frame)
                print(f"      saved one test frame → {args.save_frame} (delete it after checking framing)")
        cam.close()
    robot.close()
    print("\nNext (area clear, remote in hand):  itu robot-test --sequence --i-have-physical-estop\n")


# ---------------------------------------------------------------------- robot-test
SEQUENCE = ["stand", "sit", "stand", "lie_down", "stand", "greet", "stretch", "stand", "look_aside", "peer"]


def _real_robot(s, iface, model: str | None = None):
    """The real robot connection: direct SDK (go2) or the ZHAW bridge (zhaw_bridge)."""
    model = model or s.robot_model.value
    if model == "zhaw_bridge":
        from .robot.zhaw_bridge import ZhawBridgeAdapter

        robot = ZhawBridgeAdapter(s.zhaw_bridge_host, s.zhaw_bridge_cmd_port)
    else:
        from .robot.unitree_go2 import UnitreeGo2Adapter

        robot = UnitreeGo2Adapter(iface, state_topic=s.go2_state_topic, lowstate_topic=s.go2_lowstate_topic)
    robot.connect()
    return robot


def _go2_guard(s, iface, *, cooldown: float, model: str | None = None):
    from .motion.guard import MotionGuard

    robot = _real_robot(s, iface, model)
    time.sleep(1.5)  # let state + battery arrive
    guard = MotionGuard(robot, hardware_gates_open=True, cooldown_s=cooldown,
                        timeout_s=s.motion_timeout_seconds, min_battery=s.min_battery_percent)
    return robot, guard


def cmd_robot_test(args) -> None:
    """Supervised motion test: one behavior, or a sequence with a confirmation before each step."""
    if not args.i_have_physical_estop:
        sys.exit("Refusing: pass --i-have-physical-estop and keep the remote / e-stop in your hand.")
    s = get_settings()
    from .domain import MotionPlan, Reproducibility, RobotBehavior

    steps = SEQUENCE if args.sequence else [args.behavior]
    if not steps or steps == [None]:
        sys.exit("Give a behavior (stand, sit, lie_down, greet, stretch) or --sequence.")
    print(ESTOP_REMINDER)
    robot, guard = _go2_guard(s, args.interface or s.robot_network_interface, cooldown=0)
    ok, why = guard.arm(operator_id=0)
    print(f"  arm: {ok} ({why})")
    if not ok:
        sys.exit(1)
    unsupported = {b.value for b in getattr(robot, "unsupported_behaviors", ())}
    if unsupported & set(steps):
        print(f"  skipping {', '.join(sorted(unsupported & set(steps)))}: not available on this connection")
        steps = [b for b in steps if b not in unsupported]
    timings = []
    try:
        for b in steps:
            if input(f"\n  Enter → {b}   (q + Enter to quit) ").strip().lower() == "q":
                break
            beh = RobotBehavior(b)
            t0 = time.monotonic()
            r = guard.execute(MotionPlan(requested=beh, behavior=beh, fidelity=Reproducibility.YES,
                                         explanation="supervised test"))
            dt = time.monotonic() - t0
            timings.append((b, r.status.value, r.detail, round(dt, 1)))
            print(f"    {r.status.value}: {r.detail}  ({dt:.1f}s)  body={robot.body}  battery={robot.status().battery_percent}%")
            if b == "look_aside":
                print("    → did the body turn slightly LEFT or RIGHT? (note it; yaw sign is hardware-unverified)")
            if b == "peer":
                print("    → did the nose go DOWN? If it went up, report it: PEER_PITCH sign must be flipped")
            if r.status.value in ("failed", "stopped", "rejected"):
                print("    stopping the test here.")
                break
    except KeyboardInterrupt:
        guard.software_stop("Ctrl+C during test")
        print("\n  software stop sent")
    finally:
        guard.disarm("test finished")
        guard.shutdown()
        robot.close()
    print("\n  Results (paste these back so the settle times can be tuned):")
    for row in timings:
        print("   ", row)


# ---------------------------------------------------------------------- live
def cmd_live(args) -> None:
    """The full experience in the terminal with the real robot (no Telegram needed)."""
    s = get_settings()
    from .bot.app import build_estimator, build_library, build_planner
    from .motion.guard import MotionGuard
    from .notifier import ConsoleNotifier
    from .orchestrator import InteractionOrchestrator
    from .speech.synthesizer import LocalSpeakerPlayer, make_synthesizer
    from .storage import Storage
    from .vision.frame_sources import make_frame_source

    iface = args.interface or s.robot_network_interface
    if args.no_motion:
        from .robot.mock import MockRobotAdapter

        robot = MockRobotAdapter(time_scale=0.0)
        robot.connect()
        guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=s.motion_timeout_seconds)
        print("\n  --no-motion: real camera, robot stays still (mock motion)\n")
    else:
        if not args.i_have_physical_estop:
            sys.exit("Refusing: pass --i-have-physical-estop (or use --no-motion to only test camera + matching).")
        print(ESTOP_REMINDER)
        robot, guard = _go2_guard(s, iface, cooldown=s.motion_cooldown_seconds)
        ok, why = guard.arm(operator_id=0)
        print(f"  arm: {ok} ({why})")
        if not ok:
            sys.exit(1)

    estimator = build_estimator(s)
    if estimator is None:
        sys.exit("Pose model missing — run `itu setup`.")
    source = make_frame_source(args.source or s.camera_source, network_interface=iface)
    lang = args.lang or s.default_language
    storage = Storage(":memory:")
    storage.touch_visitor(1, lang)
    orch = InteractionOrchestrator(
        repo=build_library(s), estimator=estimator, storage=storage, guard=guard,
        notifier=ConsoleNotifier(), synthesizer=make_synthesizer(s), planner=build_planner(s, robot),
        player=LocalSpeakerPlayer(True), default_lang=lang, default_archive=args.archive or s.default_archive,
    )
    print(f"  camera: {source.name}   voice: {'OpenAI Realtime' if s.voice_configured else 'text-only'}")
    try:
        while True:
            cmd = input("\n  Enter = observe   q = quit   s = stop  > ").strip().lower()
            if cmd == "q":
                break
            if cmd == "s":
                guard.software_stop("operator s")
                print("  stopped; re-arm by restarting `itu live`")
                continue
            outcome = asyncio.run(orch.observe_installation(1, source))
            print("\n  DEBUG")
            for line in outcome.debug_lines():
                print("    " + line)
            if outcome.result.status.value == "rejected":
                print(f"    (motion refused: {outcome.result.detail})")
    except KeyboardInterrupt:
        guard.software_stop("Ctrl+C")
        print("\n  software stop sent")
    finally:
        guard.disarm("live session ended")
        guard.shutdown()
        source.close()
        estimator.close()
        robot.close()


# ---------------------------------------------------------------------- talk
def cmd_talk(args) -> None:
    """Live spoken conversation with the dog (OpenAI Realtime speech-to-speech)."""
    s = get_settings()
    if args.list_devices:
        from .talk.audio import list_devices

        print(list_devices())
        return
    if not s.openai_api_key:
        sys.exit("OPENAI_API_KEY is empty — talk mode needs it. See docs/TALK.md.")
    from openai import AsyncOpenAI

    from .bot.app import build_estimator, build_library, build_planner
    from .motion.guard import MotionGuard
    from .notifier import ConsoleNotifier
    from .orchestrator import InteractionOrchestrator
    from .speech.synthesizer import NullSynthesizer
    from .storage import Storage
    from .talk.audio import SoundDeviceAudio
    from .talk.persona import build_instructions, load_story
    from .talk.session import LiveConversation, TalkSettings
    from .talk.tools import TalkTools
    from .vision.frame_sources import make_frame_source

    iface = args.interface or s.robot_network_interface
    if args.robot in ("go2", "zhaw_bridge"):
        if not args.i_have_physical_estop:
            sys.exit(f"Refusing: --robot {args.robot} needs --i-have-physical-estop (remote in hand, area clear).")
        print(ESTOP_REMINDER)
        robot, guard = _go2_guard(s, iface, cooldown=s.motion_cooldown_seconds, model=args.robot)
        ok, why = guard.arm(operator_id=0)
        print(f"  arm: {ok} ({why})")
        if not ok:
            sys.exit(1)
    else:
        from .robot.mock import MockRobotAdapter

        robot = MockRobotAdapter(time_scale=0.0)
        robot.connect()
        guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=s.motion_timeout_seconds)

    estimator = build_estimator(s) if not args.no_camera else None
    source = None
    if not args.no_camera:
        if estimator is None:
            sys.exit("Pose model missing — run `itu setup` (or use --no-camera).")
        source = make_frame_source(args.source or s.camera_source, network_interface=iface)
    storage = Storage(":memory:")  # talk mode keeps nothing after the session
    storage.touch_visitor(0, args.lang or s.default_language)
    orch = InteractionOrchestrator(
        repo=build_library(s), estimator=estimator, storage=storage, guard=guard, notifier=ConsoleNotifier(),
        synthesizer=NullSynthesizer(),  # the live voice speaks; no second TTS voice
        planner=build_planner(s, robot), default_lang=args.lang or s.default_language,
        default_archive=args.archive or s.default_archive,
    )
    lang_names = {"en": "English", "ru": "Russian", "de": "German"}
    instructions = build_instructions(load_story(s.story_path), lang_names.get(args.lang or s.default_language, "English"))

    def dev(x):
        return int(x) if x.isdigit() else (x or None)

    audio = SoundDeviceAudio(dev(s.talk_input_device), dev(s.talk_output_device))
    talk_settings = TalkSettings(
        model=s.talk_model or s.openai_realtime_model, voice=s.talk_voice or s.openai_realtime_voice,
        half_duplex=not args.full_duplex and s.talk_half_duplex,
        idle_timeout_s=s.talk_idle_seconds, max_session_s=s.talk_max_session_seconds,
    )
    print(f"\n  🐾 talk mode — model {talk_settings.model}, voice {talk_settings.voice}, "
          f"{'half' if talk_settings.half_duplex else 'full'}-duplex, robot {robot.status().model}, "
          f"camera {source.name if source else 'off'}")
    print("  Microphone audio is streamed to OpenAI while a conversation runs. Nothing is recorded here.")
    try:
        while True:
            cmd = input("\n  Enter = start a conversation   q = quit  > ").strip().lower()
            if cmd == "q":
                break
            convo = LiveConversation(client=AsyncOpenAI(api_key=s.openai_api_key), audio=audio,
                                     tools=TalkTools(orch, source), instructions=instructions,
                                     settings=talk_settings)
            try:
                stats = asyncio.run(convo.run())
            except KeyboardInterrupt:
                guard.software_stop("Ctrl+C in talk mode")
                print("\n  conversation stopped (software stop sent)")
                continue
            print(f"  ended ({stats.ended_by}); {stats.responses} replies; tools: {stats.tool_calls or '—'}")
    except KeyboardInterrupt:
        guard.software_stop("Ctrl+C")
    finally:
        guard.disarm("talk session ended")
        guard.shutdown()
        if source:
            source.close()
        if estimator:
            estimator.close()
        robot.close()


def register(sub) -> None:
    rc = sub.add_parser("robot-check", help="read-only Go2 diagnosis (no motion)")
    rc.add_argument("--interface", help="override ROBOT_NETWORK_INTERFACE")
    rc.add_argument("--no-camera", action="store_true")
    rc.add_argument("--save-frame", type=Path, help="save one camera frame to check framing")
    rc.set_defaults(fn=cmd_robot_check)

    rt = sub.add_parser("robot-test", help="supervised motion test on the real robot")
    rt.add_argument("behavior", nargs="?",
                    choices=["stand", "sit", "lie_down", "stretch", "greet", "look_aside", "peer"])
    rt.add_argument("--sequence", action="store_true", help=f"run {' → '.join(SEQUENCE)} with a confirmation each step")
    rt.add_argument("--interface")
    rt.add_argument("--i-have-physical-estop", action="store_true")
    rt.set_defaults(fn=cmd_robot_test)

    lv = sub.add_parser("live", help="full experience in the terminal with the real robot")
    lv.add_argument("--source", help="camera: unitree | webcam[:i] | image:<path> (default CAMERA_SOURCE)")
    lv.add_argument("--interface")
    lv.add_argument("--lang", choices=["en", "ru", "de"])
    lv.add_argument("--archive", choices=["art", "meme", "both", "surprise"])
    lv.add_argument("--no-motion", action="store_true", help="real camera + matching, robot does not move")
    lv.add_argument("--i-have-physical-estop", action="store_true")
    lv.set_defaults(fn=cmd_live)

    tk = sub.add_parser("talk", help="live spoken conversation with the dog (OpenAI Realtime)")
    tk.add_argument("--robot", choices=["mock", "go2", "zhaw_bridge"], default="mock")
    tk.add_argument("--source", help="camera: webcam[:i] | unitree | image:<path> (default CAMERA_SOURCE)")
    tk.add_argument("--no-camera", action="store_true", help="talk only; the dog cannot look")
    tk.add_argument("--archive", choices=["art", "meme", "both", "surprise"])
    tk.add_argument("--lang", choices=["en", "ru", "de"], help="fallback language (the dog follows the visitor)")
    tk.add_argument("--full-duplex", action="store_true", help="allow interrupting the dog — headset/echo-cancelling only")
    tk.add_argument("--interface")
    tk.add_argument("--list-devices", action="store_true", help="show microphones and speakers, then exit")
    tk.add_argument("--i-have-physical-estop", action="store_true")
    tk.set_defaults(fn=cmd_talk)
