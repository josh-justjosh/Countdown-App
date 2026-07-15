from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections import defaultdict
from typing import Any

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from backend.sources.base import MediaSource, RemainingTime

logger = logging.getLogger(__name__)

META_KEYS = {"_meta", "_json", "playhead"}


def _parse_json_arg(args: list) -> Any:
    if not args:
        return None
    raw = args[0]
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _unwrap_qlab_payload(parsed: Any) -> Any:
    """QLab often returns {status, data, address} — pull out data when present."""
    if isinstance(parsed, dict) and "status" in parsed:
        status = parsed.get("status")
        if status in ("error", "denied", "badpass"):
            return parsed
        if "data" in parsed:
            return parsed.get("data")
    return parsed


def _is_error_payload(parsed: Any) -> bool:
    return isinstance(parsed, dict) and parsed.get("status") in ("error", "denied", "badpass")


def _failure_detail(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return str(parsed)
    status = parsed.get("status")
    data = parsed.get("data")
    if status == "denied":
        return "denied — send /connect first, or enable View under No Passcode in OSC Access"
    if status == "badpass":
        return "incorrect OSC passcode"
    if data not in (None, ""):
        return str(data)
    return f"OSC {status or 'error'}"


class QLabSource(MediaSource):
    """QLab OSC client: catalog cues with duration, track remaining for enabled running cues."""

    def __init__(
        self,
        ip: str,
        send_port: int = 53000,
        listen_port: int = 53001,
        listen_ip: str = "0.0.0.0",
        enabled_cue_ids: set[str] | None = None,
        passcode: str = "",
    ):
        self.ip = ip
        self.send_port = send_port
        self.listen_port = listen_port
        self.listen_ip = listen_ip
        self.enabled_cue_ids: set[str] | None = enabled_cue_ids
        self.passcode = (passcode or "").strip()
        self._state: dict[str, dict] = defaultdict(dict)
        self._json_replies: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bind_ok = False
        self._peer_connected = False
        self._catalog: list[dict] = []
        self._session_ok = False
        self._start()

    def set_enabled_cue_ids(self, ids: set[str] | None) -> None:
        self.enabled_cue_ids = ids

    def _start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.listen_ip, self.listen_port))
            # Connect the datagram socket to QLab so the OS (esp. Windows Firewall)
            # associates inbound UDP replies with this outbound peer. Without this,
            # ping can succeed while OSC replies are silently dropped.
            try:
                sock.connect((self.ip, self.send_port))
                self._peer_connected = True
            except OSError as exc:
                logger.warning(
                    "Could not connect UDP peer %s:%s (%s); falling back to sendto",
                    self.ip,
                    self.send_port,
                    exc,
                )
                self._peer_connected = False
            sock.settimeout(0.2)
            self._sock = sock
            self._bind_ok = True
            self._thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._thread.start()
            logger.info(
                "QLab OSC socket bound on %s:%s → %s:%s",
                self.listen_ip,
                self.listen_port,
                self.ip,
                self.send_port,
            )
        except OSError as exc:
            logger.error("Failed to bind QLab listen port %s: %s", self.listen_port, exc)
            self._sock = None
            self._bind_ok = False
            self._peer_connected = False

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                if self._peer_connected:
                    data = self._sock.recv(65535)
                else:
                    data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = OscMessage(data)
                self._handle(msg.address, list(msg.params))
            except Exception as exc:
                logger.debug("OSC parse error: %s", exc)

    def _handle(self, address: str, args: list) -> None:
        parts = [p for p in address.strip("/").split("/") if p]
        if not parts or parts[0] != "reply":
            return

        parsed = _unwrap_qlab_payload(_parse_json_arg(args))

        # /reply/connect or /reply/workspace/{id}/connect
        if parts[-1] == "connect":
            with self._lock:
                self._state["_meta"]["connect"] = parsed if not _is_error_payload(parsed) else parsed
                if _is_error_payload(parsed):
                    self._state["_meta"]["last_error"] = parsed
                self._state["_meta"]["last_updated"] = time.time()
            return

        # /reply/workspace/{id}/cueLists  or /reply/cueLists
        if "cueLists" in parts or parts[-1] in (
            "selectedCues",
            "runningCues",
            "runningOrPausedCues",
            "workspaces",
            "uniqueIDs",
        ):
            key = "uniqueIDs" if parts[-1] == "uniqueIDs" else parts[-1]
            # runningCues/uniqueIDs → store under runningCues
            if len(parts) >= 2 and parts[-1] == "uniqueIDs":
                key = parts[-2]
            with self._lock:
                if _is_error_payload(parsed):
                    self._json_replies[key] = None
                    self._state["_meta"]["last_error"] = parsed
                else:
                    self._json_replies[key] = parsed
                self._state["_meta"]["last_updated"] = time.time()
            return

        if len(parts) >= 2 and parts[1] not in ("cue", "cue_id"):
            # /reply/version etc.
            with self._lock:
                if _is_error_payload(parsed):
                    self._state["_meta"][parts[1]] = parsed
                else:
                    self._state["_meta"][parts[1]] = parsed if parsed is not None else (args[0] if args else True)
                self._state["_meta"]["last_updated"] = time.time()
            return

        if len(parts) < 4:
            return

        # /reply/cue/{id}/{prop} or /reply/cue_id/{id}/{prop}
        kind = parts[1]
        if kind not in ("cue", "cue_id"):
            return

        with self._lock:
            if parts[2] == "active" and parts[3] in ("uid", "uniqueID"):
                for data in self._state.values():
                    if isinstance(data, dict):
                        data["active"] = False
                values = parsed if isinstance(parsed, list) else list(args)
                for uid in values:
                    key = str(uid)
                    self._state[key]["active"] = True
                    self._state[key]["last_updated"] = time.time()
                return

            cue_key = parts[2]
            prop = parts[3]
            value = parsed if parsed is not None else (args[0] if args else None)
            if _is_error_payload(value):
                return
            if value is not None:
                self._state[cue_key][prop] = value
                self._state[cue_key]["last_updated"] = time.time()

    def _send(self, address: str, *args) -> None:
        if not self._sock:
            return
        builder = OscMessageBuilder(address=address)
        for arg in args:
            builder.add_arg(arg)
        packet = builder.build().dgram
        try:
            if self._peer_connected:
                self._sock.send(packet)
            else:
                self._sock.sendto(packet, (self.ip, self.send_port))
        except OSError as exc:
            logger.debug("OSC send failed: %s", exc)

    def _get(self, address: str) -> None:
        """Read from QLab: send address with no arguments (QLab 5 style)."""
        self._send(address)

    def _ensure_session(self) -> tuple[bool, str]:
        """
        Establish OSC access: keep-alive + connect to open workspaces.
        Required in QLab 5 before most workspace/cue messages are accepted.
        """
        if not self._bind_ok or not self._sock:
            return False, f"OSC listen port {self.listen_port} unavailable"

        # Ask QLab to remember this UDP client and always reply
        self._send("/udpKeepAlive", 1)
        self._send("/alwaysReply", 1)
        time.sleep(0.05)

        with self._lock:
            self._json_replies.pop("workspaces", None)
            self._state["_meta"].pop("connect", None)
            self._state["_meta"].pop("last_error", None)

        self._get("/workspaces")
        time.sleep(0.2)
        with self._lock:
            workspaces = self._json_replies.get("workspaces")
            err = self._state["_meta"].get("last_error")

        if _is_error_payload(workspaces):
            return False, _failure_detail(workspaces)
        if _is_error_payload(err) and workspaces is None:
            # Try bare connect then retry workspaces
            pass

        ws_list: list[dict] = []
        if isinstance(workspaces, list):
            ws_list = [w for w in workspaces if isinstance(w, dict)]

        if ws_list:
            for ws in ws_list:
                uid = ws.get("uniqueID")
                if not uid:
                    continue
                if self.passcode:
                    self._send(f"/workspace/{uid}/connect", self.passcode)
                else:
                    self._send(f"/workspace/{uid}/connect")
                time.sleep(0.08)
        else:
            # Frontmost / all workspaces listening on this port
            if self.passcode:
                self._send("/connect", self.passcode)
            else:
                self._send("/connect")
            time.sleep(0.12)

        # Confirm with a simple application read
        with self._lock:
            self._state["_meta"].pop("version", None)
            self._state["_meta"].pop("last_error", None)
        self._get("/version")
        time.sleep(0.15)
        with self._lock:
            version = self._state["_meta"].get("version")
            connect_reply = self._state["_meta"].get("connect")
            last_error = self._state["_meta"].get("last_error")

        if _is_error_payload(version):
            detail = _failure_detail(version)
            if version.get("status") == "denied" or "denied" in detail.lower():
                return False, (
                    f"{detail}. In QLab: Workspace Settings → Network → OSC Access — "
                    "enable View for “No Passcode” (or enter the passcode in this app)."
                )
            return False, f"QLab OSC error: {detail}"

        if _is_error_payload(connect_reply):
            return False, f"QLab connect failed: {_failure_detail(connect_reply)}"

        if version is None and last_error is None and not ws_list:
            return False, (
                f"No OSC reply from {self.ip}:{self.send_port}. "
                "QLab uses UDP (ping is ICMP and can succeed while OSC fails). "
                "On Windows, allow VT Vocal Countdown through Defender Firewall "
                "for Private networks, or add an inbound rule for UDP port "
                f"{self.listen_port} (replies) and outbound UDP {self.send_port}."
            )

        self._session_ok = True
        ver_txt = version if version is not None else "?"
        return True, f"QLab reachable (version: {ver_txt})"

    def close(self) -> None:
        try:
            if self._sock and self._session_ok:
                self._send("/udpKeepAlive", 0)
        except Exception:
            pass
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._session_ok = False

    @staticmethod
    def _flatten_cue_tree(nodes: Any, out: list[dict], parent_list: str = "") -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            uid = str(node.get("uniqueID") or "")
            cue_type = str(node.get("type") or "")
            name = str(node.get("listName") or node.get("name") or "")
            number = str(node.get("number") or "")
            entry = {
                "unique_id": uid,
                "number": number,
                "name": name,
                "type": cue_type,
                "parent_list": parent_list,
            }
            if uid and cue_type.lower() not in ("cue list", "cart", ""):
                # Keep groups and leaf cues; filter by duration later
                out.append(entry)
            list_label = name or number or parent_list
            children = node.get("cues")
            if isinstance(children, list):
                QLabSource._flatten_cue_tree(children, out, parent_list=list_label)

    def get_catalog(self) -> list[dict]:
        with self._lock:
            return list(self._catalog)

    def _compute_remaining(
        self, data: dict, fallback_name: str, *, cue_id: str | None = None
    ) -> RemainingTime | None:
        try:
            duration = float(data["duration"]) if "duration" in data else None
            elapsed = float(data["actionElapsed"]) if "actionElapsed" in data else None
        except (TypeError, ValueError):
            return None
        if duration is None or elapsed is None or duration <= 0:
            return None
        remaining = max(0.0, duration - elapsed)
        name = str(data.get("name") or fallback_name)
        return RemainingTime(remaining, name, True, "QLab", cue_id=cue_id)

    def _is_enabled(self, uid: str) -> bool:
        if self.enabled_cue_ids is None:
            return True
        return uid in self.enabled_cue_ids

    def _poll_once(self) -> RemainingTime:
        if not self._bind_ok or not self._sock:
            return RemainingTime(
                None, None, False, f"OSC listen port {self.listen_port} unavailable"
            )
        if not self._session_ok:
            ok, message = self._ensure_session()
            if not ok:
                return RemainingTime(None, None, False, message)

        with self._lock:
            self._json_replies.pop("runningOrPausedCues", None)
            self._json_replies.pop("runningCues", None)

        self._get("/runningOrPausedCues/uniqueIDs")
        self._get("/runningCues/uniqueIDs")
        self._get("/cue/active/uniqueID")
        time.sleep(0.08)

        with self._lock:
            running = self._json_replies.get("runningCues")
            running_or_paused = self._json_replies.get("runningOrPausedCues")
            state = {k: dict(v) for k, v in self._state.items()}

        candidate_ids: list[str] = []
        for raw in (running, running_or_paused):
            if isinstance(raw, list):
                for uid in raw:
                    sid = str(uid)
                    if sid not in candidate_ids:
                        candidate_ids.append(sid)

        active_uids = [
            uid
            for uid, data in state.items()
            if data.get("active") and uid not in META_KEYS
        ]
        for uid in active_uids:
            if uid not in candidate_ids:
                candidate_ids.append(uid)

        # Prefer enabled candidates only
        enabled_candidates = [uid for uid in candidate_ids if self._is_enabled(uid)]

        # Also consider catalog cues marked running that are enabled
        with self._lock:
            catalog = list(self._catalog)
        for cue in catalog:
            uid = cue["unique_id"]
            if cue.get("is_running") and self._is_enabled(uid) and uid not in enabled_candidates:
                enabled_candidates.append(uid)

        for uid in enabled_candidates:
            self._get(f"/cue_id/{uid}/type")
            self._get(f"/cue_id/{uid}/duration")
            self._get(f"/cue_id/{uid}/actionElapsed")
            self._get(f"/cue_id/{uid}/name")
            self._get(f"/cue_id/{uid}/isRunning")
        if enabled_candidates:
            time.sleep(0.1)

        with self._lock:
            state = {k: dict(v) for k, v in self._state.items()}

        best: RemainingTime | None = None
        for uid in enabled_candidates:
            data = state.get(uid, {})
            # Must be running (or have elapsed progress)
            is_running = bool(data.get("isRunning"))
            try:
                elapsed = float(data.get("actionElapsed") or 0)
            except (TypeError, ValueError):
                elapsed = 0.0
            if not is_running and elapsed <= 0:
                continue
            result = self._compute_remaining(data, f"Cue {uid[:8]}", cue_id=uid)
            if not result:
                continue
            if best is None or (
                result.seconds is not None
                and best.seconds is not None
                and result.seconds < best.seconds
            ):
                best = result

        if best:
            return best

        # Connected if we got any recent reply
        recent = any(
            (time.time() - d.get("last_updated", 0)) < 3.0
            for uid, d in state.items()
            if uid not in META_KEYS
        ) or (time.time() - float(state.get("_meta", {}).get("last_updated", 0) or 0)) < 3.0

        if recent or self._catalog:
            return RemainingTime(
                None,
                None,
                True,
                "Connected — no enabled running cue with duration",
            )
        return RemainingTime(None, None, False, f"No reply from QLab at {self.ip}:{self.send_port}")

    def test_connection(self) -> RemainingTime:
        if not self._bind_ok:
            return RemainingTime(
                None, None, False, f"Cannot bind listen port {self.listen_port}"
            )
        ok, message = self._ensure_session()
        if not ok:
            return RemainingTime(None, None, False, message)
        try:
            catalog = self.refresh_catalog()
            return RemainingTime(
                None,
                None,
                True,
                f"{message} · {len(catalog)} cue(s) with duration",
            )
        except Exception as exc:
            logger.warning("Catalog after connect failed: %s", exc)
            return RemainingTime(None, None, True, message)

    def refresh_catalog(self) -> list[dict]:
        """Return all cues that have duration > 0."""
        if not self._bind_ok or not self._sock:
            return []
        if not self._session_ok:
            ok, _msg = self._ensure_session()
            if not ok:
                return []

        with self._lock:
            self._json_replies.pop("cueLists", None)
            self._json_replies.pop("workspaces", None)

        self._get("/workspaces")
        time.sleep(0.08)
        self._get("/cueLists")
        time.sleep(0.25)

        with self._lock:
            tree = self._json_replies.get("cueLists")
            workspaces = self._json_replies.get("workspaces")

        if tree is None and isinstance(workspaces, list) and workspaces:
            ws_id = workspaces[0].get("uniqueID") if isinstance(workspaces[0], dict) else None
            if ws_id:
                with self._lock:
                    self._json_replies.pop("cueLists", None)
                self._get(f"/workspace/{ws_id}/cueLists")
                time.sleep(0.3)
                with self._lock:
                    tree = self._json_replies.get("cueLists")

        flat: list[dict] = []
        self._flatten_cue_tree(tree or [], flat)

        # Deduplicate by unique_id
        by_id: dict[str, dict] = {}
        for item in flat:
            if item["unique_id"]:
                by_id[item["unique_id"]] = item

        # Query duration / running state for each cue
        for uid in by_id:
            self._get(f"/cue_id/{uid}/duration")
            self._get(f"/cue_id/{uid}/isRunning")
            self._get(f"/cue_id/{uid}/actionElapsed")
            self._get(f"/cue_id/{uid}/name")
            self._get(f"/cue_id/{uid}/number")
            self._get(f"/cue_id/{uid}/type")
        if by_id:
            time.sleep(min(0.5, 0.02 * len(by_id) + 0.1))

        with self._lock:
            state = {k: dict(v) for k, v in self._state.items()}

        catalog: list[dict] = []
        for uid, base in by_id.items():
            data = state.get(uid, {})
            try:
                duration = float(data["duration"]) if "duration" in data else None
            except (TypeError, ValueError):
                duration = None
            if duration is None or duration <= 0:
                continue
            try:
                elapsed = float(data["actionElapsed"]) if "actionElapsed" in data else 0.0
            except (TypeError, ValueError):
                elapsed = 0.0
            is_running = bool(data.get("isRunning"))
            remaining = max(0.0, duration - elapsed) if is_running else duration
            catalog.append(
                {
                    **base,
                    "name": str(data.get("name") or base["name"] or f"Cue {uid[:8]}"),
                    "number": str(data.get("number") or base["number"] or ""),
                    "type": str(data.get("type") or base["type"] or ""),
                    "duration": duration,
                    "remaining": remaining if is_running else None,
                    "is_running": is_running,
                }
            )

        catalog.sort(key=lambda c: (c.get("number") or "", c.get("name") or ""))
        with self._lock:
            self._catalog = catalog
        return list(catalog)

    def get_remaining(self) -> RemainingTime:
        return self._poll_once()
