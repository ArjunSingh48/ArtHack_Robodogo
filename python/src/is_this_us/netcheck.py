"""Read-only network diagnostics for the Go2's USB-Ethernet link (macOS and Linux). Never changes anything.

On macOS, unplugging the USB-Ethernet adapter removes its interface (e.g. `en6`) entirely; plugging it into another
port can give it a different name. That is expected, not a robot problem — these checks say exactly which case it is.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import subprocess
from dataclasses import dataclass, field

ROBOT_SUBNET = "192.168.123.0/24"


@dataclass
class Interface:
    name: str
    active: bool
    ipv4: list[str] = field(default_factory=list)

    def in_subnet(self, subnet: str = ROBOT_SUBNET) -> list[str]:
        net = ipaddress.ip_network(subnet)
        return [a for a in self.ipv4 if ipaddress.ip_address(a) in net]


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_ifconfig(text: str) -> dict[str, Interface]:
    """macOS / BSD `ifconfig` (and Linux net-tools) output → interfaces."""
    out: dict[str, Interface] = {}
    cur: Interface | None = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+):?\s", line)
        if m and not line.startswith((" ", "\t")):
            cur = Interface(m.group(1).rstrip(":"), active=False)
            out[cur.name] = cur
            if "RUNNING" in line and "UP" in line:
                cur.active = True
            continue
        if cur is None:
            continue
        s = line.strip()
        m = re.match(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", s)
        if m:
            cur.ipv4.append(m.group(1))
        if s.startswith("status:"):  # macOS: "status: active" / "status: inactive" is the cable/link state
            cur.active = s.split(":", 1)[1].strip() == "active"
    return out


def interfaces() -> dict[str, Interface]:
    text = _run(["ifconfig"]) or _run(["/sbin/ifconfig"])
    found = parse_ifconfig(text) if text else {}
    if not found:  # minimal fallback: names only
        try:
            found = {n: Interface(n, active=True) for _, n in socket.if_nameindex()}
        except OSError:
            pass
    return found


@dataclass
class LinkReport:
    ok: bool
    interface: str
    message: str
    candidates: list[str] = field(default_factory=list)


def check_link(name: str, subnet: str = ROBOT_SUBNET, found: dict[str, Interface] | None = None) -> LinkReport:
    """Is `name` present, up, and holding an address in the robot's subnet? Suggests the right one if not."""
    found = interfaces() if found is None else found
    cands = [i.name for i in found.values() if i.in_subnet(subnet)]
    hint = (f" Interfaces with a {subnet} address: {', '.join(cands)} — use that name."
            if cands else
            f" No interface has a {subnet} address: plug in the USB-Ethernet adapter, then set it to Manually "
            f"192.168.123.100 / 255.255.255.0 (System Settings → Network → the adapter → Details → TCP/IP).")
    if not name:
        return LinkReport(False, name, "no robot network interface configured." + hint, cands)
    iface = found.get(name)
    if iface is None:
        return LinkReport(False, name, f"interface {name} does not exist — the USB-Ethernet adapter is unplugged or "
                                       f"got another name (normal on macOS, not a robot fault).{hint}", cands)
    if not iface.active:
        return LinkReport(False, name, f"interface {name} exists but has no link (cable/adapter or robot off?).", cands)
    mine = iface.in_subnet(subnet)
    if not mine:
        have = ", ".join(iface.ipv4) or "no IPv4 address"
        return LinkReport(False, name, f"interface {name} is up but has {have}, not an address in {subnet}.{hint}",
                          cands)
    return LinkReport(True, name, f"{name} up with {mine[0]}", cands)


def ping(host: str, timeout_s: float = 1.0) -> bool:
    """One ICMP echo (no privileges needed with the system ping)."""
    if platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_s * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(round(timeout_s)))), host]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s + 2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
