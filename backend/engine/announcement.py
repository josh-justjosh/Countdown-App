from __future__ import annotations

from backend.models import (
    CONTINUOUS_ROLLDOWN_CLIP,
    ScheduleRow,
    sequence_clip_ids_for_row,
)


def resolve_clips_for_threshold(schedule: list[ScheduleRow], threshold: float) -> list[str]:
    for row in schedule:
        if not row.enabled or row.row_type != "threshold":
            continue
        if row.threshold_seconds is None:
            continue
        if abs(row.threshold_seconds - threshold) > 0.01:
            continue
        variant = None
        if row.active_variant_id:
            variant = next((v for v in row.variants if v.id == row.active_variant_id), None)
        if variant is None and row.variants:
            variant = row.variants[0]
        if not variant:
            return []
        return [s.clip_id for s in variant.slots if s.enabled]
    return []


def enabled_sequence_row(schedule: list[ScheduleRow]) -> ScheduleRow | None:
    for row in schedule:
        if row.row_type == "sequence" and row.enabled:
            return row
    return None


def sequence_thresholds(schedule: list[ScheduleRow]) -> list[tuple[float, str]]:
    """Per-second (seconds, clip_id) list for separate-style roll-down only."""
    row = enabled_sequence_row(schedule)
    if not row or row.sequence_style != "separate":
        return []
    result = []
    for clip_id in sequence_clip_ids_for_row(row):
        try:
            seconds = float(clip_id)
        except ValueError:
            continue
        result.append((seconds, clip_id))
    return result


def continuous_sequence_fire(
    schedule: list[ScheduleRow],
) -> tuple[float, list[str], str] | None:
    """(start_seconds, clip_ids, row_id) for continuous roll-down, or None."""
    row = enabled_sequence_row(schedule)
    if not row or row.sequence_style != "continuous":
        return None
    start = float(row.threshold_seconds if row.threshold_seconds is not None else 10)
    clips = sequence_clip_ids_for_row(row) or [CONTINUOUS_ROLLDOWN_CLIP]
    return start, clips, row.id


def threshold_rows(schedule: list[ScheduleRow]) -> list[tuple[float, str]]:
    """(threshold_seconds, row_id) for enabled threshold rows."""
    rows = []
    for row in schedule:
        if row.enabled and row.row_type == "threshold" and row.threshold_seconds is not None:
            rows.append((float(row.threshold_seconds), row.id))
    return rows
