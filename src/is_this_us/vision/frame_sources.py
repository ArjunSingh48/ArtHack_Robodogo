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
    """Mode B (development): laptop webcam via OpenCV."""

    def __init__(self, index: int = 0):
        import cv2

        self.name = f"webcam:{index}"
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise OSError(f"Cannot open webcam {index}. On macOS, grant camera permission to your terminal.")

    def read(self) -> Frame | None:
        import cv2

        for _ in range(3):  # drop stale buffered frames
            self._cap.grab()
        ok, bgr = self._cap.read()
        if not ok:
            return None
        return Frame(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), self.name)

    def close(self) -> None:
        self._cap.release()


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


def make_frame_source(spec: str, *, network_interface: str = "") -> FrameSource:
    """Build a source from CAMERA_SOURCE: image:<path> | video:<path> | webcam[:i] | unitree."""
    kind, _, arg = spec.partition(":")
    if kind == "image":
        return ImageFileSource(arg)
    if kind == "video":
        return VideoFileSource(arg)
    if kind == "webcam":
        return WebcamSource(int(arg or 0))
    if kind == "unitree":
        return UnitreeFrontCameraSource(network_interface)
    raise ValueError(f"Unknown CAMERA_SOURCE {spec!r}")
