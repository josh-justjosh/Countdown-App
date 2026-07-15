"""Leap-second table for Greenwich-style pips (positive and negative)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

import httpx

from backend.settings import LEAP_SECONDS_PATH, ensure_data_dirs

logger = logging.getLogger(__name__)

LeapKind = Literal["none", "positive", "negative"]

IERS_LEAP_SECONDS_URLS = (
    "https://data.iana.org/time-zones/data/leap-seconds.list",
    "https://raw.githubusercontent.com/eggert/tz/master/leap-seconds.list",
)

# Bootstrap: UTC calendar dates that contain the leap second (23:59:60).
# Following 00:00:00 UTC is the long-pip on-time marker for a positive leap.
_BOOTSTRAP: list[tuple[date, int]] = [
    (date(1972, 6, 30), 1),
    (date(1972, 12, 31), 1),
    (date(1973, 12, 31), 1),
    (date(1974, 12, 31), 1),
    (date(1975, 12, 31), 1),
    (date(1976, 12, 31), 1),
    (date(1977, 12, 31), 1),
    (date(1978, 12, 31), 1),
    (date(1979, 12, 31), 1),
    (date(1981, 6, 30), 1),
    (date(1982, 6, 30), 1),
    (date(1983, 6, 30), 1),
    (date(1985, 6, 30), 1),
    (date(1987, 12, 31), 1),
    (date(1989, 12, 31), 1),
    (date(1990, 12, 31), 1),
    (date(1992, 6, 30), 1),
    (date(1993, 6, 30), 1),
    (date(1994, 6, 30), 1),
    (date(1995, 12, 31), 1),
    (date(1997, 6, 30), 1),
    (date(1998, 12, 31), 1),
    (date(2005, 12, 31), 1),
    (date(2008, 12, 31), 1),
    (date(2012, 6, 30), 1),
    (date(2015, 6, 30), 1),
    (date(2016, 12, 31), 1),
]


@dataclass(frozen=True)
class LeapEntry:
    """UTC date of the leap second (the calendar day that contains 23:59:60)."""

    day: date
    delta: int  # +1 positive, -1 negative


_lock = threading.Lock()
_entries: list[LeapEntry] | None = None
_last_fetch_monotonic: float = 0.0
_FETCH_INTERVAL = 24 * 3600


def _bootstrap_entries() -> list[LeapEntry]:
    return [LeapEntry(day=d, delta=delta) for d, delta in _BOOTSTRAP]


def _load_cache() -> list[LeapEntry] | None:
    path = LEAP_SECONDS_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out: list[LeapEntry] = []
        for item in data.get("entries", []):
            out.append(
                LeapEntry(
                    day=date.fromisoformat(item["day"]),
                    delta=int(item["delta"]),
                )
            )
        return out or None
    except Exception as exc:
        logger.warning("Failed to read leap seconds cache: %s", exc)
        return None


def _save_cache(entries: list[LeapEntry]) -> None:
    ensure_data_dirs()
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entries": [{"day": e.day.isoformat(), "delta": e.delta} for e in entries],
    }
    LEAP_SECONDS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def get_entries() -> list[LeapEntry]:
    global _entries
    with _lock:
        if _entries is None:
            cached = _load_cache()
            _entries = cached if cached is not None else _bootstrap_entries()
        return list(_entries)


def set_entries(entries: list[LeapEntry], *, persist: bool = True) -> None:
    global _entries
    with _lock:
        _entries = sorted(entries, key=lambda e: e.day)
        if persist:
            _save_cache(_entries)


def leap_kind_for_mark_utc(mark_utc: datetime, *, enabled: bool = True) -> LeapKind:
    """Kind when mark is 00:00:00 UTC on the day after a leap-second day."""
    if not enabled:
        return "none"
    if mark_utc.tzinfo is None:
        mark_utc = mark_utc.replace(tzinfo=timezone.utc)
    else:
        mark_utc = mark_utc.astimezone(timezone.utc)
    if not (mark_utc.hour == 0 and mark_utc.minute == 0 and mark_utc.second == 0):
        return "none"
    prev = date.fromordinal(mark_utc.date().toordinal() - 1)
    for entry in get_entries():
        if entry.day == prev:
            if entry.delta > 0:
                return "positive"
            if entry.delta < 0:
                return "negative"
    return "none"


def lead_seconds(kind: LeapKind) -> int:
    if kind == "positive":
        return 6
    if kind == "negative":
        return 4
    return 5


def short_pip_count(kind: LeapKind) -> int:
    """Number of short pips before the long on-time marker."""
    if kind == "positive":
        return 6
    if kind == "negative":
        return 4
    return 5


_NTP_EPOCH = datetime(1900, 1, 1, tzinfo=timezone.utc)
_LINE_RE = re.compile(r"^(\d+)\s+(\d+)\s")


def parse_leap_seconds_list(text: str) -> list[LeapEntry]:
    """
    Parse IETF/NIST leap-seconds.list.
    Lines: <NTP seconds> <TAI-UTC> …
    A leap is inferred when TAI-UTC changes vs the previous line.
    """
    rows: list[tuple[datetime, int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        ntp = int(m.group(1))
        tai_utc = int(m.group(2))
        instant = _NTP_EPOCH.timestamp() + ntp
        dt = datetime.fromtimestamp(instant, tz=timezone.utc)
        rows.append((dt, tai_utc))
    rows.sort(key=lambda r: r[0])
    entries: list[LeapEntry] = []
    prev_offset: int | None = None
    for dt, offset in rows:
        if prev_offset is None:
            prev_offset = offset
            continue
        delta = offset - prev_offset
        prev_offset = offset
        if delta == 0:
            continue
        # NTP instant is 00:00:00 UTC after the leap; leap day is previous calendar date.
        leap_day = date.fromordinal(dt.date().toordinal() - 1)
        entries.append(LeapEntry(day=leap_day, delta=1 if delta > 0 else -1))
    return entries


def fetch_iers_leap_seconds(timeout: float = 15.0) -> list[LeapEntry]:
    last_exc: Exception | None = None
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in IERS_LEAP_SECONDS_URLS:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                entries = parse_leap_seconds_list(resp.text)
                if entries:
                    return entries
            except Exception as exc:
                last_exc = exc
                logger.debug("Leap list fetch failed for %s: %s", url, exc)
    if last_exc:
        raise last_exc
    raise ValueError("Could not download leap-seconds list")


def refresh_leap_seconds(*, force: bool = False) -> dict:
    """Fetch and cache IERS list. Returns status dict."""
    global _last_fetch_monotonic
    now = time.monotonic()
    if not force and _last_fetch_monotonic and (now - _last_fetch_monotonic) < _FETCH_INTERVAL:
        return {
            "success": True,
            "skipped": True,
            "count": len(get_entries()),
            "message": "Cache still fresh",
        }
    try:
        entries = fetch_iers_leap_seconds()
        if not entries:
            raise ValueError("Parsed empty leap-second list")
        by_day = {e.day: e for e in _bootstrap_entries()}
        for e in entries:
            by_day[e.day] = e
        merged = sorted(by_day.values(), key=lambda e: e.day)
        set_entries(merged, persist=True)
        _last_fetch_monotonic = now
        return {
            "success": True,
            "skipped": False,
            "count": len(merged),
            "message": f"Updated {len(merged)} leap-second entries",
        }
    except Exception as exc:
        logger.warning("IERS leap-second fetch failed: %s", exc)
        return {
            "success": False,
            "skipped": False,
            "count": len(get_entries()),
            "message": str(exc),
        }


def maybe_auto_refresh(auto_update: bool) -> None:
    if not auto_update:
        return
    refresh_leap_seconds(force=False)
