from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from backend.models import ConnectionConfig, Profile
from backend.sources.base import RemainingTime
from backend.sources.qlab import QLabSource

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict], None]


class QLabBridge:
    """
    Shared QLab connection for cue browsing (while connected) and countdown polling.
    Only one OSC listen socket is used for the whole app.
    """

    def __init__(self, emit: EmitFn | None = None):
        self.emit = emit or (lambda _e, _d: None)
        self._lock = threading.RLock()
        self._source: QLabSource | None = None
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cues: list[dict] = []
        self._message = ""
        self._conn_key: tuple | None = None

    @property
    def connected(self) -> bool:
        return self._connected and self._source is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "message": self._message,
                "cues": list(self._cues),
                "cue_count": len(self._cues),
            }

    def apply_enabled(self, profile: Profile) -> None:
        with self._lock:
            if not self._source:
                return
            if not profile.connection.qlab_cues_initialized:
                self._source.set_enabled_cue_ids(None)
            else:
                self._source.set_enabled_cue_ids(set(profile.connection.qlab_enabled_cue_ids))

    def connect(self, conn: ConnectionConfig, profile: Profile | None = None) -> dict[str, Any]:
        key = (conn.qlab_ip, conn.qlab_send_port, conn.qlab_listen_port)

        with self._lock:
            if self._source and self._conn_key == key and self._connected:
                already = True
            else:
                already = False

        if already:
            if profile:
                self.apply_enabled(profile)
            status = self.status()
            self.emit("qlab_status", status)
            return {"success": True, "message": status.get("message") or "Already connected", **status}

        # Tear down any previous session without holding the lock across joins
        self._teardown()

        source = QLabSource(
            ip=conn.qlab_ip,
            send_port=conn.qlab_send_port,
            listen_port=conn.qlab_listen_port,
            passcode=getattr(conn, "qlab_passcode", "") or "",
        )
        if profile:
            if not profile.connection.qlab_cues_initialized:
                source.set_enabled_cue_ids(None)
            else:
                source.set_enabled_cue_ids(set(profile.connection.qlab_enabled_cue_ids))

        try:
            result = source.test_connection()
        except Exception as exc:
            logger.exception("QLab connect failed")
            source.close()
            with self._lock:
                self._connected = False
                self._message = str(exc)
            status = self.status()
            self.emit("qlab_status", status)
            return {"success": False, "message": str(exc), **status}

        if not result.connected:
            source.close()
            with self._lock:
                self._connected = False
                self._message = result.message
            status = self.status()
            self.emit("qlab_status", status)
            return {"success": False, "message": result.message, **status}

        with self._lock:
            self._source = source
            self._conn_key = key
            self._connected = True
            self._message = result.message or "Connected"
            self._cues = source.get_catalog()
            self._stop.clear()
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._thread.start()

        status = self.status()
        self.emit("qlab_status", status)
        return {"success": True, "message": status.get("message") or "Connected", **status}

    def disconnect(self) -> dict[str, Any]:
        self._teardown()
        with self._lock:
            self._message = "Disconnected"
        status = self.status()
        self.emit("qlab_status", status)
        return {"success": True, "message": "Disconnected", **status}

    def _teardown(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
            source = self._source
            self._source = None
            self._connected = False
            self._cues = []
            self._conn_key = None
        if source:
            try:
                source.close()
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def refresh(self, profile: Profile | None = None) -> dict[str, Any]:
        if profile:
            self.apply_enabled(profile)
        with self._lock:
            if not self._source or not self._connected:
                return {"success": False, "message": "Not connected", **self.status()}
            source = self._source
        try:
            cues = source.refresh_catalog()
        except Exception as exc:
            logger.error("QLab catalog refresh failed: %s", exc)
            return {"success": False, "message": str(exc), **self.status()}
        with self._lock:
            self._cues = cues
            self._message = f"Connected · {len(cues)} cue(s) with duration"
        status = self.status()
        self.emit("qlab_status", status)
        return {"success": True, "message": status.get("message"), **status}

    def get_remaining(self) -> RemainingTime:
        with self._lock:
            source = self._source
            connected = self._connected
        if not source or not connected:
            return RemainingTime(None, None, False, "QLab not connected")
        return source.get_remaining()

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                source = self._source
                connected = self._connected
            if not source or not connected:
                break
            try:
                cues = source.refresh_catalog()
                with self._lock:
                    self._cues = cues
                    if self._connected:
                        self._message = f"Connected · {len(cues)} cue(s) with duration"
                self.emit("qlab_status", self.status())
            except Exception as exc:
                logger.debug("QLab monitor refresh: %s", exc)
            for _ in range(40):
                if self._stop.is_set():
                    break
                time.sleep(0.1)


class BridgeMediaSource:
    """Adapter so CountdownEngine can poll the shared QLab bridge."""

    def __init__(self, bridge: QLabBridge):
        self.bridge = bridge

    def test_connection(self) -> RemainingTime:
        status = self.bridge.status()
        if status["connected"]:
            return RemainingTime(None, None, True, status.get("message") or "Connected")
        return RemainingTime(None, None, False, status.get("message") or "Not connected")

    def get_remaining(self) -> RemainingTime:
        return self.bridge.get_remaining()

    def close(self) -> None:
        return None
