from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.engine.announcement import (
    continuous_sequence_fire,
    resolve_clips_for_threshold,
    sequence_thresholds,
    threshold_rows,
)
from backend.engine.player import AudioPlayer
from backend.models import Profile, resolve_announcement_voice_id
from backend.qlab_bridge import BridgeMediaSource, QLabBridge
from backend.settings import POLL_INTERVAL_SECONDS, THRESHOLD_WINDOW_SECONDS, VOICES_DIR
from backend.sources.base import MediaSource, RemainingTime
from backend.sources.vmix import VmixSource

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict], None]


class CountdownEngine:
    def __init__(
        self,
        player: AudioPlayer,
        voices_dir: Path | None = None,
        emit: EmitFn | None = None,
        qlab_bridge: QLabBridge | None = None,
        *,
        clips_dir: Path | None = None,  # legacy kw; ignored if voices_dir set
    ):
        self.player = player
        self.voices_dir = voices_dir or VOICES_DIR
        self.emit = emit or (lambda _e, _d: None)
        self.qlab_bridge = qlab_bridge
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._source: MediaSource | None = None
        self._profile: Profile | None = None
        self._armed = False
        self._fired: set[str] = set()
        self._had_media = False
        self._last: RemainingTime | None = None
        self._current_cue_id: str | None = None
        self._lock = threading.Lock()
        self._owns_source = False

    @property
    def armed(self) -> bool:
        return self._armed

    def status(self) -> dict[str, Any]:
        last = self._last
        return {
            "armed": self._armed,
            "remaining_seconds": last.seconds if last else None,
            "label": last.label if last else None,
            "connected": last.connected if last else False,
            "message": last.message if last else "",
            "playing": self.player.is_playing,
        }

    def arm(self, profile: Profile) -> dict[str, Any]:
        if self._armed:
            return {"success": False, "message": "Already armed"}
        source, owns = self._build_source(profile)
        if source is None:
            return {"success": False, "message": "Timed mode is not available yet"}
        test = source.test_connection()
        if not test.connected:
            if owns:
                source.close()
            return {"success": False, "message": test.message or "Connection failed"}

        self._source = source
        self._owns_source = owns
        self._profile = profile
        self._fired.clear()
        self._had_media = False
        self._stop.clear()
        self._armed = True
        self.player.set_device(profile.output_device_id)
        if self.qlab_bridge and profile.connection.mode == "qlab":
            self.qlab_bridge.apply_enabled(profile)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.emit("engine_status", self.status())
        return {"success": True, "message": "Armed"}

    def disarm(self) -> dict[str, Any]:
        self._stop.set()
        self._armed = False
        self.player.stop()
        if self._source:
            if self._owns_source:
                self._source.close()
            self._source = None
        self._owns_source = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self.emit("engine_status", self.status())
        return {"success": True, "message": "Disarmed"}

    def update_profile(self, profile: Profile) -> None:
        """Hot-update profile (e.g. enabled QLab cues) while armed."""
        self._profile = profile
        if self.qlab_bridge and profile.connection.mode == "qlab":
            self.qlab_bridge.apply_enabled(profile)

    def _build_source(self, profile: Profile) -> tuple[MediaSource | None, bool]:
        conn = profile.connection
        if conn.mode == "vmix":
            return (
                VmixSource(
                    ip=conn.vmix_ip,
                    port=conn.vmix_port,
                    fallback1=conn.vmix_fallback1,
                    fallback2=conn.vmix_fallback2,
                ),
                True,
            )
        if conn.mode == "qlab":
            if self.qlab_bridge and self.qlab_bridge.connected:
                self.qlab_bridge.apply_enabled(profile)
                return BridgeMediaSource(self.qlab_bridge), False
            # Auto-connect bridge if available
            if self.qlab_bridge:
                result = self.qlab_bridge.connect(conn, profile)
                if not result.get("success"):
                    return None, False
                return BridgeMediaSource(self.qlab_bridge), False
            return None, False
        return None, False

    def _clip_paths(self, voice_id: str, clip_ids: list[str]) -> list[Path]:
        paths = []
        voice_dir = self.voices_dir / voice_id
        for cid in clip_ids:
            path = voice_dir / f"{cid}.wav"
            if path.exists():
                paths.append(path)
            else:
                logger.warning("Clip missing at play time: %s/%s", voice_id, cid)
        return paths

    def _fire_threshold(self, threshold: float, fire_key: str) -> None:
        if not self._profile:
            return
        if fire_key in self._fired:
            return
        row = None
        for r in self._profile.schedule:
            if not r.enabled or r.row_type != "threshold":
                continue
            if r.threshold_seconds is None:
                continue
            if abs(r.threshold_seconds - threshold) > 0.01:
                continue
            row = r
            break
        clips = resolve_clips_for_threshold(self._profile.schedule, threshold)
        voice_id = resolve_announcement_voice_id(
            row, self._profile, cue_id=self._current_cue_id
        )
        paths = self._clip_paths(voice_id, clips)
        if paths:
            self.player.play_files(paths, priority=threshold)
            self._fired.add(fire_key)
            self.emit(
                "announcement",
                {"threshold": threshold, "clips": clips, "voice_id": voice_id},
            )

    def _fire_continuous_sequence(self, start: float, clips: list[str], row_id: str) -> None:
        fire_key = f"row:{row_id}"
        if fire_key in self._fired:
            return
        if not self._profile:
            return
        seq_row = next((r for r in self._profile.schedule if r.id == row_id), None)
        voice_id = resolve_announcement_voice_id(
            seq_row, self._profile, cue_id=self._current_cue_id
        )
        paths = self._clip_paths(voice_id, clips)
        if paths:
            self.player.play_files(paths, priority=start)
            self._fired.add(fire_key)
            self.emit(
                "announcement",
                {"threshold": start, "clips": clips, "voice_id": voice_id},
            )

    def _fire_sequence(self, seconds: float, clip_id: str) -> None:
        fire_key = f"seq:{clip_id}"
        if fire_key in self._fired:
            return
        if not self._profile:
            return
        seq_row = next(
            (r for r in self._profile.schedule if r.row_type == "sequence" and r.enabled),
            None,
        )
        voice_id = resolve_announcement_voice_id(
            seq_row, self._profile, cue_id=self._current_cue_id
        )
        paths = self._clip_paths(voice_id, [clip_id])
        if paths:
            self.player.play_files(paths, priority=seconds)
            self._fired.add(fire_key)
            self.emit(
                "announcement",
                {"threshold": seconds, "clips": [clip_id], "voice_id": voice_id},
            )

    def _handle_remaining(self, remaining: float, prev: float | None) -> None:
        if not self._profile:
            return
        for threshold, row_id in threshold_rows(self._profile.schedule):
            crossed = (
                prev is not None
                and prev > threshold
                and remaining <= threshold
            )
            entered_window = (
                prev is not None
                and prev > threshold + THRESHOLD_WINDOW_SECONDS
                and abs(remaining - threshold) <= THRESHOLD_WINDOW_SECONDS
            )
            if crossed or entered_window:
                self._fire_threshold(threshold, f"row:{row_id}")

        continuous = continuous_sequence_fire(self._profile.schedule)
        if continuous:
            start, clips, row_id = continuous
            crossed = prev is not None and prev > start and remaining <= start
            entered_window = (
                prev is not None
                and prev > start + THRESHOLD_WINDOW_SECONDS
                and abs(remaining - start) <= THRESHOLD_WINDOW_SECONDS
            )
            if crossed or entered_window:
                self._fire_continuous_sequence(start, clips, row_id)

        for seq_sec, clip_id in sequence_thresholds(self._profile.schedule):
            crossed = prev is not None and prev > seq_sec and remaining <= seq_sec
            entered_window = (
                prev is not None
                and prev > seq_sec + THRESHOLD_WINDOW_SECONDS
                and abs(remaining - seq_sec) <= THRESHOLD_WINDOW_SECONDS
            )
            if crossed or entered_window:
                self._fire_sequence(seq_sec, clip_id)

    def _loop(self) -> None:
        prev_seconds: float | None = None
        while not self._stop.is_set() and self._source:
            try:
                result = self._source.get_remaining()
            except Exception as exc:
                logger.error("Poll error: %s", exc)
                result = RemainingTime(None, None, False, str(exc))

            self._last = result
            self._current_cue_id = result.cue_id if result.connected else None
            payload = {
                **self.status(),
                "timestamp": time.time(),
            }
            self.emit("countdown_tick", payload)

            if not result.connected:
                # Keep session state across brief link drops; avoid false crossings on resume
                prev_seconds = None
                self._current_cue_id = None
            elif result.seconds is None:
                if self._had_media:
                    self._fired.clear()
                    self._had_media = False
                    prev_seconds = None
                    self._current_cue_id = None
                    self.emit("countdown_reset", {"reason": "no_media"})
            else:
                if not self._had_media:
                    # Fresh media — do not speak catch-up for thresholds already passed
                    self._had_media = True
                    self._fired.clear()
                    for threshold, row_id in threshold_rows(
                        self._profile.schedule if self._profile else []
                    ):
                        if result.seconds < threshold - THRESHOLD_WINDOW_SECONDS:
                            self._fired.add(f"row:{row_id}")
                    continuous = continuous_sequence_fire(
                        self._profile.schedule if self._profile else []
                    )
                    if continuous:
                        start, _clips, row_id = continuous
                        if result.seconds < start - THRESHOLD_WINDOW_SECONDS:
                            self._fired.add(f"row:{row_id}")
                    for seq_sec, clip_id in sequence_thresholds(
                        self._profile.schedule if self._profile else []
                    ):
                        if result.seconds < seq_sec - THRESHOLD_WINDOW_SECONDS:
                            self._fired.add(f"seq:{clip_id}")
                else:
                    self._handle_remaining(result.seconds, prev_seconds)
                prev_seconds = result.seconds

            time.sleep(POLL_INTERVAL_SECONDS)
