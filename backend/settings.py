from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_root() -> Path:
    """Read-only resources shipped with the app (source tree or PyInstaller)."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    """Writable profile/voices location."""
    if not _is_frozen():
        return _bundle_root() / "data"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "VTVocalCountdown"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VTVocalCountdown"
    else:
        base = Path.home() / ".local" / "share" / "VTVocalCountdown"
    return base


ROOT_DIR = _bundle_root()
DATA_DIR = _user_data_dir()
CLIPS_DIR = DATA_DIR / "clips"  # legacy; migrated into VOICES_DIR/default
VOICES_DIR = DATA_DIR / "voices"
LEAP_SECONDS_PATH = DATA_DIR / "leap_seconds.json"
PROFILE_PATH = DATA_DIR / "profile.json"
STATIC_DIR = ROOT_DIR / "static"
STOCK_VOICES_DIR = ROOT_DIR / "assets" / "stock_voices"

HOST = "0.0.0.0"
PORT = 5050

POLL_INTERVAL_SECONDS = 0.25
THRESHOLD_WINDOW_SECONDS = 0.75
# After media disappears from a poll (flaky OSC / brief disconnect), coast from the
# last good remaining time for this long before treating the VT as finished.
MEDIA_HOLDOVER_SECONDS = 10.0
# On resume from coast, keep fired thresholds if live remaining is within this of
# the extrapolated value and the cue id matches.
MEDIA_RESYNC_TOLERANCE_SECONDS = 2.0

DEFAULT_VMIX_PORT = 8088
DEFAULT_QLAB_SEND_PORT = 53000
DEFAULT_QLAB_LISTEN_PORT = 53001

# Stock Default voice is read-only while locked. Clear this set to unlock editing again.
# Example unlock: LOCKED_VOICE_IDS = frozenset()
LOCKED_VOICE_IDS: frozenset[str] = frozenset({"default"})


def voice_is_locked(voice_id: str) -> bool:
    return voice_id in LOCKED_VOICE_IDS


def seed_stock_voices() -> None:
    """Copy bundled Default wavs into DATA_DIR if the folder is missing or empty."""
    if not STOCK_VOICES_DIR.exists():
        return
    for src_dir in STOCK_VOICES_DIR.iterdir():
        if not src_dir.is_dir():
            continue
        dest = VOICES_DIR / src_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.glob("*.wav")):
            continue
        for wav in src_dir.glob("*.wav"):
            shutil.copy2(wav, dest / wav.name)


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    (VOICES_DIR / "default").mkdir(parents=True, exist_ok=True)
    seed_stock_voices()
