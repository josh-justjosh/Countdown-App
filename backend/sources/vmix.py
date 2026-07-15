from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

from backend.sources.base import MediaSource, RemainingTime

logger = logging.getLogger(__name__)


class VmixSource(MediaSource):
    def __init__(
        self,
        ip: str,
        port: int = 8088,
        fallback1: str = "",
        fallback2: str = "",
        timeout: float = 1.5,
    ):
        self.base_url = f"http://{ip}:{port}/api"
        self.fallback1 = (fallback1 or "").strip()
        self.fallback2 = (fallback2 or "").strip()
        self.timeout = timeout

    def _fetch_xml(self) -> ET.Element | None:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self.base_url)
                response.raise_for_status()
                return ET.fromstring(response.content)
        except Exception as exc:
            logger.warning("vMix fetch failed: %s", exc)
            return None

    @staticmethod
    def _input_remaining(root: ET.Element, number: str | None = None, title: str | None = None) -> tuple[float | None, str | None]:
        xpath = None
        if number is not None:
            xpath = f".//input[@number='{number}']"
        elif title:
            xpath = f".//input[@title='{title}']"
        if not xpath:
            return None, None
        node = root.find(xpath)
        if node is None:
            return None, None
        try:
            duration = float(node.get("duration") or 0)
            position = float(node.get("position") or 0)
        except ValueError:
            return None, None
        if duration <= 0:
            return None, node.get("title")
        remaining_ms = max(0.0, duration - position)
        return remaining_ms / 1000.0, node.get("title")

    def _resolve(self, root: ET.Element) -> RemainingTime:
        active = root.findtext("active")
        candidates: list[tuple[str, str | None, str | None]] = []
        if active:
            candidates.append(("program", active, None))
        if self.fallback1:
            candidates.append(("fallback1", None, self.fallback1))
        if self.fallback2:
            candidates.append(("fallback2", None, self.fallback2))

        for kind, number, title in candidates:
            seconds, label = self._input_remaining(root, number=number, title=title)
            if seconds is not None:
                return RemainingTime(
                    seconds=seconds,
                    label=label or kind,
                    connected=True,
                    message=f"vMix {kind}",
                )

        return RemainingTime(
            seconds=None,
            label=None,
            connected=True,
            message="Connected — no media with duration on program/fallbacks",
        )

    def test_connection(self) -> RemainingTime:
        root = self._fetch_xml()
        if root is None:
            return RemainingTime(None, None, False, f"Cannot reach vMix at {self.base_url}")
        result = self._resolve(root)
        result.message = result.message or "Connected"
        return result

    def get_remaining(self) -> RemainingTime:
        root = self._fetch_xml()
        if root is None:
            return RemainingTime(None, None, False, f"Cannot reach vMix at {self.base_url}")
        return self._resolve(root)
