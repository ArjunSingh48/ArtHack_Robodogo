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
        if frame is None:
            return False, f"{spec} opened but no frame"
        info = src.info() if hasattr(src, "info") else None
        if info:  # what actually runs: name, index, size, measured frame rate
            return True, (f"{spec} → \"{info['label']}\" (webcam:{info['index']}), {info['width']}×{info['height']} "
                          f"at {info['fps']:.0f} fps")
        return True, f"{spec} delivers frames"
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


def default_camera() -> str:
    """macOS: the built-in camera by NAME (never the iPhone via Continuity); elsewhere webcam:0."""
    return "macbook" if sys.platform == "darwin" else "webcam:0"


def cmd_camera_list(args) -> int:
    from .vision.cameras import describe, list_cameras

    cams = list_cameras()
    if not cams:
        print("No cameras found (macOS: allow the terminal in System Settings → Privacy & Security → Camera; "
              "install names with `pip install -e '.[vision]'`).")
        return 1
    print(f"Cameras ({cams[0].method}" + ("" if cams[0].index_verified else
                                          " — indices UNVERIFIED: pip install -e '.[vision]' for AVFoundation names")
          + "):")
    for line in describe(cams):
        print(line)
    print("\nUse:  itu present --camera macbook   or   itu present --camera-name \"FaceTime HD Camera\"")
    if args.snapshots:
        from pathlib import Path

        from PIL import Image

        from .vision.frame_sources import WebcamSource

        out = Path(args.snapshots)
        out.mkdir(parents=True, exist_ok=True)
        for c in cams:
            try:
                src = WebcamSource(c.index)
                f = src.read()
                src.close()
            except Exception as exc:
                print(f"  webcam:{c.index}: {exc}")
                continue
            if f is not None:
                path = out / f"webcam{c.index}.jpg"
                Image.fromarray(f.image).save(path, quality=80)
                print(f"  webcam:{c.index} ({c.name}) → {path}")
    return 0


def cmd_bridge(args) -> int:
    from .bridge_launcher import BridgeLauncher, bridge_status, read_pid
    from .config import get_settings

    s = get_settings()
    launcher = BridgeLauncher(args.interface, host=s.zhaw_bridge_host, cmd_port=s.zhaw_bridge_cmd_port)
    pid = read_pid(launcher.pid_path)
    st = bridge_status(s.zhaw_bridge_host, s.zhaw_bridge_cmd_port)
    if args.bridge_action == "stop":
        if pid is None:
            print("no bridge started by `itu present --with-bridge` is running" +
                  (" (one answers on the port — stop it in its own terminal with Ctrl+C)" if st else ""))
            return 0 if st is None else 1
        print(f"stopping bridge pid {pid} (Ctrl+C → its own Move 0 → StopMove; then SIGTERM / SIGKILL if needed)…")
        return 0 if launcher.stop_pid(pid) else 1
    print(f"bridge on {s.zhaw_bridge_host}:{s.zhaw_bridge_cmd_port}: " +
          (f"answers — SDK timeout {st.get('sport_timeout_s', '3 (stock)')} s" if st else "no answer") +
          (f"; started by the launcher (pid {pid})" if pid else ""))
    return 0


def cmd_present(args) -> int:
    """One show and at most one bridge at a time; the robot is stopped before the bridge goes."""
    import signal

    from .bridge_launcher import LaunchRefused, ShowLock

    def term(signum, frame):  # SIGTERM (e.g. `kill`) ends like Ctrl+C: every finally block runs
        raise KeyboardInterrupt
    old = signal.signal(signal.SIGTERM, term)
    try:
        with ShowLock():
            try:
                return _cmd_present(args)
            finally:
                if getattr(args, "_launcher", None) is not None:
                    print("stopping the bridge (after the robot was stopped)…")
                    _close_launcher(args)
    except LaunchRefused as exc:
        print(f"✗ {exc}")
        return 2
    finally:
        signal.signal(signal.SIGTERM, old)


def _cmd_present(args) -> int:
    from .config import get_settings

    if getattr(args, "camera_name", None):
        args.camera = f"name:{args.camera_name}"

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
    if getattr(args, "with_bridge", False) and not args.sim_robot and not args.no_robot:
        from .bridge_launcher import BridgeLauncher, LaunchRefused

        # the checks need the bridge: start (or reuse) it first; cmd_present stops it after the show
        try:
            args._launcher = BridgeLauncher(args.interface, host=s.zhaw_bridge_host, cmd_port=s.zhaw_bridge_cmd_port)
            h = args._launcher.ensure(restart=getattr(args, "restart_bridge", False))
        except LaunchRefused as exc:
            print(f"✗ {exc}")
            return 2
        print(("✓ started the bridge (python/scripts/start_bridge.sh " + args.interface + f"), log {h.log_path}"
               if h.started_by_us else "✓ using the bridge that is already running (left running afterwards)")
              + f" · SDK timeout {h.status.get('sport_timeout_s')} s")
    if not run_checks(args, s):
        if args.motion or args.walk:  # never --force past a failed check when the robot would move
            print("\nNot starting: a check failed and motion was requested (--force does not apply to motion). "
                  "Fix the ✗ above.")
            return 2
        if not args.force:
            print("\nNot starting (fix the ✗ above, or --force to start anyway without what failed).")
            return 2
    print("\nPoses: " + ("SIMULATED dog" if args.sim_robot else
                         "ON (armed, in place)" if args.motion else "off (add --motion --i-have-physical-estop)")
          + " · Walking: " + ("ON" if args.walk else "off") + " · Telegram: " + ("on" if args.telegram else "off"))
    print(f"Interface: {args.interface} · robot {args.robot_ip} · photo/display camera: {args.camera} · "
          f"pose camera: {args.pose_camera} (the robot's camera steers the walk)")
    from .performance.cli import cmd_show

    show_args = argparse.Namespace(
        show_action=None, lang="en", dry_run=False, auto=False, mode="automatic", display="web", source=args.camera,
        no_voice=args.no_voice, fast=None, motion=args.motion, out=None, seed=None, walk=args.walk, finale=None,
        programme="presentation", telegram=args.telegram, sim_robot=args.sim_robot, interactive=args.rounds_only, start_at=None,
        participant="one", consent="yes", send="yes", i_have_physical_estop=args.i_have_physical_estop)
    return cmd_show(show_args) or 0


def _close_launcher(args) -> None:
    launcher = getattr(args, "_launcher", None)
    if launcher is not None:
        launcher.close()


def register(sub) -> None:
    p = sub.add_parser("present", help="check robot/bridge/camera, then start the presentation (beamer + robot)")
    p.add_argument("--interface", default=os.environ.get("ROBOT_NETWORK_INTERFACE") or "en6",
                   help="the USB-Ethernet interface to the Go2 (default en6)")
    p.add_argument("--robot-ip", default="192.168.123.161")
    p.add_argument("--camera", default=os.environ.get("SHOW_CAMERA_SOURCE") or default_camera(),
                   help="photo + display camera: macbook (default on macOS: the built-in camera by name, never the "
                        "iPhone), name:<camera name>, or webcam:<index> — see `itu camera-list`")
    p.add_argument("--camera-name", help='the photo camera by name, e.g. "FaceTime HD Camera" (= --camera name:…)')
    p.add_argument("--with-bridge", action="store_true",
                   help="start python/scripts/start_bridge.sh <interface> first (or reuse a running patched bridge); "
                        "stopped after the show, after the robot")
    p.add_argument("--restart-bridge", action="store_true", help="with --with-bridge: restart the bridge it started")
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

    c = sub.add_parser("camera-list", help="list cameras with names and OpenCV indices (macOS: built-in vs iPhone)")
    c.add_argument("--snapshots", metavar="DIR", help="also save one frame per camera into DIR, to see which is which")
    c.set_defaults(fn=cmd_camera_list)

    b = sub.add_parser("bridge", help="the bridge started by `itu present --with-bridge`: status | stop")
    b.add_argument("bridge_action", choices=["status", "stop"])
    b.add_argument("--interface", default=os.environ.get("ROBOT_NETWORK_INTERFACE") or "en6")
    b.set_defaults(fn=cmd_bridge)
