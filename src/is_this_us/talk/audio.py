"""Microphone in, speaker out — 24 kHz mono PCM16, the Realtime API's native format.

Uses `sounddevice` (PortAudio). Works with the laptop's mic/speaker or a USB/Bluetooth
speakerphone placed near the robot (the Go2 Python SDK has no audio API).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

RATE = 24_000
BLOCK = 480  # 20 ms
BYTES_PER_SAMPLE = 2


class AudioIO(Protocol):
    def start(self, on_mic: Callable[[bytes], None]) -> None: ...
    def play(self, pcm16: bytes) -> None: ...
    def clear(self) -> None: ...
    def is_playing(self) -> bool: ...
    def close(self) -> None: ...


class SoundDeviceAudio:
    """Full-duplex audio with a playback buffer; `tail_s` keeps the mic muted briefly after
    the dog stops speaking (room echo), when half-duplex mode is used."""

    def __init__(self, input_device=None, output_device=None, tail_s: float = 0.4):
        try:
            import sounddevice  # noqa: F401
        except (ImportError, OSError) as exc:
            raise RuntimeError("Audio needs `sounddevice` (pip install -e '.[talk]') and PortAudio.") from exc
        self.input_device, self.output_device, self.tail_s = input_device, output_device, tail_s
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_audio = 0.0
        self._in = self._out = None

    def start(self, on_mic: Callable[[bytes], None]) -> None:
        import sounddevice as sd

        def mic_cb(indata, frames, t, status):
            on_mic(bytes(indata))

        def spk_cb(outdata, frames, t, status):
            n = frames * BYTES_PER_SAMPLE
            with self._lock:
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
            if chunk:
                self._last_audio = time.monotonic()
            outdata[:] = chunk + b"\x00" * (n - len(chunk))

        self._in = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                     device=self.input_device, callback=mic_cb)
        self._out = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                       device=self.output_device, callback=spk_cb)
        self._out.start()
        self._in.start()

    def play(self, pcm16: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm16)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def is_playing(self) -> bool:
        with self._lock:
            pending = len(self._buf) > 0
        return pending or (time.monotonic() - self._last_audio) < self.tail_s

    def close(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                s.stop()
                s.close()


def list_devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())
