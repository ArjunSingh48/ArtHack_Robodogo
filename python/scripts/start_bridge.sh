#!/usr/bin/env bash
# Start the ZHAW go2_bridge for the real Go2 — with this repository's fixes (10 s SDK timeout, StopMove on exit).
# go2_main itself is never changed. Ctrl+C stops it.
#
#   python/scripts/start_bridge.sh en6          # the Ethernet interface wired to the Go2 (macOS: en*, Linux: eno1/eth0)
#
# GO2_MAIN     the go2_main checkout (default ~/Projects/go2-dog/go2_main), set up by its INSTALL.md
# GO2_PYTHON   its Python 3.11 with the Unitree SDK (default $GO2_MAIN/go2_py311/bin/python)
# GO2_SPORT_TIMEOUT_S  SDK timeout per action, seconds (default 10)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IFACE="${1:-${GO2_NETWORK_INTERFACE:-}}"
GO2_MAIN="${GO2_MAIN:-$HOME/Projects/go2-dog/go2_main}"
GO2_PYTHON="${GO2_PYTHON:-$GO2_MAIN/go2_py311/bin/python}"

fail() { echo "✗ $*" >&2; exit 1; }

[ -n "$IFACE" ] || fail "which network interface is wired to the Go2? usage: $0 <interface>  (e.g. en6)"
[ -d "$GO2_MAIN/go2_bridge" ] || fail "no go2_main checkout at $GO2_MAIN — clone github.com/zhaw-physical-ai/go2_main, follow its INSTALL.md, or set GO2_MAIN=/path/to/go2_main"
[ -x "$GO2_PYTHON" ] || fail "no Python at $GO2_PYTHON — create go2_main's venv (INSTALL.md) or set GO2_PYTHON"

if command -v ifconfig >/dev/null 2>&1; then
  ifconfig "$IFACE" >/dev/null 2>&1 || {
    echo "✗ interface $IFACE does not exist. Interfaces with a 192.168.123.x address:" >&2
    ifconfig 2>/dev/null | awk '/^[a-z]/{i=$1} /inet 192\.168\.123\./{print "   " i " " $2}' >&2
    exit 1
  }
  ifconfig "$IFACE" | grep -q "inet 192\.168\.123\." \
    || echo "! $IFACE has no 192.168.123.x address — is the Go2 plugged in and the address set?" >&2
elif command -v ip >/dev/null 2>&1; then
  ip link show "$IFACE" >/dev/null 2>&1 || fail "interface $IFACE does not exist (ip link)"
fi

PYTHONPATH="$GO2_MAIN${PYTHONPATH:+:$PYTHONPATH}" "$GO2_PYTHON" -c \
  "import unitree_sdk2py, go2_bridge, zmq" 2>/dev/null \
  || fail "$GO2_PYTHON cannot import unitree_sdk2py / go2_bridge / zmq — finish go2_main's INSTALL.md"

export GO2_NETWORK_INTERFACE="$IFACE"
export GO2_SPORT_TIMEOUT_S="${GO2_SPORT_TIMEOUT_S:-10}"
echo "✓ go2_bridge on $IFACE · SDK timeout ${GO2_SPORT_TIMEOUT_S} s · commands tcp://*:5555 · camera tcp://*:5556"
echo "  keep the Unitree remote in your hand · Ctrl+C stops the bridge (Move 0 → StopMove)"
cd "$GO2_MAIN"
PYTHONPATH="$GO2_MAIN${PYTHONPATH:+:$PYTHONPATH}" exec "$GO2_PYTHON" "$HERE/go2_bridge_patched.py"
