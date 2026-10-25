"""Frame sources (camera modes A/B/C). Decoupled from pose estimation.

All sources return RGB `numpy.ndarray` frames. Frames are kept in memory only.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Frame:
    image: np.ndarray  # H x W x 3, RGB, uint8
    source: str
    timestamp: float = field(default_factory=time.time)


class FrameSource(Protocol):
    name: str

    def read(self) -> Frame | None: ...
    def close(self) -> None: ...


def decode_image_bytes(data: bytes) -> np.ndarray:
    """Decode JPEG/PNG bytes to an RGB array without touching the disk."""
    from io import BytesIO

    from PIL import Image, ImageOps

    with Image.open(BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        return np.asarray(im).copy()


class ImageFileSource:
    """Mode A (simulation): a single stored photo, returned on every read."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.name = f"image:{self.path.name}"
        self._image = decode_image_bytes(self.path.read_bytes())

    def read(self) -> Frame | None:
        return Frame(self._image, self.name)

    def close(self) -> None:
        pass


class VideoFileSource:
    """Mode A (simulation): a stored video, looping."""

    def __init__(self, path: str | Path):
        import cv2

        self.path = Path(path)
        self.name = f"video:{self.path.name}"
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise OSError(f"Cannot open video {self.path}")

    def read(self) -> Frame | None:
        import cv2

        ok, bgr = self._cap.read()
        if not ok:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, bgr = self._cap.read()
            if not ok:
                return None
        return Frame(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), self.name)

    def close(self) -> None:
        self._cap.release()


class WebcamSource:
    """Mode B: a local camera via OpenCV, read by ONE background thread that keeps only the newest frame.

    read() never waits for the camera's buffer (no stale frames, no queue): it returns the latest frame (waiting
    only for the very first one). The camera is opened once and never reopened. The OpenCV capture object is
    touched by that one thread only."""

    FIRST_FRAME_TIMEOUT_S = 5.0

    def __init__(self, index: int = 0, *, label: str = "", width: int = 1280, height: int = 720, fps: int = 30,
                 capture=None):
        import threading

        self.index = index
        self.label = label or f"webcam:{index}"
        self.name = f"webcam:{index}"
        if capture is None:
            import cv2

            capture = cv2.VideoCapture(index)
            if capture.isOpened():
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                capture.set(cv2.CAP_PROP_FPS, fps)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = capture
        if not self._cap.isOpened():
            raise OSError(f"Cannot open webcam {index}. On macOS, grant camera permission to your terminal.")
        self._latest = None  # (bgr, monotonic time)
        self._cond = threading.Condition()
        self._running = True
        self.frames = 0
        self._thread = threading.Thread(target=self._grab, daemon=True, name=f"camera-{index}")
        self._thread.start()

    def _grab(self) -> None:
        import time as _t

        while self._running:
            ok, bgr = self._cap.read()
            if not ok:
                _t.sleep(0.02)
                continue
            with self._cond:
                self._latest = (bgr, _t.monotonic())
                self.frames += 1
                self._cond.notify_all()

    def info(self) -> dict:
        """What actually runs: size and frame rate as delivered (measured), for the log."""
        import time as _t

        with self._cond:
            self._cond.wait_for(lambda: self._latest is not None, timeout=self.FIRST_FRAME_TIMEOUT_S)
            n0, t0 = self.frames, _t.monotonic()
        _t.sleep(1.0)
        with self._cond:
            n1, latest = self.frames, self._latest
        h, w = (latest[0].shape[:2] if latest is not None else (0, 0))
        return {"label": self.label, "index": self.index, "width": int(w), "height": int(h),
                "fps": round((n1 - n0) / max(_t.monotonic() - t0, 1e-6), 1)}

    def read(self) -> Frame | None:
        import cv2

        with self._cond:
            if self._latest is None:
                self._cond.wait_for(lambda: self._latest is not None, timeout=self.FIRST_FRAME_TIMEOUT_S)
            latest = self._latest
        if latest is None:
            return None
        return Frame(cv2.cvtColor(latest[0], cv2.COLOR_BGR2RGB), self.name)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1.0)
        self._cap.release()


def open_named_camera(spec: str, cameras=None) -> WebcamSource:
    """`macbook` / `name:<part>` → the camera with that name (see vision/cameras.py), logged with its size + fps."""
    from .cameras import resolve

    cam = resolve(spec, cameras)
    src = WebcamSource(cam.index, label=cam.name)
    src.camera_name = cam.name
    log.info("camera %s → %r at index %d (%s)", spec, cam.name, cam.index, cam.method)
    return src


class UnitreeFrontCameraSource:
    """Mode C (real robot): Go2 front camera through the official SDK.

    HARDWARE-UNVERIFIED. Based on `example/go2/front_camera/capture_image.py` in
    unitreerobotics/unitree_sdk2_python: `VideoClient().GetImageSample()` returns
    `(code, jpeg_bytes)`. Requires DDS access to the robot (typically Go2 EDU, wired).
    """

    def __init__(self, network_interface: str = ""):
        from ..robot.unitree_dds import init_dds

        init_dds(network_interface)
        from unitree_sdk2py.go2.video.video_client import VideoClient  # type: ignore

        self._client = VideoClient()
        self._client.SetTimeout(3.0)
        self._client.Init()
        self.name = "unitree:front_camera"

    def read(self) -> Frame | None:
        code, data = self._client.GetImageSample()
        if code != 0 or not data:
            log.warning("Unitree camera returned code %s", code)
            return None
        return Frame(decode_image_bytes(bytes(data)), self.name)

    def close(self) -> None:
        pass


def make_frame_source(spec: str, *, network_interface: str = "", timeout_ms: int | None = None) -> FrameSource:
    """Build a source from CAMERA_SOURCE: image:<path> | video:<path> | webcam[:i] | macbook | name:<camera name> |
    unitree | zhaw_bridge[:host[:port]]. `timeout_ms`: how long a bridge camera read may wait for a frame."""
    kind, _, arg = spec.partition(":")
    if kind == "image":
        return ImageFileSource(arg)
    if kind == "video":
        return VideoFileSource(arg)
    if kind == "webcam":
        return WebcamSource(int(arg or 0))
    if kind in ("macbook", "name"):
        return open_named_camera(spec)
    if kind == "unitree":
        return UnitreeFrontCameraSource(network_interface)
    if kind == "zhaw_bridge":  # zhaw_bridge[:host[:port]]
        from ..robot.zhaw_bridge import ZhawBridgeCameraSource

        host, _, port = arg.partition(":")
        return ZhawBridgeCameraSource(host or "localhost", int(port or 5556), timeout_ms=timeout_ms or 3000)
    raise ValueError(f"Unknown CAMERA_SOURCE {spec!r}")
