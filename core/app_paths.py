"""
Runtime path helpers for source and PyInstaller execution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "M3U8D"


def is_frozen() -> bool:
    """Return True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _get_user_data_root() -> Path:
    """Return a writable per-user data root on Windows."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_DIR_NAME
    return Path.home() / "AppData" / "Roaming" / APP_DIR_NAME


def get_app_root() -> Path:
    """Return the external runtime root directory."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_bundle_root() -> Path:
    """Return the internal bundle root when available."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
    return get_app_root()


def get_data_root() -> Path:
    """Return the writable data directory for logs/temp/state."""
    if is_frozen():
        return _get_user_data_root()
    return get_app_root()


def resolve_app_path(relative_path: str | Path) -> Path:
    """Resolve an app-relative path against the runtime root."""
    path = Path(relative_path)
    if path.is_absolute():
        return path
    return get_app_root() / path


def get_bin_dir() -> Path:
    """Return the external bin directory."""
    return get_app_root() / "bin"


def get_bin_path(*parts: str) -> Path:
    """Return a path under the external bin directory."""
    return get_bin_dir().joinpath(*parts)


def get_resources_dir() -> Path:
    """Return the resources directory, preferring the external install layout."""
    app_resources = get_app_root() / "resources"
    if app_resources.exists():
        return app_resources
    return get_bundle_root() / "resources"


def get_resource_path(*parts: str) -> Path:
    """Return a path under the resources directory."""
    return get_resources_dir().joinpath(*parts)


def get_config_path() -> Path:
    """Return the writable config file path."""
    return get_app_root() / "config.json"


def get_dependency_manifest_path() -> Path:
    """Return the dependency manifest file path."""
    return get_app_root() / "deps.json"


def get_logs_dir() -> Path:
    """Return the writable logs directory."""
    return get_data_root() / "logs"


def get_temp_dir() -> Path:
    """Return the writable temp directory."""
    return get_data_root() / "Temp"


def get_runtime_directories() -> tuple[Path, ...]:
    """Return runtime directories that should always exist."""
    return get_data_root(), get_logs_dir(), get_temp_dir()


def initialize_runtime_directories() -> tuple[Path, ...]:
    """Create writable runtime directories when missing."""
    created_directories = []
    for directory in get_runtime_directories():
        directory.mkdir(parents=True, exist_ok=True)
        created_directories.append(directory)
    return tuple(created_directories)
