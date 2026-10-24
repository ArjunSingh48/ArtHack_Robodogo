"""Single DDS initialisation shared by the Go2 adapter and the Go2 camera.

`ChannelFactoryInitialize` must be called once per process; calling it from both
the camera and the motion adapter would fail or bind two interfaces.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_initialized_iface: str | None = None


def init_dds(network_interface: str = "") -> None:
    global _initialized_iface
    with _lock:
        if _initialized_iface is not None:
            if network_interface and network_interface != _initialized_iface:
                raise RuntimeError(
                    f"DDS already initialised on {_initialized_iface!r}, cannot switch to {network_interface!r}"
                )
            return
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # type: ignore
        except ImportError as exc:
            raise RuntimeError("unitree_sdk2py is not installed. See docs/HARDWARE_SETUP.md.") from exc
        if network_interface:
            ChannelFactoryInitialize(0, network_interface)
        else:
            ChannelFactoryInitialize(0)
        _initialized_iface = network_interface or "<default>"
