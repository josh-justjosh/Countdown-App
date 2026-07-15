from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import socketio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.engine.countdown_engine import CountdownEngine
from backend.engine.leap_seconds import refresh_leap_seconds
from backend.engine.pips_scheduler import PipsScheduler
from backend.engine.player import AudioPlayer
from backend.models import (
    CLIP_LEXICON,
    ClipSlot,
    ConnectionConfig,
    CustomPhrase,
    PipsConfig,
    Profile,
    QLabCueSettings,
    ScheduleRow,
    ScheduleVariant,
    VoicePack,
    auto_slots_for_threshold,
    default_profile,
    ensure_profile_voices,
    format_threshold_label,
    parse_time_to_seconds,
    slugify_phrase,
    sort_schedule,
)
from backend.qlab_bridge import QLabBridge
from backend.settings import (
    HOST,
    PORT,
    STATIC_DIR,
    VOICES_DIR,
    LOCKED_VOICE_IDS,
    ensure_data_dirs,
    voice_is_locked,
)
from backend.sources.vmix import VmixSource
from backend.store import ClipStore, ProfileStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ensure_data_dirs()

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio

    emit_sync._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    pips_scheduler.start()
    yield
    pips_scheduler.stop()
    if engine.armed:
        engine.disarm()
    qlab_bridge.disconnect()


fastapi_app = FastAPI(title="VT Vocal Countdown", lifespan=lifespan)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

profile_store = ProfileStore()
clip_store = ClipStore()
player = AudioPlayer()
player.set_device(profile_store.profile.output_device_id)


def emit_sync(event: str, data: dict) -> None:
    """Schedule emit from sync engine thread."""
    try:
        import asyncio

        loop = getattr(emit_sync, "_loop", None)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(sio.emit(event, data), loop)
    except Exception as exc:
        logger.debug("emit failed: %s", exc)


qlab_bridge = QLabBridge(emit=emit_sync)
engine = CountdownEngine(
    player=player,
    voices_dir=VOICES_DIR,
    emit=emit_sync,
    qlab_bridge=qlab_bridge,
)


def _persist_pips_side_effects() -> None:
    """Persist profile if scheduler cleared simulate flag."""
    profile_store.save()


def _on_pips_fired(_event) -> None:  # noqa: ANN001
    _persist_pips_side_effects()


pips_scheduler = PipsScheduler(
    player=player,
    get_profile=lambda: profile_store.profile,
    on_fired=_on_pips_fired,
)


def _cues_for_ui(profile: Profile | None = None) -> list[dict]:
    profile = profile or profile_store.profile
    status = qlab_bridge.status()
    cues = status.get("cues") or []
    initialized = profile.connection.qlab_cues_initialized
    enabled = set(profile.connection.qlab_enabled_cue_ids)
    settings_map = profile.connection.qlab_cue_settings or {}
    result = []
    for cue in cues:
        uid = cue.get("unique_id")
        cue_settings = settings_map.get(uid) if uid else None
        result.append(
            {
                **cue,
                "countdown_enabled": (uid in enabled) if initialized else True,
                "voice_id": cue_settings.voice_id if cue_settings else None,
            }
        )
    return result


def _qlab_payload(profile: Profile | None = None) -> dict:
    profile = profile or profile_store.profile
    status = qlab_bridge.status()
    return {
        **status,
        "cues": _cues_for_ui(profile),
    }


def _emit_qlab() -> None:
    emit_sync("qlab_status", _qlab_payload())


# Prefer enriched cue payloads on the wire
qlab_bridge.emit = lambda event, _data: _emit_qlab() if event == "qlab_status" else emit_sync(event, _data)


@fastapi_app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@fastapi_app.get("/api/profile")
def get_profile() -> dict:
    return profile_store.profile.model_dump()


@fastapi_app.put("/api/profile")
def put_profile(profile: Profile) -> dict:
    if engine.armed:
        raise HTTPException(400, "Disarm before changing profile")
    saved = profile_store.save(profile)
    player.set_device(saved.output_device_id)
    return saved.model_dump()


class ConnectionUpdate(BaseModel):
    connection: ConnectionConfig


@fastapi_app.put("/api/profile/connection")
def put_connection(body: ConnectionUpdate) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    prev_mode = profile.connection.mode
    # Preserve live enable list / initialized unless client sends them
    incoming = body.connection
    profile.connection = incoming
    saved = profile_store.save(profile)
    if prev_mode == "qlab" and body.connection.mode != "qlab":
        qlab_bridge.disconnect()
    if engine.armed:
        engine.update_profile(saved)
    return saved.model_dump()


class QlabEnabledUpdate(BaseModel):
    enabled_cue_ids: list[str]
    cue_voices: dict[str, str | None] | None = None


@fastapi_app.get("/api/qlab/status")
def qlab_status() -> dict:
    return _qlab_payload()


@fastapi_app.post("/api/qlab/connect")
def qlab_connect() -> dict:
    profile = profile_store.profile
    if profile.connection.mode != "qlab":
        raise HTTPException(400, "Switch source mode to QLab first")
    result = qlab_bridge.connect(profile.connection, profile)
    # First successful connect: default-enable all duration cues
    if result.get("success") and not profile.connection.qlab_cues_initialized:
        cues = result.get("cues") or qlab_bridge.status().get("cues") or []
        ids = [c["unique_id"] for c in cues if c.get("unique_id")]
        profile = profile.model_copy(deep=True)
        profile.connection.qlab_enabled_cue_ids = ids
        profile.connection.qlab_cues_initialized = True
        profile_store.save(profile)
        qlab_bridge.apply_enabled(profile)
        result = {**result, **_qlab_payload(profile)}
    else:
        result = {**result, **_qlab_payload()}
    return result


@fastapi_app.post("/api/qlab/disconnect")
def qlab_disconnect() -> dict:
    if engine.armed and profile_store.profile.connection.mode == "qlab":
        raise HTTPException(400, "Disarm before disconnecting QLab")
    return {**qlab_bridge.disconnect(), "cues": []}


@fastapi_app.post("/api/qlab/refresh")
def qlab_refresh() -> dict:
    profile = profile_store.profile
    result = qlab_bridge.refresh(profile)
    # Auto-enable newly discovered cues when initialized
    if result.get("success") and profile.connection.qlab_cues_initialized:
        cues = qlab_bridge.status().get("cues") or []
        known = set(profile.connection.qlab_enabled_cue_ids)
        # Also track previously disabled: only add brand-new IDs as enabled
        # We need known disabled set — use: any id not in enabled and was seen before.
        # Simpler: new IDs (not in previous enabled list AND not already stored as known)
        # Just enable new ones that aren't in the enabled list and weren't intentionally disabled...
        # Store approach: enabled list is source of truth; new cues get enabled.
        all_ids = {c["unique_id"] for c in cues if c.get("unique_id")}
        previous_all = set(getattr(qlab_refresh, "_seen_ids", set()))  # type: ignore[attr-defined]
        new_ids = all_ids - previous_all - known
        qlab_refresh._seen_ids = all_ids  # type: ignore[attr-defined]
        if new_ids:
            profile = profile.model_copy(deep=True)
            profile.connection.qlab_enabled_cue_ids = sorted(known | new_ids)
            profile_store.save(profile)
            qlab_bridge.apply_enabled(profile)
            if engine.armed:
                engine.update_profile(profile)
    return {**result, **_qlab_payload()}


@fastapi_app.put("/api/qlab/enabled")
def qlab_set_enabled(body: QlabEnabledUpdate) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.connection.qlab_enabled_cue_ids = list(body.enabled_cue_ids)
    profile.connection.qlab_cues_initialized = True
    if body.cue_voices is not None:
        settings = dict(profile.connection.qlab_cue_settings)
        for uid, voice_id in body.cue_voices.items():
            if not uid:
                continue
            if voice_id:
                settings[uid] = QLabCueSettings(voice_id=voice_id)
            else:
                settings.pop(uid, None)
        profile.connection.qlab_cue_settings = settings
    profile_store.save(profile)
    qlab_bridge.apply_enabled(profile)
    if engine.armed:
        engine.update_profile(profile)
    return {"success": True, **_qlab_payload(profile)}


class ScheduleUpdate(BaseModel):
    schedule: list[ScheduleRow]


@fastapi_app.put("/api/profile/schedule")
def put_schedule(body: ScheduleUpdate) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.schedule = sort_schedule(body.schedule)
    return profile_store.save(profile).model_dump()


class CustomPhraseCreate(BaseModel):
    prompt: str
    say: str | None = None


@fastapi_app.post("/api/profile/custom-phrases")
def add_custom_phrase(body: CustomPhraseCreate) -> dict:
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt required")
    say = (body.say or prompt).strip()
    base_id = slugify_phrase(prompt)
    profile = profile_store.profile.model_copy(deep=True)
    existing = {c.id for c in profile.custom_phrases}
    clip_id = base_id
    n = 2
    while clip_id in existing or clip_id in {c.id for c in CLIP_LEXICON}:
        clip_id = f"{base_id}_{n}"
        n += 1
    profile.custom_phrases.append(
        CustomPhrase(id=clip_id, prompt=prompt, say=say, context="Custom phrase")
    )
    profile_store.save(profile)
    return {
        "success": True,
        "phrase": profile.custom_phrases[-1].model_dump(),
        "profile": profile.model_dump(),
    }


@fastapi_app.delete("/api/profile/custom-phrases/{phrase_id}")
def delete_custom_phrase(phrase_id: str) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.custom_phrases = [c for c in profile.custom_phrases if c.id != phrase_id]
    profile_store.save(profile)
    return {"success": True, "profile": profile.model_dump()}


class AddScheduleRowBody(BaseModel):
    value: str
    unit: str = "auto"


@fastapi_app.post("/api/profile/schedule/add")
def add_schedule_row(body: AddScheduleRowBody) -> dict:
    seconds = parse_time_to_seconds(body.value, body.unit)
    if seconds is None or seconds <= 0:
        raise HTTPException(400, "Could not parse time value")
    profile = profile_store.profile.model_copy(deep=True)
    for row in profile.schedule:
        if row.row_type == "threshold" and row.threshold_seconds == seconds:
            raise HTTPException(400, f"A row already exists for {seconds}s")
    slots = auto_slots_for_threshold(seconds)
    new_row = ScheduleRow(
        id=f"t{int(seconds)}",
        threshold_seconds=float(seconds),
        label=format_threshold_label(seconds),
        variants=[
            ScheduleVariant(
                id="default",
                label="Default",
                slots=[ClipSlot(clip_id=s) for s in slots],
            )
        ],
        active_variant_id="default",
    )
    profile.schedule = sort_schedule([*profile.schedule, new_row])
    profile_store.save(profile)
    return profile.model_dump()


@fastapi_app.delete("/api/profile/schedule/{row_id}")
def delete_schedule_row(row_id: str) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.schedule = sort_schedule([r for r in profile.schedule if r.id != row_id])
    profile_store.save(profile)
    return profile.model_dump()


class DeviceUpdate(BaseModel):
    output_device_id: int | None = None
    output_device_name: str | None = None


@fastapi_app.put("/api/profile/device")
def put_device(body: DeviceUpdate) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.output_device_id = body.output_device_id
    profile.output_device_name = body.output_device_name
    player.set_device(body.output_device_id)
    return profile_store.save(profile).model_dump()


def _pips_payload() -> dict:
    profile = profile_store.profile
    nxt = pips_scheduler.peek_next()
    return {
        "pips": profile.pips.model_dump(),
        "next": nxt,
        "next_at": nxt["at"] if nxt else None,
        "next_leap_kind": nxt["leap_kind"] if nxt else "none",
    }


@fastapi_app.get("/api/profile/pips")
def get_pips() -> dict:
    cfg = profile_store.profile.pips
    if cfg.leap_seconds_auto_update:
        refresh_leap_seconds(force=False)
    return _pips_payload()


@fastapi_app.put("/api/profile/pips")
def put_pips(body: PipsConfig) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.pips = body
    profile_store.save(profile)
    pips_scheduler.notify_config_changed()
    return _pips_payload()


@fastapi_app.post("/api/profile/pips/leap-seconds/refresh")
def refresh_pips_leap_seconds() -> dict:
    result = refresh_leap_seconds(force=True)
    pips_scheduler.notify_config_changed()
    return {**result, **_pips_payload()}


@fastapi_app.post("/api/profile/pips/test")
def test_pips() -> dict:
    """Play a normal pip sequence immediately (for modal testing)."""
    from backend.engine.pips_scheduler import generate_pips_pcm

    player.set_device(profile_store.profile.output_device_id)
    player.play_array(generate_pips_pcm("none"), 48000, priority=-1, interrupt=False)
    return {"success": True}


@fastapi_app.post("/api/profile/reset-schedule")
def reset_schedule() -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    profile.schedule = sort_schedule(default_profile().schedule)
    return profile_store.save(profile).model_dump()


@fastapi_app.get("/api/devices")
def list_devices() -> list[dict]:
    try:
        return player.list_devices()
    except Exception as exc:
        logger.error("Device list failed: %s", exc)
        return []


@fastapi_app.get("/api/clips")
def list_clips() -> dict:
    profile = profile_store.profile
    voice_id = profile.active_voice_id
    return _clips_payload(profile, voice_id)


def _clips_payload(profile: Profile, voice_id: str) -> dict:
    missing = clip_store.missing_for_profile(profile)
    lexicon = clip_store.list_status(profile, voice_id=voice_id)
    voice_missing = clip_store.missing_for_voice(profile, voice_id)
    return {
        "voice_id": voice_id,
        "lexicon": lexicon,
        "phrase_bank": clip_store.phrase_bank_status(profile, voice_id=voice_id),
        "missing": missing,
        "voice_missing": voice_missing,
        "complete": len(missing) == 0,
        "total": len(lexicon),
        "recorded_count": sum(1 for c in lexicon if c["recorded"]),
        "recorded_ids": [c["id"] for c in lexicon if c["recorded"]],
    }


def _ensure_voice_writable(voice_id: str) -> None:
    if voice_is_locked(voice_id):
        raise HTTPException(
            403,
            "This voice pack is locked and cannot be edited. Create a new voice to record.",
        )


@fastapi_app.get("/api/voices")
def list_voices() -> dict:
    profile = ensure_profile_voices(profile_store.profile.model_copy(deep=True))
    return {
        "voices": [v.model_dump() for v in profile.voices],
        "active_voice_id": profile.active_voice_id,
        "locked_voice_ids": sorted(LOCKED_VOICE_IDS),
    }


class VoiceCreate(BaseModel):
    name: str
    id: str | None = None


@fastapi_app.post("/api/voices")
def create_voice(body: VoiceCreate) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    profile = profile_store.profile.model_copy(deep=True)
    profile = ensure_profile_voices(profile)
    base = slugify_phrase(body.id or name)
    existing = {v.id for v in profile.voices}
    voice_id = base
    n = 2
    while voice_id in existing:
        voice_id = f"{base}_{n}"
        n += 1
    profile.voices.append(VoicePack(id=voice_id, name=name))
    clip_store.voice_dir(voice_id)
    profile_store.save(profile)
    return {
        "success": True,
        "voice": profile.voices[-1].model_dump(),
        "profile": profile.model_dump(),
    }


class VoiceUpdate(BaseModel):
    name: str


@fastapi_app.patch("/api/voices/{voice_id}")
def rename_voice(voice_id: str, body: VoiceUpdate) -> dict:
    _ensure_voice_writable(voice_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    profile = profile_store.profile.model_copy(deep=True)
    hit = next((v for v in profile.voices if v.id == voice_id), None)
    if not hit:
        raise HTTPException(404, "Voice not found")
    hit.name = name
    profile_store.save(profile)
    return {"success": True, "voice": hit.model_dump(), "profile": profile.model_dump()}


@fastapi_app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str) -> dict:
    _ensure_voice_writable(voice_id)
    profile = profile_store.profile.model_copy(deep=True)
    profile = ensure_profile_voices(profile)
    if len(profile.voices) <= 1:
        raise HTTPException(400, "Cannot delete the last voice pack")
    if not any(v.id == voice_id for v in profile.voices):
        raise HTTPException(404, "Voice not found")
    profile.voices = [v for v in profile.voices if v.id != voice_id]
    for row in profile.schedule:
        if row.voice_id == voice_id:
            row.voice_id = None
    if profile.active_voice_id == voice_id:
        profile.active_voice_id = profile.voices[0].id
    clip_store.delete_voice_dir(voice_id)
    profile_store.save(profile)
    return {"success": True, "profile": profile.model_dump()}


class ActiveVoiceBody(BaseModel):
    active_voice_id: str


@fastapi_app.put("/api/profile/active-voice")
def put_active_voice(body: ActiveVoiceBody) -> dict:
    profile = profile_store.profile.model_copy(deep=True)
    if not any(v.id == body.active_voice_id for v in profile.voices):
        raise HTTPException(400, "Unknown voice id")
    profile.active_voice_id = body.active_voice_id
    return profile_store.save(profile).model_dump()


@fastapi_app.get("/api/voices/{voice_id}/clips")
def list_voice_clips(voice_id: str) -> dict:
    profile = profile_store.profile
    if not any(v.id == voice_id for v in profile.voices):
        raise HTTPException(404, "Voice not found")
    return _clips_payload(profile, voice_id)


@fastapi_app.post("/api/voices/{voice_id}/clips/{clip_id}")
async def upload_voice_clip(voice_id: str, clip_id: str, file: UploadFile = File(...)) -> dict:
    _ensure_voice_writable(voice_id)
    profile = profile_store.profile
    if not any(v.id == voice_id for v in profile.voices):
        raise HTTPException(404, "Voice not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    try:
        path = clip_store.save_bytes(voice_id, clip_id, raw, file.content_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True, "clip_id": clip_id, "voice_id": voice_id, "path": path.name}


@fastapi_app.delete("/api/voices/{voice_id}/clips/{clip_id}")
def delete_voice_clip(voice_id: str, clip_id: str) -> dict:
    _ensure_voice_writable(voice_id)
    ok = clip_store.delete(voice_id, clip_id)
    return {"success": ok}


@fastapi_app.get("/api/voices/{voice_id}/clips/{clip_id}/audio")
def get_voice_clip_audio(voice_id: str, clip_id: str) -> FileResponse:
    path = clip_store.path_for(voice_id, clip_id)
    if not path.exists():
        raise HTTPException(404, "Clip not recorded")
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@fastapi_app.post("/api/voices/{voice_id}/clips/{clip_id}/preview")
def preview_voice_clip(voice_id: str, clip_id: str, wait: bool = False) -> dict:
    path = clip_store.path_for(voice_id, clip_id)
    if not path.exists():
        raise HTTPException(404, "Clip not recorded")
    player.set_device(profile_store.profile.output_device_id)
    player.play_files([path], priority=999, wait=wait)
    return {"success": True}


@fastapi_app.post("/api/audio/stop")
def stop_audio() -> dict:
    player.stop()
    return {"success": True}


@fastapi_app.post("/api/clips/{clip_id}")
async def upload_clip(clip_id: str, file: UploadFile = File(...)) -> dict:
    voice_id = profile_store.profile.active_voice_id
    return await upload_voice_clip(voice_id, clip_id, file)


@fastapi_app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str) -> dict:
    voice_id = profile_store.profile.active_voice_id
    return delete_voice_clip(voice_id, clip_id)


@fastapi_app.get("/api/clips/{clip_id}/audio")
def get_clip_audio(clip_id: str) -> FileResponse:
    voice_id = profile_store.profile.active_voice_id
    return get_voice_clip_audio(voice_id, clip_id)


@fastapi_app.post("/api/clips/{clip_id}/preview")
def preview_clip(clip_id: str) -> dict:
    voice_id = profile_store.profile.active_voice_id
    return preview_voice_clip(voice_id, clip_id)


class TestConnectionBody(BaseModel):
    connection: ConnectionConfig | None = None


@fastapi_app.post("/api/test-connection")
def test_connection(body: TestConnectionBody | None = None) -> dict:
    conn = (body.connection if body and body.connection else profile_store.profile.connection)
    if conn.mode == "vmix":
        source = VmixSource(conn.vmix_ip, conn.vmix_port, conn.vmix_fallback1, conn.vmix_fallback2)
        result = source.test_connection()
        return {
            "connected": result.connected,
            "message": result.message,
            "remaining_seconds": result.seconds,
            "label": result.label,
        }
    if conn.mode == "qlab":
        profile = profile_store.profile.model_copy(deep=True)
        profile.connection = conn
        profile_store.save(profile)
        result = qlab_bridge.connect(conn, profile)
        if result.get("success") and not profile.connection.qlab_cues_initialized:
            cues = qlab_bridge.status().get("cues") or []
            ids = [c["unique_id"] for c in cues if c.get("unique_id")]
            profile.connection.qlab_enabled_cue_ids = ids
            profile.connection.qlab_cues_initialized = True
            profile_store.save(profile)
            qlab_bridge.apply_enabled(profile)
        payload = _qlab_payload()
        return {
            "connected": bool(result.get("success")),
            "message": result.get("message") or "",
            "remaining_seconds": None,
            "label": None,
            "qlab": payload,
        }
    return {"connected": False, "message": "Timed mode is not available yet"}


@fastapi_app.post("/api/arm")
def arm() -> dict:
    profile = profile_store.profile
    if profile.connection.mode == "timed":
        return {"success": False, "message": "Timed mode is not available yet"}
    missing = clip_store.missing_for_profile(profile)
    if missing:
        return {
            "success": False,
            "message": "Missing clips for enabled schedule rows",
            "missing": missing,
        }
    return engine.arm(profile)


@fastapi_app.post("/api/disarm")
def disarm() -> dict:
    return engine.disarm()


@fastapi_app.get("/api/status")
def status() -> dict:
    return engine.status()


@fastapi_app.get("/api/export")
def export_profile() -> Response:
    data = clip_store.export_zip(profile_store.profile)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=vt-countdown-pack.zip"},
    )


@fastapi_app.post("/api/import")
async def import_profile(file: UploadFile = File(...), replace: bool = True) -> dict:
    if engine.armed:
        raise HTTPException(400, "Disarm before importing")
    raw = await file.read()
    try:
        profile = clip_store.import_zip(raw, replace=replace)
    except Exception as exc:
        raise HTTPException(400, f"Import failed: {exc}") from exc
    profile_store.save(profile)
    player.set_device(profile.output_device_id)
    return {"success": True, "profile": profile.model_dump()}


@sio.event
async def connect(sid, environ):  # noqa: ANN001
    await sio.emit("countdown_tick", engine.status(), to=sid)
    await sio.emit("engine_status", engine.status(), to=sid)
    await sio.emit("qlab_status", _qlab_payload(), to=sid)


@sio.event
async def disconnect(sid):  # noqa: ANN001
    logger.info("Client disconnected: %s", sid)


@sio.event
async def request_status(sid):  # noqa: ANN001
    await sio.emit("countdown_tick", engine.status(), to=sid)


if STATIC_DIR.exists():
    @fastapi_app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @fastapi_app.get("/display")
    def display() -> FileResponse:
        return FileResponse(STATIC_DIR / "display.html")

    @fastapi_app.get("/voice")
    def voice_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "voice.html")

    @fastapi_app.get("/wizard")
    def wizard(clip: str | None = None, voice: str | None = None, mode: str | None = None) -> RedirectResponse:
        from urllib.parse import urlencode

        params = {"mode": mode or "pack"}
        if clip:
            params["clip"] = clip
        if voice:
            params["voice"] = voice
        return RedirectResponse(url=f"/voice?{urlencode(params)}", status_code=302)

    fastapi_app.mount(
        "/assets",
        StaticFiles(directory=STATIC_DIR),
        name="assets",
    )


app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)


def main() -> None:
    import os
    import sys
    import threading
    import time
    import webbrowser

    import uvicorn

    ensure_data_dirs()
    logger.info("Starting VT Vocal Countdown on http://%s:%s", HOST, PORT)
    logger.info("Static dir: %s | Data dir: %s", STATIC_DIR, VOICES_DIR.parent)

    open_browser = os.environ.get("COUNTDOWN_OPEN_BROWSER", "").strip() in ("1", "true", "yes")
    if getattr(sys, "frozen", False):
        open_browser = True

    if open_browser:

        def _open() -> None:
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

        threading.Thread(target=_open, daemon=True).start()

    # String import fails under PyInstaller; pass the ASGI app object when frozen.
    if getattr(sys, "frozen", False):
        uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="info")
    else:
        uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False, factory=False)


if __name__ == "__main__":
    main()
