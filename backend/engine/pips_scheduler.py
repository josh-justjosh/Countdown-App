"""Wall-clock Greenwich Time Signal (pips) scheduler."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np

from backend.engine.leap_seconds import (
    LeapKind,
    lead_seconds,
    leap_kind_for_mark_utc,
    maybe_auto_refresh,
    short_pip_count,
)
from backend.engine.player import AudioPlayer
from backend.models import PipsConfig, PipsPeriod, Profile

logger = logging.getLogger(__name__)

ProfileFn = Callable[[], Profile]


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    parts = (text or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _minutes_of_day(h: int, m: int) -> int:
    return h * 60 + m


def _in_period(mark_min: int, period: PipsPeriod) -> bool:
    start = _parse_hhmm(period.start)
    end = _parse_hhmm(period.end)
    if not start or not end:
        return False
    a, b = _minutes_of_day(*start), _minutes_of_day(*end)
    if a <= b:
        return a <= mark_min <= b
    return mark_min >= a or mark_min <= b


def generate_pips_pcm(
    kind: LeapKind = "none",
    *,
    sample_rate: int = 48000,
    freq: float = 1000.0,
    amplitude: float = 0.35,
) -> np.ndarray:
    """
    Synthesize GTS-style pips.
    Short pip starts at each of the last N seconds before T; long pip starts at T.
    Negative leap: N=4. Positive: N=6. Normal: N=5.
    """
    n_short = short_pip_count(kind)
    lead = lead_seconds(kind)
    short_dur = 0.1
    long_dur = 0.5
    total_sec = lead + long_dur
    n = int(total_sec * sample_rate)
    buf = np.zeros((n, 1), dtype=np.float32)
    t = np.arange(int(max(short_dur, long_dur) * sample_rate) + 8, dtype=np.float64) / sample_rate

    def write_tone(start_sec: float, duration: float) -> None:
        start_i = int(round(start_sec * sample_rate))
        length = int(round(duration * sample_rate))
        if start_i >= n or length <= 0:
            return
        end_i = min(n, start_i + length)
        length = end_i - start_i
        tone = (amplitude * np.sin(2 * np.pi * freq * t[:length])).astype(np.float32)
        fade = min(48, length // 4)
        if fade > 0:
            tone[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            tone[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        buf[start_i:end_i, 0] += tone

    for i in range(n_short):
        write_tone(float(i), short_dur)
    write_tone(float(lead), long_dur)
    return buf


@dataclass
class PipsEvent:
    mark_local: datetime
    leap_kind: LeapKind
    source: str  # "mark" | "specific"


class PipsScheduler:
    def __init__(
        self,
        player: AudioPlayer,
        get_profile: ProfileFn,
        *,
        on_fired: Callable[[PipsEvent], None] | None = None,
    ) -> None:
        self.player = player
        self.get_profile = get_profile
        self.on_fired = on_fired
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired: set[str] = set()
        self._last_next: PipsEvent | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pips-scheduler", daemon=True)
        self._thread.start()
        logger.info("Pips scheduler started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def notify_config_changed(self) -> None:
        self._wake.set()

    def peek_next(self) -> dict | None:
        event = self._compute_next(self.get_profile().pips, datetime.now().astimezone())
        self._last_next = event
        if not event:
            return None
        return {
            "at": event.mark_local.isoformat(timespec="seconds"),
            "label": event.mark_local.strftime("%a %H:%M"),
            "leap_kind": event.leap_kind,
            "source": event.source,
        }

    def _event_key(self, event: PipsEvent) -> str:
        return (
            f"{event.mark_local.date().isoformat()}|"
            f"{event.mark_local.strftime('%H:%M')}|{event.leap_kind}|{event.source}"
        )

    def _quarter_minutes(self, cfg: PipsConfig) -> list[int]:
        mins: list[int] = []
        if cfg.on_hour:
            mins.append(0)
        if cfg.on_quarter_past:
            mins.append(15)
        if cfg.on_half:
            mins.append(30)
        if cfg.on_quarter_to:
            mins.append(45)
        return mins

    def _mark_allowed(self, cfg: PipsConfig, mark: datetime) -> bool:
        if mark.weekday() not in set(cfg.days):
            return False
        mark_min = mark.hour * 60 + mark.minute
        if not cfg.use_periods or not cfg.periods:
            return True
        return any(_in_period(mark_min, p) for p in cfg.periods)

    def _leap_kind_for_local_mark(self, cfg: PipsConfig, mark_local: datetime) -> LeapKind:
        mark_utc = mark_local.astimezone(timezone.utc)
        kind = leap_kind_for_mark_utc(mark_utc, enabled=cfg.leap_seconds_enabled)
        if (
            cfg.simulate_leap_next_utc_midnight
            and mark_utc.hour == 0
            and mark_utc.minute == 0
            and mark_utc.second == 0
        ):
            return "positive"
        return kind

    def _iter_candidate_marks(
        self, cfg: PipsConfig, now_local: datetime, horizon_hours: int = 48
    ) -> list[PipsEvent]:
        events: list[PipsEvent] = []
        quarters = set(self._quarter_minutes(cfg))
        end = now_local + timedelta(hours=horizon_hours)

        if quarters:
            cursor = now_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while cursor <= end:
                if cursor.minute in quarters and self._mark_allowed(cfg, cursor):
                    kind = self._leap_kind_for_local_mark(cfg, cursor)
                    events.append(PipsEvent(cursor, kind, "mark"))
                if quarters == {0} and cursor.minute == 0:
                    cursor += timedelta(hours=1)
                else:
                    cursor += timedelta(minutes=1)

        day0 = now_local.date()
        for spec in cfg.specific_times:
            if not spec.enabled:
                continue
            hm = _parse_hhmm(spec.time)
            if not hm:
                continue
            h, m = hm
            for day_offset in range(0, max(2, horizon_hours // 24 + 2)):
                d = date.fromordinal(day0.toordinal() + day_offset)
                mark = datetime(d.year, d.month, d.day, h, m, tzinfo=now_local.tzinfo)
                if mark > now_local:
                    kind = self._leap_kind_for_local_mark(cfg, mark)
                    events.append(PipsEvent(mark, kind, "specific"))

        events.sort(key=lambda e: e.mark_local)
        return events

    def _compute_next(self, cfg: PipsConfig, now_local: datetime) -> PipsEvent | None:
        if not cfg.enabled:
            return None
        for event in self._iter_candidate_marks(cfg, now_local):
            key = self._event_key(event)
            if key in self._fired:
                continue
            # Skip only once the on-time mark itself has passed (not merely the lead-in).
            if event.mark_local <= now_local - timedelta(seconds=0.5):
                continue
            return event
        return None

    def _clear_simulate_flag_if_needed(self, event: PipsEvent) -> None:
        profile = self.get_profile()
        if not profile.pips.simulate_leap_next_utc_midnight:
            return
        mark_utc = event.mark_local.astimezone(timezone.utc)
        if mark_utc.hour == 0 and mark_utc.minute == 0:
            profile.pips.simulate_leap_next_utc_midnight = False

    def _play_event(self, event: PipsEvent) -> None:
        pcm = generate_pips_pcm(event.leap_kind)
        self.player.play_array(pcm, 48000, priority=-1, interrupt=False)
        self._fired.add(self._event_key(event))
        # Prune old fired keys
        if len(self._fired) > 64:
            self._fired = set(list(self._fired)[-32:])
        self._clear_simulate_flag_if_needed(event)
        if self.on_fired:
            try:
                self.on_fired(event)
            except Exception as exc:
                logger.debug("on_fired failed: %s", exc)
        logger.info(
            "Pips fired for %s (%s, leap=%s)",
            event.mark_local.isoformat(timespec="seconds"),
            event.source,
            event.leap_kind,
        )

    def _still_valid(self, event: PipsEvent, cfg: PipsConfig, now_local: datetime) -> bool:
        """True if this mark is still the next scheduled fire."""
        if self._event_key(event) in self._fired:
            return False
        nxt = self._compute_next(cfg, now_local)
        return nxt is not None and self._event_key(nxt) == self._event_key(event)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                profile = self.get_profile()
                cfg = profile.pips
                if cfg.leap_seconds_auto_update:
                    maybe_auto_refresh(True)

                now = datetime.now().astimezone()
                event = self._compute_next(cfg, now)
                self._last_next = event

                if not event:
                    self._wake.wait(timeout=30)
                    self._wake.clear()
                    continue

                lead = lead_seconds(event.leap_kind)
                start_at = event.mark_local - timedelta(seconds=lead)
                logger.debug(
                    "Pips waiting for %s (start %s, leap=%s)",
                    event.mark_local.isoformat(timespec="seconds"),
                    start_at.isoformat(timespec="seconds"),
                    event.leap_kind,
                )

                aborted = False
                while not self._stop.is_set():
                    delay = (start_at - datetime.now().astimezone()).total_seconds()
                    if delay <= 0.02:
                        break
                    if self._wake.wait(timeout=min(delay, 15.0)):
                        aborted = True
                        break
                self._wake.clear()

                if aborted or self._stop.is_set():
                    continue

                while not self._stop.is_set():
                    remaining = (start_at - datetime.now().astimezone()).total_seconds()
                    if remaining <= 0.01:
                        break
                    if self._wake.is_set():
                        aborted = True
                        break
                    time.sleep(min(0.05, max(remaining, 0)))

                if aborted or self._stop.is_set():
                    self._wake.clear()
                    continue

                now2 = datetime.now().astimezone()
                cfg2 = self.get_profile().pips
                if now2 > event.mark_local + timedelta(seconds=1.0):
                    logger.warning(
                        "Missed pips mark %s (now %s)",
                        event.mark_local.isoformat(timespec="seconds"),
                        now2.isoformat(timespec="seconds"),
                    )
                    continue
                # Re-check gently from slightly before start so late lead-in still matches.
                check_at = min(now2, start_at + timedelta(milliseconds=50))
                if self._still_valid(event, cfg2, check_at):
                    self._play_event(event)
                else:
                    logger.info(
                        "Skipped stale pips plan for %s (schedule changed)",
                        event.mark_local.isoformat(timespec="seconds"),
                    )
            except Exception as exc:
                logger.error("Pips scheduler error: %s", exc)
                time.sleep(2)
