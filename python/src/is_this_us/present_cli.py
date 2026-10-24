"""`itu present` — the one command for the normal local presentation (laptop + beamer + Go2 via the ZHAW bridge).

Checks first (read-only, nothing moves), then starts the presentation programme:
  1. the robot's USB-Ethernet interface exists, has link and a 192.168.123.x address
  2. the robot answers ping
  3. the ZHAW bridge answers (and reports its SDK timeout / safe-actions mode)
  4. the show camera opens and delivers a frame (macOS camera permission)
  5. prints the projector and operator URLs, then runs — Telegram off, walking off unless asked.

Settings for this run are set in-process (no .env editing). Motion still needs --motion AND
--i-have-physical-estop; walking additionally --walk, SHOW_WALK_ENABLED=true and calibration (SHOW_WALK_CALIBRATED).
Dangerous Unitree actions are never sent (the adapter only knows calm actions; start the bridge with
GO2_SAFE_ACTIONS_ONLY=1 for a second lock).
"""

from __future__ import annotations

import argparse
import os
import sys

OK, BAD, WARN = "  ✓", "  ✗", "  !"


def _bridge_status(host: str, port: int) -> tuple[bool, dict | str]:
    try:
        import zmq
    except ImportError:
        return False, "pyzmq missing: pip install -e '.[bridge]'"
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 1500)
    s.setsockopt(zmq.SNDTIMEO, 1500)
    s.setsockopt(zmq.LINGER, 0)
    try:
        s.connect(f"tcp://{host}:{port}")
        s.send_json({"cmd": "status"})
        resp = s.recv_json()
        return bool(resp.get("ok")), resp.get("data") or {}
    except zmq.ZMQError:
        return False, "no answer"
    finally:
        s.close(linger=0)


def _camera_ok(spec: str) -> tuple[bool, str]:
    if spec in ("", "none") or spec.startswith("synthetic"):
        return True, spec or "none"
    from .performance.runtime import explain_camera_error
    from .vision.frame_sources import make_frame_source

    try:
        src = make_frame_source(spec)
    except Exception as exc:
        return False, explain_camera_error(spec, exc)
    try:
        frame = src.read()
        return (frame is not None), (f"{spec} delivers frames" if frame is not None else f"{spec} opened but no frame")
    finally:
        try:
            src.close()
        except Exception:
            pass


def run_checks(args, settings) -> bool:
    """→ True if the presentation may start with what was requested."""
    from .netcheck import check_link, ping
    from .robot.zhaw_bridge import REQUIRED_SDK_TIMEOUT_S

    robot_needed = args.motion or args.walk
    good = True
    print("\nChecks (read-only, nothing moves):")
    if args.no_robot:
        print(f"{WARN} robot checks skipped (--no-robot)")
    else:
        link = check_link(args.interface)
        print(f"{OK if link.ok else BAD} network: {link.message}")
        reach = link.ok and ping(args.robot_ip)
        if link.ok:
            print(f"{OK if reach else BAD} robot {args.robot_ip} " + ("answers ping" if reach else
                  "does not answer ping — is it on and fully booted (~1 min)? cable in the robot's Ethernet port?"))
        alive, info = _bridge_status(settings.zhaw_bridge_host, settings.zhaw_bridge_cmd_port)
        start_hint = f"      python/scripts/start_bridge.sh {args.interface}   (from the Is_this_us checkout)"
        if alive:
            t = info.get("sport_timeout_s") if isinstance(info, dict) else None
            if t and float(t) >= REQUIRED_SDK_TIMEOUT_S:
                print(f"{OK} bridge answers on {settings.zhaw_bridge_host}:{settings.zhaw_bridge_cmd_port} "
                      f"(SDK timeout {float(t):.0f} s)")
            else:
                print(f"{BAD} bridge answers, but " + (f"its SDK timeout is {float(t):.0f} s" if t else
                      "it is stock go2_main (3 s SDK timeout)") + " — hello would return 3104 and the robot "
                      f"would not arm. Stop it and start it with:\n{start_hint}")
                if robot_needed:
                    good = False
        else:
            print(f"{BAD} bridge: {info}. Start it in another terminal:\n{start_hint}")
        if alive:
            dog_ok, dog_msg = _camera_ok(f"zhaw_bridge:{settings.zhaw_bridge_host}:{settings.zhaw_bridge_pub_port}")
            print(f"{OK if dog_ok else BAD} dog's camera: {dog_msg if dog_ok else dog_msg + ' (bridge camera stream)'}"
                  + ("" if dog_ok else " — poses will be read with the laptop camera"))
        if robot_needed and not (link.ok and reach and alive):
            good = False
        elif not (link.ok and alive):
            print(f"{WARN} the presentation will run WITHOUT the robot (no poses)")
    cam_ok, cam_msg = _camera_ok(args.camera)
    print(f"{OK if cam_ok else BAD} camera: {cam_msg}")
    if not cam_ok:
        if args.fallback:
            print(f"{WARN} will use the fallback camera {args.fallback} for poses (no photos of person + robot)")
        else:
            print(f"{WARN} no camera: the presentation runs without pose reading and photos "
                  f"(or pass --fallback zhaw_bridge)")
    if args.walk and not (settings.show_walk_calibrated or os.environ.get("SHOW_WALK_CALIBRATED", "").lower() == "true"):
        print(f"{BAD} walking requested but not calibrated: `itu show walk-test --calibrate 2 --posture sitting` "
              f"(and standing), put the k values in .env, then SHOW_WALK_CALIBRATED=true")
        good = False
    return good


def cmd_present(args) -> int:
    from .config import get_settings

    if args.sim_robot and (args.motion or args.walk):
        sys.exit("--sim-robot replaces the real robot: leave out --motion/--walk (the simulated dog does both).")
    if args.sim_robot:
        args.no_robot = True
    if (args.motion or args.walk) and not args.i_have_physical_estop:
        sys.exit("Refusing motion: add --i-have-physical-estop and keep the Unitree remote in your hand.")
    if args.walk and not args.motion:
        sys.exit("--walk needs --motion as well (walking is an extra on top of poses).")
    # this run's settings, in-process only (nothing is written to .env)
    env = {"SHOW_PROGRAMME": "presentation", "SHOW_DISPLAY": "web", "SHOW_MODE": "automatic",
           "SHOW_CAMERA_SOURCE": args.camera, "SHOW_CAMERA_FALLBACK": args.fallback or "",
           "ROBOT_MODEL": "zhaw_bridge", "ROBOT_NETWORK_INTERFACE": args.interface,
           "SHOW_POSE_CAMERA": args.pose_camera}
    if args.motion:
        env.update({"APP_MODE": "installation", "ROBOT_MOTION_ENABLED": "true", "SHOW_MOTION_ENABLED": "true"})
    env["SHOW_WALK_ENABLED"] = "true" if args.walk else "false"
    if args.sim_robot:
        env["ROBOT_MODEL"] = "mock"  # nothing connects to a robot
    if not args.telegram:
        env["SHOW_TELEGRAM_CHAT_ID"] = ""  # Telegram off unless asked: local display only
    os.environ.update(env)
    get_settings.cache_clear()
    s = get_settings()
    if not run_checks(args, s) and not args.force:
        print("\nNot starting (fix the ✗ above, or --force to start anyway without what failed).")
        return 2
    print("\nPoses: " + ("SIMULATED dog" if args.sim_robot else
                         "ON (armed, in place)" if args.motion else "off (add --motion --i-have-physical-estop)")
          + " · Walking: " + ("ON" if args.walk else "off") + " · Telegram: " + ("on" if args.telegram else "off"))
    from .performance.cli import cmd_show

    show_args = argparse.Namespace(
        show_action=None, lang="en", dry_run=False, auto=False, mode="automatic", display="web", source=args.camera,
        no_voice=args.no_voice, fast=None, motion=args.motion, out=None, seed=None, walk=args.walk, finale=None,
        programme="presentation", telegram=args.telegram, sim_robot=args.sim_robot, interactive=args.rounds_only, start_at=None,
        participant="one", consent="yes", send="yes", i_have_physical_estop=args.i_have_physical_estop)
    return cmd_show(show_args) or 0


def register(sub) -> None:
    p = sub.add_parser("present", help="check robot/bridge/camera, then start the presentation (beamer + robot)")
    p.add_argument("--interface", default=os.environ.get("ROBOT_NETWORK_INTERFACE") or "en6",
                   help="the USB-Ethernet interface to the Go2 (default en6)")
    p.add_argument("--robot-ip", default="192.168.123.161")
    p.add_argument("--camera", default=os.environ.get("SHOW_CAMERA_SOURCE") or "webcam:0",
                   help="pose + photo camera (default the laptop's webcam:0)")
    p.add_argument("--fallback", default=os.environ.get("SHOW_CAMERA_FALLBACK", ""),
                   help="if the camera fails, read poses with this one (e.g. zhaw_bridge; then no joint photos)")
    p.add_argument("--pose-camera", choices=["robot", "laptop"], default=os.environ.get("SHOW_POSE_CAMERA") or "robot",
                   help="which camera reads the person's pose (default robot = the dog's own camera); "
                        "the laptop camera always takes the photo")
    p.add_argument("--motion", action="store_true", help="poses in place (sit, lie down, paw, stretch)")
    p.add_argument("--walk", action="store_true", help="also walk to the person (needs --motion + calibration)")
    p.add_argument("--i-have-physical-estop", action="store_true")
    p.add_argument("--telegram", action="store_true", help="also send to the configured Telegram chat")
    p.add_argument("--no-robot", action="store_true", help="skip robot checks (projector + camera only)")
    p.add_argument("--sim-robot", action="store_true",
                   help="no robot: a simulated dog (poses + walking) is drawn on the projector instead")
    p.add_argument("--no-voice", action="store_true")
    p.add_argument("--rounds-only", action="store_true", help="skip the narration, straight to each round")
    p.add_argument("--force", action="store_true", help="start even if a check failed (what failed stays off)")
    p.set_defaults(fn=cmd_present)
