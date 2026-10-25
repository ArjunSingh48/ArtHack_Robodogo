"""One bridge, one show: start `python/scripts/start_bridge.sh` for the presentation — never a second copy.

`itu present --with-bridge` uses this:
* a bridge that already answers on the command port is REUSED if it runs our 10 s timeout patch (and left running
  afterwards — we did not start it); a stock go2_main bridge (3 s) is refused: stop it first;
* a port that is taken but does not answer is refused (a hung bridge — look at it, do not start another);
* a show that is already running (data/runtime/present.pid) is refused;
* otherwise the repository's own `scripts/start_bridge.sh <interface>` is started in its own process group, its
  output goes to data/runtime/bridge.log, and we wait until it answers `status` with `sport_timeout_s`;
* shutdown order: the show stops the robot first (software stop → Move 0 → stop → stop_move), THEN the bridge gets
  Ctrl+C (its own exit sends Move 0 → StopMove), then SIGTERM, then SIGKILL — each after a bounded wait.

go2_main is never touched: start_bridge.sh runs it unchanged (see scripts/go2_bridge_patched.py).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # python/
START_SCRIPT = ROOT / "scripts" / "start_bridge.sh"
RUNTIME = ROOT / "data" / "runtime"


class LaunchRefused(RuntimeError):
    pass


def bridge_status(host: str = "localhost", port: int = 5555, timeout_ms: int = 1500) -> dict | None:
    """The bridge's `status` data, or None if nothing answers."""
    try:
        import zmq
    except ImportError:
        return None
    s = zmq.Context.instance().socket(zmq.REQ)
    for opt in (zmq.RCVTIMEO, zmq.SNDTIMEO):
        s.setsockopt(opt, timeout_ms)
    s.setsockopt(zmq.LINGER, 0)
    try:
        s.connect(f"tcp://{host}:{port}")
        s.send_json({"cmd": "status"})
        resp = s.recv_json()
        return (resp.get("data") or {}) if resp.get("ok") else None
    except zmq.ZMQError:
        return None
    finally:
        s.close(linger=0)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


class ShowLock:
    """Refuses a second `itu present` while one runs (stale lock files from a crash are taken over)."""

    def __init__(self, runtime: Path = RUNTIME):
        self.path = runtime / "present.pid"

    def __enter__(self):
        other = read_pid(self.path)
        if other is not None and other != os.getpid():
            raise LaunchRefused(f"a presentation is already running (pid {other}) — stop it first "
                                f"(its terminal: Ctrl+C), or delete {self.path} if that process is gone")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        if read_pid(self.path) == os.getpid():
            self.path.unlink(missing_ok=True)


@dataclass
class BridgeHandle:
    started_by_us: bool
    status: dict
    proc: subprocess.Popen | None = None
    log_path: Path | None = None


class BridgeLauncher:
    def __init__(self, interface: str, *, host: str = "localhost", cmd_port: int = 5555, runtime: Path = RUNTIME,
                 script: Path = START_SCRIPT, required_timeout_s: float = 10.0, wait_s: float = 25.0,
                 popen=subprocess.Popen, status_fn=bridge_status, port_fn=port_in_use, clock=time.monotonic,
                 sleep=time.sleep):
        self.interface, self.host, self.cmd_port = interface, host, cmd_port
        self.runtime, self.script, self.required = runtime, script, required_timeout_s
        self.wait_s, self.popen, self.status_fn, self.port_fn = wait_s, popen, status_fn, port_fn
        self.clock, self.sleep = clock, sleep
        self.pid_path = runtime / "bridge.pid"
        self.handle: BridgeHandle | None = None

    def _check_existing(self, st: dict) -> BridgeHandle:
        t = st.get("sport_timeout_s")
        if t is None or float(t) < self.required:
            raise LaunchRefused(
                ("a bridge is already running WITHOUT our 10 s timeout patch (stock go2_main, 3 s)" if t is None else
                 f"a bridge is already running with a {float(t):.0f} s SDK timeout") +
                f" on port {self.cmd_port}. Stop it (Ctrl+C in its terminal), then run this again — not starting a "
                f"second bridge.")
        return BridgeHandle(started_by_us=False, status=st)

    def ensure(self, *, restart: bool = False) -> BridgeHandle:
        """Reuse a good running bridge, or start ours. Never two."""
        ours = read_pid(self.pid_path)
        st = self.status_fn(self.host, self.cmd_port)
        if restart:
            if ours is None and (st is not None or self.port_fn(self.cmd_port)):
                raise LaunchRefused("--restart-bridge only restarts a bridge this launcher started; the running one "
                                    "was started elsewhere — stop it in its own terminal (Ctrl+C)")
            if ours is not None:
                self.stop_pid(ours)
                st = None
        if st is not None:
            self.handle = self._check_existing(st)
            if ours is not None:
                self.handle.started_by_us = False  # an earlier launcher's bridge: reused, left running
            return self.handle
        if ours is not None:
            raise LaunchRefused(f"a bridge this launcher started earlier (pid {ours}) is running but does not "
                                f"answer — `itu bridge stop`, or --restart-bridge")
        if self.port_fn(self.cmd_port):
            raise LaunchRefused(f"port {self.cmd_port} is taken but nothing answers the bridge protocol — a hung "
                                f"bridge? Close it (or reboot that terminal); not starting a second one")
        if not self.interface:
            raise LaunchRefused("which network interface is wired to the Go2? --interface en6 (see `ifconfig`)")
        self.runtime.mkdir(parents=True, exist_ok=True)
        log_path = self.runtime / "bridge.log"
        out = open(log_path, "ab")
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start_bridge.sh {self.interface} ===\n".encode())
        out.flush()
        env = {**os.environ, "GO2_SPORT_TIMEOUT_S": os.environ.get("GO2_SPORT_TIMEOUT_S", "10")}
        proc = self.popen(["bash", str(self.script), self.interface], stdout=out, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        self.pid_path.write_text(str(proc.pid))
        deadline = self.clock() + self.wait_s
        while self.clock() < deadline:
            if proc.poll() is not None:
                self.pid_path.unlink(missing_ok=True)
                raise LaunchRefused(f"start_bridge.sh exited with code {proc.returncode} — see {log_path}")
            st = self.status_fn(self.host, self.cmd_port)
            if st is not None:
                if st.get("sport_timeout_s") is None or float(st["sport_timeout_s"]) < self.required:
                    self.stop_proc(proc)
                    raise LaunchRefused("the bridge started without the 10 s timeout patch — see " + str(log_path))
                self.handle = BridgeHandle(True, st, proc, log_path)
                return self.handle
            self.sleep(0.5)
        self.stop_proc(proc)
        raise LaunchRefused(f"the bridge did not answer within {self.wait_s:.0f} s — see {log_path} "
                            f"(robot on? cable? interface {self.interface}?)")

    # ------------------------------------------------------------------ shutdown (after the show stopped the robot)
    def stop_proc(self, proc, *, grace_s: float = 10.0) -> None:
        if proc.poll() is None:
            for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if read_pid(self.pid_path) in (proc.pid, None):
            self.pid_path.unlink(missing_ok=True)

    def stop_pid(self, pid: int, *, grace_s: float = 10.0) -> bool:
        for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            end = self.clock() + wait
            while self.clock() < end and pid_alive(pid):
                self.sleep(0.2)
            if not pid_alive(pid):
                break
        self.pid_path.unlink(missing_ok=True)
        return not pid_alive(pid)

    def close(self) -> None:
        """Stop the bridge only if we started it (a reused one keeps running)."""
        h = self.handle
        if h is not None and h.started_by_us and h.proc is not None:
            self.stop_proc(h.proc)
