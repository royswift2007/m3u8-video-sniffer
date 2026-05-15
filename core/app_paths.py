"""
Runtime path helpers for source and PyInstaller execution.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


APP_DIR_NAME = "M3U8D"

_logger = logging.getLogger(__name__)


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


def get_component_update_state_path() -> Path:
    """Return the writable component update state file path."""
    return get_data_root() / "component_updates.json"


def get_component_update_temp_dir() -> Path:
    """Return the writable temp directory for component update assets."""
    return get_temp_dir() / "component_updates"


def get_component_backup_dir() -> Path:
    """Return the writable backup directory for component updates."""
    return get_data_root() / "component_backups"


def get_engine_paths_trusted_path() -> Path:
    """Return the writable trust registry path for user-authorized engine exe.

    Consumed by :mod:`utils.engine_paths`. The file records
    ``[{path, sha256, added_at}]`` entries that were explicitly approved by
    the user through the "Settings → Custom engine path" flow. On each
    startup the sha256 is recomputed and mismatching entries are dropped.
    """
    return get_data_root() / "engine_paths_trusted.json"


def get_safe_engine_roots() -> tuple[Path, ...]:
    """Return the safe roots that an engine exe must fall under.

    Per Requirement 7 of ``security-stability-hardening`` the allowed
    roots are:

    * the current package ``bin/`` directory (always), and
    * ``sys._MEIPASS/bin`` when running from a PyInstaller bundle.

    Paths are returned unresolved; callers that care about symlink escape
    should resolve both sides before comparing.
    """
    roots: list[Path] = [get_bin_dir()]
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "bin")
    return tuple(roots)


#: Snapshot of :func:`get_safe_engine_roots` taken at import time. Prefer
#: the function form from inside long-running processes if the runtime
#: bundle root can change (tests); keep this alias for readability at
#: call sites that do not need refreshable behavior.
SAFE_ENGINE_ROOTS: tuple[Path, ...] = get_safe_engine_roots()


def get_runtime_directories() -> tuple[Path, ...]:
    """Return runtime directories that should always exist."""
    return (
        get_data_root(),
        get_logs_dir(),
        get_temp_dir(),
        get_component_update_temp_dir(),
        get_component_backup_dir(),
    )


def initialize_runtime_directories() -> tuple[Path, ...]:
    """Create writable runtime directories when missing."""
    created_directories = []
    for directory in get_runtime_directories():
        directory.mkdir(parents=True, exist_ok=True)
        created_directories.append(directory)
    # R17.4: clean up any leftover ``*.bak`` siblings in ``bin/`` from a
    # previous component install. See
    # ``ComponentUpdateInstaller.cleanup_stale_backup_files``.
    try:
        from core.component_update_installer import ComponentUpdateInstaller
        ComponentUpdateInstaller.cleanup_stale_backup_files_static(get_bin_dir())
    except (OSError, ImportError) as exc:
        # Startup must never fail because of opportunistic .bak cleanup.
        # Redact the exception message — it may contain resolved bin/ paths
        # that, while not secret, aren't useful to users.
        _logger.debug(
            "app_paths: stale .bak cleanup skipped (%s)", type(exc).__name__
        )
    return tuple(created_directories)
