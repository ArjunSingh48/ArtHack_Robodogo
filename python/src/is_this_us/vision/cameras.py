"""Which camera is which — by NAME, not by a fragile OpenCV index.

On a Mac, `webcam:0` is not necessarily the built-in camera: with Apple Continuity Camera the iPhone can take
index 0 (laggy, and not wanted for the show). OpenCV's AVFoundation backend numbers cameras by sorting
`[AVCaptureDevice devicesWithMediaType:Video] + [...Muxed]` by `uniqueID` (opencv/modules/videoio/src/
cap_avfoundation_mac.mm). We list the same devices the same way, so a name maps to the index OpenCV will open.

Name sources, best first:
* PyObjC (pyobjc-framework-AVFoundation, installed with the `vision` extra on macOS) — the exact list and order
  OpenCV uses ("avfoundation");
* `system_profiler SPCameraDataType` — names and unique IDs, sorted the same way ("system_profiler"); it may not
  list Continuity cameras, so an index from it is reported as unverified.

Camera specs understood by `make_frame_source`:
  macbook                the built-in camera (FaceTime HD / MacBook … Camera), never iPhone / Continuity / Desk View
  name:<part of a name>  e.g. name:FaceTime HD Camera — exactly one camera must match
  webcam:<index>         as before (no name check)
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

BUILTIN_HINTS = ("facetime", "macbook", "built-in", "builtin", "integrated")
NOT_BUILTIN = ("iphone", "ipad", "continuity", "desk view")


@dataclass(frozen=True)
class CameraInfo:
    index: int  # the OpenCV index (webcam:<index>)
    name: str
    unique_id: str = ""
    method: str = ""  # avfoundation | system_profiler | opencv-probe

    @property
    def is_builtin(self) -> bool:
        n = self.name.lower()
        return any(h in n for h in BUILTIN_HINTS) and not any(x in n for x in NOT_BUILTIN)

    @property
    def index_verified(self) -> bool:
        return self.method == "avfoundation"


class CameraSelectionError(RuntimeError):
    pass


def _avfoundation() -> list[tuple[str, str]] | None:
    try:
        import AVFoundation  # type: ignore  # pyobjc-framework-AVFoundation
    except Exception:
        return None
    devs = list(AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeVideo) or [])
    devs += list(AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeMuxed) or [])
    return [(str(d.localizedName()), str(d.uniqueID())) for d in devs]


def _system_profiler() -> list[tuple[str, str]] | None:
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType", "-json"], capture_output=True, text=True,
                             timeout=15).stdout
        items = json.loads(out).get("SPCameraDataType", [])
    except Exception:
        return None
    return [(str(i.get("_name", "?")), str(i.get("spcamera_unique-id", ""))) for i in items]


def order_like_opencv(devices: list[tuple[str, str]], method: str) -> list[CameraInfo]:
    """(name, uniqueID) pairs → CameraInfo in OpenCV's AVFoundation index order (sorted by uniqueID)."""
    ordered = sorted(devices, key=lambda d: d[1])
    return [CameraInfo(i, name, uid, method) for i, (name, uid) in enumerate(ordered)]


def list_cameras(*, platform: str | None = None) -> list[CameraInfo]:
    """The cameras with names and OpenCV indices. Off macOS: indices only (names unknown)."""
    platform = platform or sys.platform
    if platform == "darwin":
        for method, fn in (("avfoundation", _avfoundation), ("system_profiler", _system_profiler)):
            devs = fn()
            if devs:
                return order_like_opencv(devs, method)
        return []
    return [CameraInfo(i, f"camera {i}", method="opencv-probe") for i in _probe_indices()]


def _probe_indices(limit: int = 4) -> list[int]:
    try:
        import cv2
    except ImportError:
        return []
    found = []
    for i in range(limit):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
        cap.release()
    return found


def resolve(spec: str, cameras: list[CameraInfo] | None = None) -> CameraInfo:
    """`macbook` or `name:<part>` → the one matching camera. Never silently picks an iPhone / Continuity camera.

    Raises CameraSelectionError with what WAS found and how to choose."""
    cams = list_cameras() if cameras is None else cameras
    listing = "; ".join(f"{c.index}: {c.name}" for c in cams) or "no cameras listed"
    if spec == "macbook":
        matches = [c for c in cams if c.is_builtin]
        if not matches:
            raise CameraSelectionError(
                f"the built-in MacBook camera was not found (cameras: {listing}). Not falling back to another "
                f"camera (an iPhone via Continuity would be laggy). Check System Settings → Privacy & Security → "
                f"Camera, close apps that use the camera, or pick one with --camera-name \"…\" (see `itu camera-list`).")
    elif spec.startswith("name:"):
        want = spec.partition(":")[2].strip().lower()
        if not want:
            raise CameraSelectionError("name: needs part of a camera name, e.g. name:FaceTime HD Camera")
        matches = [c for c in cams if c.name.lower() == want] or [c for c in cams if want in c.name.lower()]
        if not matches:
            raise CameraSelectionError(f"no camera named like {want!r} (cameras: {listing})")
    else:
        raise CameraSelectionError(f"not a camera name spec: {spec!r}")
    if len(matches) > 1:
        raise CameraSelectionError(f"{spec!r} matches several cameras ({'; '.join(c.name for c in matches)}) — "
                                   f"use --camera-name with the full name")
    cam = matches[0]
    if not cam.index_verified:
        log.warning("camera %r: index %d comes from %s, not AVFoundation — install pyobjc-framework-AVFoundation "
                    "(`pip install -e '.[vision]'`) to be sure it is the same index OpenCV opens", cam.name, cam.index,
                    cam.method)
    return cam


def describe(cameras: list[CameraInfo]) -> list[str]:
    lines = []
    for c in cameras:
        tag = " ← built-in (used by --camera macbook)" if c.is_builtin else (
            " (iPhone / Continuity — not used for the show)" if any(x in c.name.lower() for x in NOT_BUILTIN) else "")
        lines.append(f"  webcam:{c.index}  {c.name}{tag}")
    return lines
