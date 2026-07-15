from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RemainingTime:
    seconds: float | None
    label: str | None
    connected: bool
    message: str = ""
    cue_id: str | None = None


class MediaSource(ABC):
    @abstractmethod
    def test_connection(self) -> RemainingTime:
        ...

    @abstractmethod
    def get_remaining(self) -> RemainingTime:
        ...

    def close(self) -> None:
        return None
