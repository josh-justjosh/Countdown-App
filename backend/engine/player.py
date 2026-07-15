from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Load, concatenate, and play WAV clips on a selected output device."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._device: int | None = None
        self._playing = False
        self._cancel = threading.Event()

    def list_devices(self) -> list[dict]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        result = []
        for index, dev in enumerate(devices):
            if int(dev["max_output_channels"]) <= 0:
                continue
            host = hostapis[dev["hostapi"]]["name"] if isinstance(dev["hostapi"], int) else ""
            result.append(
                {
                    "id": index,
                    "name": dev["name"],
                    "hostapi": host,
                    "channels": int(dev["max_output_channels"]),
                    "default_samplerate": float(dev["default_samplerate"]),
                    "is_default": index == sd.default.device[1]
                    if isinstance(sd.default.device, (list, tuple))
                    else index == sd.default.device,
                }
            )
        return result

    def set_device(self, device_id: int | None) -> None:
        self._device = device_id

    def stop(self) -> None:
        self._cancel.set()
        try:
            sd.stop()
        except Exception:
            pass
        self._playing = False

    def play_files(self, paths: list[Path], priority: float = 0.0, *, wait: bool = False) -> bool:
        """Concat and play WAV/audio files. Returns False if nothing to play."""
        if not paths:
            return False
        arrays: list[np.ndarray] = []
        samplerate = None
        channels = 1
        for path in paths:
            if not path.exists():
                logger.warning("Missing clip file: %s", path)
                continue
            data, sr = sf.read(str(path), always_2d=True, dtype="float32")
            if samplerate is None:
                samplerate = sr
                channels = data.shape[1]
            elif sr != samplerate:
                # Simple resample via linear interpolation of length
                duration = len(data) / sr
                new_len = int(duration * samplerate)
                x_old = np.linspace(0, 1, len(data), endpoint=False)
                x_new = np.linspace(0, 1, new_len, endpoint=False)
                resampled = np.zeros((new_len, data.shape[1]), dtype=np.float32)
                for ch in range(data.shape[1]):
                    resampled[:, ch] = np.interp(x_new, x_old, data[:, ch])
                data = resampled
            if data.shape[1] != channels:
                if data.shape[1] == 1 and channels > 1:
                    data = np.repeat(data, channels, axis=1)
                elif data.shape[1] > 1 and channels == 1:
                    data = np.mean(data, axis=1, keepdims=True)
            arrays.append(data)

        if not arrays or samplerate is None:
            return False

        audio = np.concatenate(arrays, axis=0)
        return self.play_array(audio, float(samplerate), priority=priority, wait=wait)

    def play_array(
        self,
        audio: np.ndarray,
        samplerate: float,
        priority: float = 0.0,
        *,
        interrupt: bool = True,
        wait: bool = False,
    ) -> bool:
        """Play a float32 PCM array (samples, channels).

        Higher urgency = lower priority number (reserved for callers).
        If interrupt is False, starts a parallel stream so existing playback continues.
        If wait is True, block until primary playback finishes (ignored for parallel).
        """
        if audio is None or audio.size == 0:
            return False
        if audio.ndim == 1:
            audio = audio.reshape(-1, 1)
        audio = np.asarray(audio, dtype=np.float32)

        if not interrupt:
            return self._play_parallel(audio, float(samplerate))

        self._cancel.clear()

        def _run() -> None:
            with self._lock:
                self._playing = True
                try:
                    sd.play(audio, samplerate=samplerate, device=self._device, blocking=True)
                except Exception as exc:
                    logger.error("Playback failed: %s", exc)
                finally:
                    self._playing = False

        if self._playing:
            self.stop()
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

    def _play_parallel(self, audio: np.ndarray, samplerate: float) -> bool:
        """Play without stopping the primary (countdown) stream."""
        channels = int(audio.shape[1])

        def _run() -> None:
            try:
                with sd.OutputStream(
                    samplerate=samplerate,
                    channels=channels,
                    dtype="float32",
                    device=self._device,
                ) as stream:
                    # Chunk so PortAudio stays happy on long buffers
                    frame = 0
                    n = len(audio)
                    chunk = max(int(samplerate * 0.25), 1024)
                    while frame < n:
                        end = min(frame + chunk, n)
                        stream.write(audio[frame:end])
                        frame = end
            except Exception as exc:
                logger.error("Parallel playback failed: %s", exc)

        threading.Thread(target=_run, daemon=True, name="audio-parallel").start()
        return True

    @property
    def is_playing(self) -> bool:
        return self._playing
