from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

from backend.models import (
    PHRASE_BANK,
    ClipDefinition,
    ClipSlot,
    Profile,
    ScheduleVariant,
    VoicePack,
    build_lexicon,
    clip_ids_for_schedule,
    clip_refs_for_profile,
    default_profile,
    default_schedule,
    ensure_profile_voices,
    lexicon_for_profile_schedule,
    sort_schedule,
    under_two_min_variants,
    UNDER_TWO_MIN_THRESHOLDS,
    CONTINUOUS_ROLLDOWN_CLIP,
    SEPARATE_ROLLDOWN_CLIPS,
)
from backend.settings import CLIPS_DIR, PROFILE_PATH, VOICES_DIR, LOCKED_VOICE_IDS, ensure_data_dirs

logger = logging.getLogger(__name__)

# Previous default split short calls into number + "seconds"/"minute" chips.
_SPLIT_UNDER_2MIN = frozenset({15, 20, 30, 45, 60, 90})


def normalize_clip_peak(
    data: np.ndarray,
    *,
    target_peak: float = 0.82,
    max_gain: float = 10.0,
) -> np.ndarray:
    """Peak-normalise an entire clip to a consistent level."""
    if data.size == 0:
        return data
    mono = np.mean(data, axis=1) if data.ndim == 2 else data.reshape(-1)
    peak = float(np.max(np.abs(mono)))
    if peak < 1e-6:
        return data
    gain = min(max_gain, target_peak / peak)
    if abs(gain - 1.0) < 0.02:
        return data
    out = np.array(data, dtype=np.float32, copy=True)
    np.clip(out * gain, -1.0, 1.0, out=out)
    return out


def normalize_for_clip(clip_id: str, data: np.ndarray, sample_rate: int) -> np.ndarray:
    """Rolldown: per-digit segments. All other clips: whole-file peak."""
    if clip_id == CONTINUOUS_ROLLDOWN_CLIP:
        return normalize_segment_peaks(data, sample_rate)
    return normalize_clip_peak(data)


def normalize_segment_peaks(
    data: np.ndarray,
    sample_rate: int,
    *,
    target_peak: float = 0.82,
    frame_ms: float = 20.0,
    silence_ratio: float = 0.10,
    min_gap_ms: float = 60.0,
    min_seg_ms: float = 60.0,
    max_gain: float = 10.0,
) -> np.ndarray:
    """Peak-normalise each voiced burst (for continuous 10→1 takes)."""
    if data.size == 0:
        return data
    mono = np.mean(data, axis=1) if data.ndim == 2 else data.reshape(-1)
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    n_frames = max(1, int(np.ceil(len(mono) / frame)))
    energies = np.zeros(n_frames, dtype=np.float64)
    for i in range(n_frames):
        chunk = mono[i * frame : (i + 1) * frame]
        if chunk.size:
            energies[i] = float(np.sqrt(np.mean(chunk * chunk)))
    energy_peak = float(np.max(energies)) if energies.size else 0.0
    if energy_peak < 1e-9:
        return data
    thresh = energy_peak * silence_ratio
    voiced = energies >= thresh
    min_gap = max(1, int(round(min_gap_ms / frame_ms)))
    min_seg = max(1, int(round(min_seg_ms / frame_ms)))

    segments: list[tuple[int, int]] = []
    i = 0
    while i < len(voiced):
        while i < len(voiced) and not voiced[i]:
            i += 1
        if i >= len(voiced):
            break
        start = i
        end = i
        while end < len(voiced):
            if voiced[end]:
                end += 1
                continue
            gap = 0
            while end + gap < len(voiced) and not voiced[end + gap]:
                gap += 1
            if gap >= min_gap:
                break
            end += gap
        if end - start >= min_seg:
            segments.append((start * frame, min(len(mono), end * frame)))
        i = end

    out = np.array(data, dtype=np.float32, copy=True)

    def apply_gain(a: int, b: int, gain: float) -> None:
        if out.ndim == 2:
            out[a:b, :] = np.clip(out[a:b, :] * gain, -1.0, 1.0)
        else:
            out[a:b] = np.clip(out[a:b] * gain, -1.0, 1.0)

    if not segments:
        peak = float(np.max(np.abs(mono)))
        if peak > 1e-6:
            apply_gain(0, len(mono), min(max_gain, target_peak / peak))
        return out

    for a, b in segments:
        peak = float(np.max(np.abs(mono[a:b]))) if b > a else 0.0
        if peak < 1e-6:
            continue
        gain = min(max_gain, target_peak / peak)
        if abs(gain - 1.0) < 0.02:
            continue
        apply_gain(a, b, gain)
    return out


def migrate_legacy_clips() -> None:
    """Move flat data/clips/*.wav into data/voices/default/ once."""
    ensure_data_dirs()
    if not CLIPS_DIR.exists():
        return
    wavs = list(CLIPS_DIR.glob("*.wav"))
    if not wavs:
        return
    dest = VOICES_DIR / "default"
    dest.mkdir(parents=True, exist_ok=True)
    for wav in wavs:
        target = dest / wav.name
        if not target.exists():
            shutil.move(str(wav), str(target))
            logger.info("Migrated legacy clip %s → voices/default/", wav.name)
        else:
            wav.unlink(missing_ok=True)
    try:
        if CLIPS_DIR.exists() and not any(CLIPS_DIR.iterdir()):
            CLIPS_DIR.rmdir()
    except OSError:
        pass


class ProfileStore:
    def __init__(self) -> None:
        ensure_data_dirs()
        migrate_legacy_clips()
        self._profile = self.load()

    @property
    def profile(self) -> Profile:
        return self._profile

    def load(self) -> Profile:
        ensure_data_dirs()
        migrate_legacy_clips()
        if PROFILE_PATH.exists():
            try:
                data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
                profile = Profile.model_validate(data)
                profile = ensure_profile_voices(profile)
                dirty = False
                if self._normalize_sequence_rows(profile):
                    dirty = True
                if self._normalize_under_two_min_rows(profile):
                    dirty = True
                if self._needs_schedule_migration(profile):
                    logger.info("Migrating schedule to joined under-2-minute duration clips")
                    profile.schedule = default_schedule()
                    dirty = True
                else:
                    sorted_rows = sort_schedule(profile.schedule)
                    if [r.id for r in sorted_rows] != [r.id for r in profile.schedule]:
                        profile.schedule = sorted_rows
                        dirty = True
                if dirty or not data.get("voices"):
                    self.save(profile)
                return profile
            except Exception as exc:
                logger.error("Failed to load profile: %s", exc)
        profile = default_profile()
        self.save(profile)
        return profile

    @staticmethod
    def _normalize_sequence_rows(profile: Profile) -> bool:
        """Preserve separate numeric roll-downs; normalize continuous clip ids."""
        dirty = False
        for row in profile.schedule:
            if row.row_type != "sequence":
                continue
            ids = list(row.sequence_clip_ids or [])
            all_numeric = bool(ids) and all(cid.isdigit() for cid in ids)
            if all_numeric and CONTINUOUS_ROLLDOWN_CLIP not in ids:
                if row.sequence_style != "separate":
                    row.sequence_style = "separate"
                    dirty = True
                if row.threshold_seconds is None:
                    row.threshold_seconds = 10.0
                    dirty = True
            elif row.sequence_style == "continuous":
                if ids != [CONTINUOUS_ROLLDOWN_CLIP]:
                    row.sequence_clip_ids = [CONTINUOUS_ROLLDOWN_CLIP]
                    dirty = True
                if row.threshold_seconds is None:
                    row.threshold_seconds = 10.0
                    dirty = True
            elif row.sequence_style == "separate" and not ids:
                row.sequence_clip_ids = list(SEPARATE_ROLLDOWN_CLIPS)
                dirty = True
        return dirty

    @staticmethod
    def _normalize_under_two_min_rows(profile: Profile) -> bool:
        """Ensure under-2-minute rows offer number-only and 'N seconds' variants (plus 1 minute at 60)."""
        dirty = False
        for row in profile.schedule:
            if row.row_type != "threshold" or row.threshold_seconds is None:
                continue
            s = int(round(row.threshold_seconds))
            if s not in UNDER_TWO_MIN_THRESHOLDS:
                continue

            ids: list[str] = []
            for v in row.variants:
                ids.extend(slot.clip_id for slot in v.slots)
            has_number = str(s) in ids
            has_joined = f"{s}_seconds" in ids
            has_minute = s == 60 and "1_minute" in ids
            expected_count = 3 if s == 60 else 2
            expected_ids = {f"n{s}", f"n{s}_seconds"} | ({"one_minute"} if s == 60 else set())
            have_ids = {v.id for v in row.variants}
            if (
                has_number
                and has_joined
                and (s != 60 or has_minute)
                and len(row.variants) >= expected_count
                and expected_ids <= have_ids
            ):
                continue

            prev_active = row.active_variant_id
            prev_variant = next((v for v in row.variants if v.id == prev_active), None)
            prev_slots = [slot.clip_id for slot in prev_variant.slots] if prev_variant else []

            row.variants = under_two_min_variants(s)
            row.label = (
                f"{s} / {s} seconds / 1 minute" if s == 60 else f"{s} / {s} seconds"
            )

            active = f"n{s}"
            if prev_active in {"forty_five_seconds", "sixty"} or prev_slots == [f"{s}_seconds"]:
                active = f"n{s}_seconds"
            elif prev_active in {"forty_five"} or prev_slots == [str(s)]:
                active = f"n{s}"
            elif prev_active == "one_minute" or "1_minute" in prev_slots:
                active = "one_minute"
            elif has_joined and not has_number:
                active = f"n{s}_seconds"
            elif prev_active in {v.id for v in row.variants}:
                active = prev_active

            row.active_variant_id = active
            dirty = True
        return dirty

    @staticmethod
    def _needs_schedule_migration(profile: Profile) -> bool:
        for row in profile.schedule:
            if row.row_type == "sequence" or row.threshold_seconds is None:
                continue
            t = int(round(row.threshold_seconds))
            if t not in _SPLIT_UNDER_2MIN:
                continue
            for variant in row.variants:
                ids = [s.clip_id for s in variant.slots]
                if str(t) in ids and "seconds" in ids and f"{t}_seconds" not in ids:
                    return True
                if t == 60 and "1" in ids and "minute" in ids and "1_minute" not in ids:
                    return True
        return False

    def save(self, profile: Profile | None = None) -> Profile:
        ensure_data_dirs()
        if profile is not None:
            self._profile = ensure_profile_voices(profile)
        PROFILE_PATH.write_text(
            self._profile.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return self._profile

    def update(self, **kwargs) -> Profile:
        data = self._profile.model_dump()
        data.update(kwargs)
        self._profile = Profile.model_validate(data)
        return self.save()


class ClipStore:
    def __init__(self, voices_dir: Path | None = None) -> None:
        ensure_data_dirs()
        migrate_legacy_clips()
        self.voices_dir = voices_dir or VOICES_DIR
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        (self.voices_dir / "default").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_id(value: str) -> str:
        return "".join(c for c in value if c.isalnum() or c in ("_", "-", ".")) or "x"

    def voice_dir(self, voice_id: str) -> Path:
        d = self.voices_dir / self._safe_id(voice_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, voice_id: str, clip_id: str) -> Path:
        return self.voice_dir(voice_id) / f"{self._safe_id(clip_id)}.wav"

    def exists(self, voice_id: str, clip_id: str) -> bool:
        return self.path_for(voice_id, clip_id).exists()

    def list_status(self, profile: Profile | None = None, voice_id: str | None = None) -> list[dict]:
        custom = profile.custom_phrases if profile else []
        schedule = profile.schedule if profile else []
        vid = voice_id or (profile.active_voice_id if profile else "default")
        lexicon = (
            lexicon_for_profile_schedule(schedule, custom)
            if profile
            else build_lexicon(custom)
        )
        result = []
        for clip in lexicon:
            path = self.path_for(vid, clip.id)
            result.append(
                {
                    **clip.model_dump(),
                    "recorded": path.exists(),
                    "path": str(path.name) if path.exists() else None,
                    "voice_id": vid,
                }
            )
        return result

    def phrase_bank_status(
        self, profile: Profile | None = None, voice_id: str | None = None
    ) -> list[dict]:
        custom = profile.custom_phrases if profile else []
        vid = voice_id or (profile.active_voice_id if profile else "default")
        items: list[ClipDefinition] = list(PHRASE_BANK)
        seen = {c.id for c in items}
        for c in custom:
            if c.id in seen:
                continue
            items.append(
                ClipDefinition(
                    id=c.id,
                    prompt=c.prompt,
                    say=c.say,
                    context=c.context or "Custom phrase",
                    kind="custom",
                )
            )
            seen.add(c.id)
        return [
            {
                **clip.model_dump(),
                "recorded": self.path_for(vid, clip.id).exists(),
                "voice_id": vid,
            }
            for clip in items
        ]

    def missing_for_profile(self, profile: Profile) -> list[str]:
        """Return 'voice_id/clip_id' strings for missing enabled-schedule clips."""
        missing = []
        for voice_id, clip_id in sorted(clip_refs_for_profile(profile)):
            if not self.exists(voice_id, clip_id):
                missing.append(f"{voice_id}/{clip_id}")
        return missing

    def missing_for_voice(self, profile: Profile, voice_id: str) -> list[str]:
        needed = clip_ids_for_schedule(profile.schedule)
        return sorted(cid for cid in needed if not self.exists(voice_id, cid))

    def save_bytes(
        self, voice_id: str, clip_id: str, raw: bytes, mime: str | None = None
    ) -> Path:
        dest = self.path_for(voice_id, clip_id)
        try:
            data, sr = sf.read(io.BytesIO(raw), always_2d=True, dtype="float32")
            data = normalize_for_clip(clip_id, data, int(sr))
            sf.write(str(dest), data, sr)
            return dest
        except Exception:
            pass

        tmp = self.voice_dir(voice_id) / f".upload_{self._safe_id(clip_id)}.bin"
        tmp.write_bytes(raw)
        wav_out = self._convert_with_ffmpeg(tmp, dest)
        tmp.unlink(missing_ok=True)
        if wav_out:
            try:
                data, sr = sf.read(str(dest), always_2d=True, dtype="float32")
                data = normalize_for_clip(clip_id, data, int(sr))
                sf.write(str(dest), data, sr)
            except Exception as exc:
                logger.warning("Clip normalise after ffmpeg failed: %s", exc)
            return dest

        raise ValueError(
            "Could not decode audio. Prefer WAV uploads, or install ffmpeg for webm/mp3."
        )

    def _convert_with_ffmpeg(self, src: Path, dest: Path) -> bool:
        import subprocess

        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(src),
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "48000",
                    str(dest),
                ],
                capture_output=True,
                timeout=60,
            )
            return proc.returncode == 0 and dest.exists()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def delete(self, voice_id: str, clip_id: str) -> bool:
        path = self.path_for(voice_id, clip_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def delete_voice_dir(self, voice_id: str) -> None:
        d = self.voices_dir / self._safe_id(voice_id)
        if d.exists() and d.is_dir():
            shutil.rmtree(d)

    def export_zip(self, profile: Profile) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("profile.json", profile.model_dump_json(indent=2))
            for voice in profile.voices or [VoicePack(id="default", name="Default")]:
                vdir = self.voice_dir(voice.id)
                for path in sorted(vdir.glob("*.wav")):
                    zf.write(path, arcname=f"voices/{voice.id}/{path.name}")
        return buf.getvalue()

    def import_zip(self, raw: bytes, replace: bool = True) -> Profile:
        ensure_data_dirs()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            names = zf.namelist()
            profile_name = next((n for n in names if n.endswith("profile.json")), None)
            if not profile_name:
                raise ValueError("ZIP missing profile.json")
            profile = Profile.model_validate_json(zf.read(profile_name))
            profile = ensure_profile_voices(profile)

            if replace:
                for existing in self.voices_dir.iterdir():
                    if existing.is_dir() and existing.name not in LOCKED_VOICE_IDS:
                        shutil.rmtree(existing)
                self.voices_dir.mkdir(parents=True, exist_ok=True)

            for name in names:
                norm = name.replace("\\", "/")
                # New format: voices/{id}/file.wav
                if "/voices/" in f"/{norm}" or norm.startswith("voices/"):
                    parts = Path(norm).parts
                    try:
                        vi = parts.index("voices")
                        voice_id = parts[vi + 1]
                        base = parts[vi + 2]
                    except (ValueError, IndexError):
                        continue
                    if voice_id in LOCKED_VOICE_IDS:
                        continue
                    if not base.endswith(".wav"):
                        continue
                    target = self.path_for(voice_id, Path(base).stem)
                    target.write_bytes(zf.read(name))
                    continue
                # Legacy flat clips/ → default voice
                if "/clips/" in f"/{norm}" or norm.startswith("clips/"):
                    if "default" in LOCKED_VOICE_IDS:
                        continue
                    base = Path(norm).name
                    if not base.endswith(".wav"):
                        continue
                    target = self.path_for("default", Path(base).stem)
                    target.write_bytes(zf.read(name))
        return profile


def ensure_wav_from_float32(path: Path, samples: np.ndarray, samplerate: int = 48000) -> None:
    sf.write(str(path), samples, samplerate)
