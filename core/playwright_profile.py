"""
Helpers for choosing safe Playwright profile directories.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.app_paths import APP_DIR_NAME, get_temp_dir


def get_primary_user_data_dir() -> Path:
    """Return the persistent browser profile directory."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_DIR_NAME / "chromium_user_data"
    return Path.home() / "AppData" / "Roaming" / APP_DIR_NAME / "chromium_user_data"


def create_temporary_user_data_dir() -> Path:
    """Create an isolated fallback profile under the runtime temp directory."""
    profile_root = get_temp_dir() / "playwright_profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="profile_", dir=str(profile_root)))


def is_profile_lock_error(error: Exception) -> bool:
    """Best-effort detection for profile-in-use startup failures."""
    text = str(error or "").lower()
    if not text:
        return False

    needles = (
        "singletonlock",
        "singletoncookie",
        "singletonsocket",
        "user data directory is already in use",
        "profile appears to be in use",
        "another browser is using",
    )
    return any(needle in text for needle in needles)
