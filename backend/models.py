from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


ClipId = str


class ClipDefinition(BaseModel):
    id: ClipId
    prompt: str
    say: str
    context: str
    kind: Literal["numeric", "phrase", "custom"] = "phrase"


class CustomPhrase(BaseModel):
    id: ClipId
    prompt: str
    say: str
    context: str = "Custom phrase"


# Atomic phrase chips — dragged into schedule rows for custom composition
_ATOMIC_PHRASES: list[ClipDefinition] = [
    ClipDefinition(
        id="on_vt",
        prompt="on VT",
        say="on VT",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="left_on_vt",
        prompt="left on VT",
        say="left on VT",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="seconds",
        prompt="seconds",
        say="seconds",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="minutes",
        prompt="minutes",
        say="minutes",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="minute",
        prompt="minute",
        say="minute",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="hour",
        prompt="hour",
        say="hour",
        context="Phrase bank",
        kind="phrase",
    ),
    ClipDefinition(
        id="hours",
        prompt="hours",
        say="hours",
        context="Phrase bank",
        kind="phrase",
    ),
]

# Joined duration phrases for short defaults (< 2 min) — one natural recording each
JOINED_DURATION_PHRASES: list[ClipDefinition] = [
    ClipDefinition(
        id="15_seconds",
        prompt="15 seconds",
        say="fifteen seconds",
        context="15s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="20_seconds",
        prompt="20 seconds",
        say="twenty seconds",
        context="20s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="30_seconds",
        prompt="30 seconds",
        say="thirty seconds",
        context="30s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="45_seconds",
        prompt="45 seconds",
        say="forty-five seconds",
        context="45s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="60_seconds",
        prompt="60 seconds",
        say="sixty seconds",
        context="60s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="90_seconds",
        prompt="90 seconds",
        say="ninety seconds",
        context="90s call",
        kind="phrase",
    ),
    ClipDefinition(
        id="1_minute",
        prompt="1 minute",
        say="one minute",
        context="1-minute call",
        kind="phrase",
    ),
]

CONTINUOUS_ROLLDOWN_CLIP = "countdown_10_to_1"
SEPARATE_ROLLDOWN_CLIPS: list[str] = [str(n) for n in range(10, 0, -1)]

ROLLDOWN_PHRASE = ClipDefinition(
    id=CONTINUOUS_ROLLDOWN_CLIP,
    prompt="10 to 1 countdown",
    say="ten, nine, eight… one",
    context="Continuous 10→1 roll-down",
    kind="phrase",
)

PHRASE_BANK: list[ClipDefinition] = [
    *_ATOMIC_PHRASES,
    *JOINED_DURATION_PHRASES,
    ROLLDOWN_PHRASE,
]

# Core numeric IDs used by the default schedule + roll-down + minute composition
_NUMERIC_IDS = (
    list(range(1, 11))
    + [15, 20, 30, 45, 60, 90]
    + [2, 3, 5]  # leading numbers for 2/3/5 minute compositional calls
)

NUMERIC_CLIPS: list[ClipDefinition] = []
_seen_numeric: set[str] = set()
for n in _NUMERIC_IDS:
    cid = str(n)
    if cid in _seen_numeric:
        continue
    _seen_numeric.add(cid)
    NUMERIC_CLIPS.append(
        ClipDefinition(
            id=cid,
            prompt=cid,
            say=cid,
            context="Number call",
            kind="numeric",
        )
    )

CLIP_LEXICON: list[ClipDefinition] = [*PHRASE_BANK, *NUMERIC_CLIPS]
CLIP_BY_ID: dict[str, ClipDefinition] = {c.id: c for c in CLIP_LEXICON}
PHRASE_BANK_IDS: set[str] = {c.id for c in PHRASE_BANK}


def numeric_clip_definition(n: int | str) -> ClipDefinition:
    cid = str(int(n)) if str(n).isdigit() else str(n)
    return ClipDefinition(
        id=cid,
        prompt=cid,
        say=cid,
        context="Number call",
        kind="numeric",
    )


def ensure_numeric_in_lexicon(clip_id: str) -> ClipDefinition | None:
    """Return a numeric ClipDefinition if clip_id is an integer string."""
    if not re.fullmatch(r"-?\d+", clip_id):
        return None
    return numeric_clip_definition(clip_id)


def build_lexicon(custom_phrases: list[CustomPhrase] | None = None) -> list[ClipDefinition]:
    """Full lexicon for wizard / clip library: phrases + numerics + customs."""
    items = list(CLIP_LEXICON)
    seen = {c.id for c in items}
    for custom in custom_phrases or []:
        if custom.id in seen:
            continue
        items.append(
            ClipDefinition(
                id=custom.id,
                prompt=custom.prompt,
                say=custom.say,
                context=custom.context or "Custom phrase",
                kind="custom",
            )
        )
        seen.add(custom.id)
    return items


def lexicon_for_profile_schedule(
    schedule: list[ScheduleRow],
    custom_phrases: list[CustomPhrase] | None = None,
) -> list[ClipDefinition]:
    """Lexicon plus any numeric IDs referenced by the schedule."""
    items = build_lexicon(custom_phrases)
    by_id = {c.id: c for c in items}
    for cid in clip_ids_for_schedule(schedule, enabled_only=False):
        if cid in by_id:
            continue
        numeric = ensure_numeric_in_lexicon(cid)
        if numeric:
            items.append(numeric)
            by_id[cid] = numeric
        else:
            # Unknown custom already covered; orphaned ids get a stub
            items.append(
                ClipDefinition(
                    id=cid,
                    prompt=cid.replace("_", " "),
                    say=cid.replace("_", " "),
                    context="Referenced by schedule",
                    kind="custom",
                )
            )
            by_id[cid] = items[-1]
    return items


class ClipSlot(BaseModel):
    clip_id: ClipId
    enabled: bool = True


class ScheduleVariant(BaseModel):
    id: str
    label: str
    slots: list[ClipSlot]


class ScheduleRow(BaseModel):
    id: str
    threshold_seconds: float | None = None
    label: str
    enabled: bool = True
    row_type: Literal["threshold", "sequence"] = "threshold"
    active_variant_id: str | None = None
    variants: list[ScheduleVariant] = Field(default_factory=list)
    sequence_clip_ids: list[ClipId] = Field(default_factory=list)
    sequence_style: Literal["continuous", "separate"] = "continuous"
    voice_id: str | None = None  # None → profile.active_voice_id


class VoicePack(BaseModel):
    id: str
    name: str


class PipsPeriod(BaseModel):
    id: str
    start: str  # "HH:MM"
    end: str  # "HH:MM" inclusive


class PipsSpecificTime(BaseModel):
    id: str
    time: str  # "HH:MM"
    enabled: bool = True
    label: str = ""


class PipsConfig(BaseModel):
    enabled: bool = False
    on_hour: bool = True
    on_quarter_past: bool = False
    on_half: bool = False
    on_quarter_to: bool = False
    days: list[int] = Field(default_factory=lambda: list(range(7)))  # Mon=0 … Sun=6
    use_periods: bool = False
    periods: list[PipsPeriod] = Field(default_factory=list)
    specific_times: list[PipsSpecificTime] = Field(default_factory=list)
    leap_seconds_enabled: bool = True
    leap_seconds_auto_update: bool = True
    simulate_leap_next_utc_midnight: bool = False


class QLabCueSettings(BaseModel):
    voice_id: str | None = None  # None → Active voice


class ConnectionConfig(BaseModel):
    mode: Literal["vmix", "qlab", "timed"] = "vmix"
    vmix_ip: str = "127.0.0.1"
    vmix_port: int = 8088
    vmix_fallback1: str = ""
    vmix_fallback2: str = ""
    qlab_ip: str = "127.0.0.1"
    qlab_send_port: int = 53000
    qlab_listen_port: int = 53001
    qlab_passcode: str = ""
    qlab_cues_initialized: bool = False
    qlab_enabled_cue_ids: list[str] = Field(default_factory=list)
    qlab_cue_settings: dict[str, QLabCueSettings] = Field(default_factory=dict)
    voice_priority: Literal["cue", "schedule"] = "cue"


class Profile(BaseModel):
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    schedule: list[ScheduleRow] = Field(default_factory=list)
    custom_phrases: list[CustomPhrase] = Field(default_factory=list)
    voices: list[VoicePack] = Field(default_factory=list)
    active_voice_id: str = "default"
    output_device_id: int | None = None
    output_device_name: str | None = None
    pips: PipsConfig = Field(default_factory=PipsConfig)


def _row(
    seconds: float,
    label: str,
    slots: list[str],
    *,
    row_id: str | None = None,
    variants: list[ScheduleVariant] | None = None,
    active_variant_id: str = "default",
) -> ScheduleRow:
    if variants is None:
        variants = [
            ScheduleVariant(
                id="default",
                label="Default",
                slots=[ClipSlot(clip_id=s) for s in slots],
            )
        ]
    return ScheduleRow(
        id=row_id or f"t{int(seconds)}",
        threshold_seconds=seconds,
        label=label,
        variants=variants,
        active_variant_id=active_variant_id,
    )


# Thresholds under 2 minutes that offer number-only + "N seconds" (and 1 minute at 60).
UNDER_TWO_MIN_THRESHOLDS: tuple[int, ...] = (15, 20, 30, 45, 60, 90)


def under_two_min_variants(seconds: int) -> list[ScheduleVariant]:
    """Number-only and joined seconds phrases; 60s also keeps a 1-minute option."""
    s = int(seconds)
    variants = [
        ScheduleVariant(
            id=f"n{s}",
            label=str(s),
            slots=[ClipSlot(clip_id=str(s))],
        ),
        ScheduleVariant(
            id=f"n{s}_seconds",
            label=f"{s} seconds",
            slots=[ClipSlot(clip_id=f"{s}_seconds")],
        ),
    ]
    if s == 60:
        variants.append(
            ScheduleVariant(
                id="one_minute",
                label="1 minute left on VT",
                slots=[
                    ClipSlot(clip_id="1_minute"),
                    ClipSlot(clip_id="left_on_vt"),
                ],
            )
        )
    return variants


def under_two_min_row(seconds: int) -> ScheduleRow:
    s = int(seconds)
    label = f"{s} / {s} seconds" if s != 60 else f"{s} / {s} seconds / 1 minute"
    return ScheduleRow(
        id=f"t{s}",
        threshold_seconds=float(s),
        label=label,
        variants=under_two_min_variants(s),
        active_variant_id=f"n{s}",
    )


def default_schedule() -> list[ScheduleRow]:
    """Shortest first, longest last; roll-down sequence pinned at top."""
    return [
        ScheduleRow(
            id="rolldown",
            threshold_seconds=10,
            label="10 → 1",
            row_type="sequence",
            sequence_style="continuous",
            sequence_clip_ids=[CONTINUOUS_ROLLDOWN_CLIP],
        ),
        *[under_two_min_row(s) for s in UNDER_TWO_MIN_THRESHOLDS],
        # 2 minutes and up: compositional ([n] + minutes + left on VT)
        _row(120, "2 minutes", ["2", "minutes", "left_on_vt"]),
        _row(180, "3 minutes", ["3", "minutes", "left_on_vt"]),
        _row(300, "5 minutes", ["5", "minutes", "left_on_vt"]),
    ]


def sort_schedule(schedule: list[ScheduleRow]) -> list[ScheduleRow]:
    """Sequence rows first, then thresholds ascending (longest at bottom)."""
    sequences = [r for r in schedule if r.row_type == "sequence"]
    thresholds = [r for r in schedule if r.row_type != "sequence"]
    thresholds.sort(key=lambda r: (r.threshold_seconds is None, r.threshold_seconds or 0))
    return sequences + thresholds


def default_profile() -> Profile:
    return Profile(
        schedule=default_schedule(),
        voices=[VoicePack(id="default", name="Default")],
        active_voice_id="default",
    )


def ensure_profile_voices(profile: Profile) -> Profile:
    """Guarantee at least one voice pack and a valid active_voice_id."""
    if not profile.voices:
        profile.voices = [VoicePack(id="default", name="Default")]
    if not any(v.id == profile.active_voice_id for v in profile.voices):
        profile.active_voice_id = profile.voices[0].id
    return profile


def resolve_row_voice_id(row: ScheduleRow, profile: Profile) -> str:
    if row.voice_id and any(v.id == row.voice_id for v in profile.voices):
        return row.voice_id
    return profile.active_voice_id


def _valid_voice_id(voice_id: str | None, profile: Profile) -> str | None:
    if voice_id and any(v.id == voice_id for v in profile.voices):
        return voice_id
    return None


def resolve_announcement_voice_id(
    row: ScheduleRow | None,
    profile: Profile,
    *,
    cue_id: str | None = None,
) -> str:
    """Resolve playback voice.

    Priority (cue vs schedule) only applies when *both* sides have an explicit
    voice other than Active. Otherwise the one specific choice wins.
    """
    row_voice = _valid_voice_id(row.voice_id if row else None, profile)
    cue_voice = None
    if cue_id and profile.connection.mode == "qlab":
        settings = profile.connection.qlab_cue_settings.get(cue_id)
        if settings is not None:
            cue_voice = _valid_voice_id(settings.voice_id, profile)

    if profile.connection.mode == "qlab":
        if cue_voice and row_voice:
            if profile.connection.voice_priority == "schedule":
                return row_voice
            return cue_voice
        return cue_voice or row_voice or profile.active_voice_id

    return row_voice or profile.active_voice_id


def sequence_clip_ids_for_row(row: ScheduleRow) -> list[str]:
    """Clips required/played for a sequence row based on style."""
    if row.sequence_style == "continuous":
        return [CONTINUOUS_ROLLDOWN_CLIP]
    if row.sequence_clip_ids:
        return list(row.sequence_clip_ids)
    return list(SEPARATE_ROLLDOWN_CLIPS)


def clip_ids_for_schedule(
    schedule: list[ScheduleRow],
    *,
    enabled_only: bool = True,
) -> set[str]:
    """Return clip IDs referenced by schedule rows."""
    needed: set[str] = set()
    for row in schedule:
        if enabled_only and not row.enabled:
            continue
        if row.row_type == "sequence":
            needed.update(sequence_clip_ids_for_row(row))
            continue
        variant = None
        if row.active_variant_id:
            variant = next((v for v in row.variants if v.id == row.active_variant_id), None)
        if variant is None and row.variants:
            variant = row.variants[0]
        if variant:
            for slot in variant.slots:
                if enabled_only and not slot.enabled:
                    continue
                needed.add(slot.clip_id)
    return needed


def clip_refs_for_profile(profile: Profile, *, enabled_only: bool = True) -> set[tuple[str, str]]:
    """Return (voice_id, clip_id) pairs needed by enabled schedule rows."""
    refs: set[tuple[str, str]] = set()
    for row in profile.schedule:
        if enabled_only and not row.enabled:
            continue
        voice_id = resolve_row_voice_id(row, profile)
        if row.row_type == "sequence":
            for cid in sequence_clip_ids_for_row(row):
                refs.add((voice_id, cid))
            continue
        variant = None
        if row.active_variant_id:
            variant = next((v for v in row.variants if v.id == row.active_variant_id), None)
        if variant is None and row.variants:
            variant = row.variants[0]
        if variant:
            for slot in variant.slots:
                if enabled_only and not slot.enabled:
                    continue
                refs.add((voice_id, slot.clip_id))
    return refs


def slugify_phrase(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "custom"


def parse_time_to_seconds(raw: str, unit: str = "auto") -> float | None:
    """
    Parse a time value to seconds.
    unit: auto | seconds | minutes | hours
    Also accepts mm:ss and hh:mm:ss when unit is auto (or always for colon forms).
    """
    text = (raw or "").strip().lower()
    if not text:
        return None

    if ":" in text:
        parts = text.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return None

    try:
        value = float(text)
    except ValueError:
        return None

    if unit == "minutes":
        return value * 60
    if unit == "hours":
        return value * 3600
    # seconds or auto without colon
    return value


def format_threshold_label(seconds: float) -> str:
    s = int(round(seconds))
    if s % 3600 == 0 and s >= 3600:
        h = s // 3600
        return f"{h} hour" if h == 1 else f"{h} hours"
    if s % 60 == 0 and s >= 60:
        m = s // 60
        return f"{m} minute" if m == 1 else f"{m} minutes"
    return f"{s} seconds"


def auto_slots_for_threshold(seconds: float) -> list[str]:
    """Suggest chips for a new threshold (number-only under 2 minutes)."""
    s = int(round(seconds))
    if s in UNDER_TWO_MIN_THRESHOLDS:
        return [str(s)]
    if s > 0 and s % 3600 == 0:
        h = s // 3600
        return [str(h), "hours"] if h != 1 else ["1", "hour"]
    if s > 0 and s % 60 == 0:
        m = s // 60
        if m == 1:
            return ["1_minute"]
        return [str(m), "minutes"]
    return [str(s), "seconds"]
