"""Run the ZHAW go2_bridge (github.com/zhaw-physical-ai/go2_main) with this project's fixes — go2_main unchanged.

go2_main is university-owned; we never edit it. This runs its own `go2_bridge` in its own Python (3.11, Unitree
SDK) and adjusts it in memory before it starts:

* the SportClient timeout becomes GO2_SPORT_TIMEOUT_S (default 10 s) instead of go2_main's 3 s — otherwise a slow
  gesture (hello ≈ 4 s) is performed but answered with code 3104 "outcome unknown";
* `status` reports it (`sport_timeout_s`, `patched_by`) so our adapter can check it before arming;
* on Ctrl+C / SIGTERM the bridge leaves the walking gait: Move(0,0,0) → StopMove, then go2_main's own shutdown.

Started by `start_bridge.sh` (next to this file), which sets PYTHONPATH to the go2_main checkout.
"""

from __future__ import annotations

import json
import logging
import os

PATCHED_BY = "is_this_us"
log = logging.getLogger("go2_bridge.is_this_us")


def sport_timeout() -> float:
    return float(os.environ.get("GO2_SPORT_TIMEOUT_S", "10"))


def apply() -> None:
    """Patch go2_bridge's Robot and CommandHandler classes (in memory only)."""
    from go2_bridge import command_handler, robot

    timeout = sport_timeout()
    init, shutdown, status = robot.Robot.init, robot.Robot.shutdown, command_handler.CommandHandler._handle_status

    def patched_init(self) -> None:
        init(self)
        self.sport.SetTimeout(timeout)
        log.info("SportClient timeout %.1f s (go2_main default 3 s; set by Is_this_us)", timeout)

    def patched_shutdown(self) -> None:
        for name, call in (("Move(0,0,0)", lambda: self.sport.Move(0.0, 0.0, 0.0)), ("StopMove", self.sport.StopMove)):
            try:
                log.info("shutdown: %s → %s", name, call() if self.sport else "no SportClient")
            except Exception as exc:
                log.warning("shutdown: %s failed: %s", name, exc)
        shutdown(self)

    def patched_status(self) -> bytes:
        resp = json.loads(status(self))
        resp["data"] = {**(resp.get("data") or {}), "sport_timeout_s": timeout, "patched_by": PATCHED_BY}
        return json.dumps(resp).encode()

    robot.Robot.init = patched_init
    robot.Robot.shutdown = patched_shutdown
    command_handler.CommandHandler._handle_status = patched_status


def main() -> None:
    apply()
    from go2_bridge.__main__ import main as bridge_main

    bridge_main()


if __name__ == "__main__":
    main()
