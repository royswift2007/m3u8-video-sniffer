"""
Safe component update installation: preflight, backup, atomic replacement, post-check, and rollback.

This module intentionally exposes structured result objects instead of leaking
operational exceptions to UI callers. Tests may inject temp roots, target path
resolvers, version probes, and replace functions to keep smoke checks offline and
away from the real bin directory.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from core.app_paths import get_component_backup_dir
from core.component_update_downloader import ComponentDownloadStageResult
from core.component_update_models import ComponentVersionInfo
from core.component_update_state import ComponentUpdateStateStore
from core.component_version_probe import ComponentVersionProbe
from core.dependency_manifest import DependencyEntry


class ComponentVersionProbeLike(Protocol):
    """Minimal protocol accepted by the installer for post-install probing."""

    def probe(self, entry: DependencyEntry) -> ComponentVersionInfo:
        """Probe a dependency entry and return a structured version result."""


class ComponentUpdateStateStoreLike(Protocol):
    """Minimal protocol for the state store used to look up expected sha256 fallback."""

    def get_component_state(self, component_id: str) -> dict[str, Any]:
        """Return a copy of one component state."""


@dataclass(frozen=True)
class ComponentInstallResult:
    """Structured result for an install attempt."""

    component_id: str
    success: bool
    code: str
    message: str
    target_path: str | None = None
    staged_exe_path: str | None = None
    staging_dir: str | None = None
    backup_path: str | None = None
    old_version: str | None = None
    new_version: str | None = None
    post_check_error: str | None = None
    warning: str | None = None
    rollback_attempted: bool = False
    rollback_success: bool | None = None
    rollback_error: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentInstallPlan:
    """Validated filesystem plan for replacing one component executable."""

    component_id: str
    target_path: Path
    staged_exe_path: Path
    staging_dir: Path
    backup_dir: Path
    expected_version: str | None = None


class ComponentUpdateInstaller:
    """Install a staged component executable with backup and rollback safety."""

    def __init__(
        self,
        backup_root: Path | None = None,
        version_probe: ComponentVersionProbeLike | None = None,
        target_path_resolver: Callable[[DependencyEntry], Path] | None = None,
        replace_func: Callable[[Path, Path], None] | None = None,
        time_func: Callable[[], float] | None = None,
        state_store: ComponentUpdateStateStoreLike | None = None,
    ):
        self.backup_root = Path(backup_root) if backup_root is not None else get_component_backup_dir()
        self.version_probe = version_probe or ComponentVersionProbe()
        self.target_path_resolver = target_path_resolver or (lambda entry: entry.path)
        self.replace_func = replace_func or os.replace
        self.time_func = time_func or time.time
        self.state_store = state_store or ComponentUpdateStateStore()

    def install_staged_update(
        self,
        download_result: ComponentDownloadStageResult,
        entry: DependencyEntry,
        expected_version: str | None = None,
    ) -> ComponentInstallResult:
        """Install a previously staged executable and return a structured result."""
        component_id = entry.id
        plan_result = self.build_install_plan(download_result, entry, expected_version=expected_version)
        if isinstance(plan_result, ComponentInstallResult):
            return plan_result
        plan = plan_result

        # R1.6 pre-replace re-verification: recompute sha256 of the staged file
        # and compare against the digest captured at download time (and
        # persisted in state.json via ``last_download``). A mismatch means the
        # staged artifact was tampered with between download and install.
        # ``bin/*.exe`` must stay untouched on failure, so this runs before any
        # backup / probe / replace that could mutate the target tree.
        tamper_check = self._verify_staged_sha256(download_result, plan)
        if tamper_check is not None:
            return tamper_check

        target_path = plan.target_path
        old_version = self._probe_version(entry)
        backup_path: Path | None = None

        # R17.2 / R17.3 disk pre-check: require ``free >= need * 1.2`` in the
        # target parent volume before mutating ``bin/``. ``need`` is the staged
        # executable size; the 20% safety margin keeps enough headroom for the
        # sibling ``.bak`` copy + same-volume temp copy used by
        # ``replace_atomically``. On insufficient disk the install aborts with
        # a structured ``insufficient_disk`` code carrying the observed
        # ``need`` / ``free`` bytes so the UI / scheduler can render the
        # actionable error. Nothing under ``bin/`` is touched in this branch.
        disk_check = self._check_disk_space(plan)
        if disk_check is not None:
            return disk_check

        process_free = self.ensure_process_free(target_path, component_id)
        if process_free is not None:
            # R17.1: an in-use executable is no longer treated as a hard
            # failure. It is surfaced to downstream UI via a dedicated
            # ``deferred_pending_restart`` code so the scheduler can retry the
            # install on the next launch once the component is no longer
            # locked. ``ensure_process_free`` continues to return the
            # lower-level ``"process_in_use"`` sentinel for introspection.
            if process_free == "process_in_use":
                return self._failure_from_plan(
                    plan,
                    "deferred_pending_restart",
                    "component is currently running; scheduled to retry on next restart",
                    old_version=old_version,
                )
            return self._failure_from_plan(plan, process_free, process_free, old_version=old_version)

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._failure_from_plan(
                plan,
                self._classify_os_error(exc, default="target_dir_failed"),
                f"failed to create target directory: {exc}",
                old_version=old_version,
            )

        backup_result = self.backup_current(target_path, component_id, old_version)
        if isinstance(backup_result, ComponentInstallResult):
            return self._copy_failure_with_plan(backup_result, plan, old_version=old_version)
        backup_path = backup_result

        # R17.4 short-lived sibling rollback point: before the atomic replace,
        # rename the current target to ``target + ".bak"``. The full
        # ``backup_root`` copy above remains the long-term backup; this
        # ``.bak`` sibling lives next to the target so a replace failure can
        # be rolled back with a single same-volume ``os.replace``. On success
        # the ``.bak`` is intentionally kept and cleaned up on the next
        # startup via ``cleanup_stale_backup_files``.
        bak_sibling: Path | None = None
        if target_path.exists():
            try:
                bak_sibling = self._sibling_bak_path(target_path)
                self._remove_stale_sibling_bak(bak_sibling)
                self.replace_func(target_path, bak_sibling)
            except OSError as exc:
                code = self._classify_os_error(exc, default="replace_failed")
                hint = "may require administrator privileges or installing M3U8D to a writable directory"
                return self._failure_from_plan(
                    plan,
                    code,
                    f"failed to stage rollback sibling {bak_sibling}: {exc}; {hint}",
                    backup_path=backup_path,
                    old_version=old_version,
                )

        replace_error = self.replace_atomically(plan.staged_exe_path, target_path)
        if replace_error is not None:
            # Restore the ``.bak`` sibling so ``bin/`` remains intact when the
            # replace fails. If restoration itself fails we still surface the
            # original error code but annotate the message so operators can
            # recover manually (the ``.bak`` file stays in place).
            sibling_restore_error: str | None = None
            if bak_sibling is not None and bak_sibling.exists():
                try:
                    self.replace_func(bak_sibling, target_path)
                except OSError as restore_exc:
                    sibling_restore_error = f"sibling rollback failed: {restore_exc}"
            message = replace_error[1]
            if sibling_restore_error:
                message = f"{message}; {sibling_restore_error}"
            return self._failure_from_plan(
                plan,
                replace_error[0],
                message,
                backup_path=backup_path,
                old_version=old_version,
            )

        post_check = self.verify_after_replace(entry, expected_version=plan.expected_version, target_path=target_path)
        if post_check.success:
            return ComponentInstallResult(
                component_id=component_id,
                success=True,
                code=post_check.code,
                message=post_check.message if post_check.warning else "component installed successfully",
                target_path=str(target_path),
                staged_exe_path=str(plan.staged_exe_path),
                staging_dir=str(plan.staging_dir),
                backup_path=str(backup_path) if backup_path else None,
                old_version=old_version,
                new_version=post_check.new_version,
                post_check_error=post_check.post_check_error,
                warning=post_check.warning,
            )

        rollback_success, rollback_error = self.rollback(backup_path, target_path)
        code = "post_check_failed"
        if plan.expected_version and post_check.new_version and post_check.new_version != plan.expected_version:
            code = "version_mismatch"
        return ComponentInstallResult(
            component_id=component_id,
            success=False,
            code=code,
            message="post-install version check failed; rollback attempted",
            target_path=str(target_path),
            staged_exe_path=str(plan.staged_exe_path),
            staging_dir=str(plan.staging_dir),
            backup_path=str(backup_path) if backup_path else None,
            old_version=old_version,
            new_version=post_check.new_version,
            post_check_error=post_check.post_check_error or post_check.message,
            warning=post_check.warning,
            rollback_attempted=True,
            rollback_success=rollback_success,
            rollback_error=rollback_error,
        )

    def build_install_plan(
        self,
        download_result: ComponentDownloadStageResult,
        entry: DependencyEntry,
        expected_version: str | None = None,
    ) -> ComponentInstallPlan | ComponentInstallResult:
        """Validate staged paths and target path before modifying anything."""
        component_id = entry.id
        if not download_result.success:
            return self._failure(component_id, "stage_failed", f"staging result is not successful: {download_result.code}")
        if download_result.component_id != component_id:
            return self._failure(component_id, "component_mismatch", "staging result component does not match manifest entry")
        if not download_result.staging_dir or not download_result.staged_exe_path:
            return self._failure(component_id, "missing_staged_path", "staging result has no staged executable path")

        try:
            staging_dir = Path(download_result.staging_dir).resolve()
            staged_exe = Path(download_result.staged_exe_path).resolve()
        except OSError as exc:
            return self._failure(component_id, "staged_path_invalid", f"failed to resolve staged path: {exc}")

        # R1.5 containment check: reject any staged product that does not resolve
        # under the caller-owned staging_dir. ``Path.relative_to`` raises
        # ``ValueError`` when the staged exe escapes the staging root; mapping
        # the resolve-then-relative_to check to an explicit error code makes
        # the rejection reason machine-parsable.
        try:
            staged_exe.relative_to(staging_dir)
        except ValueError:
            return self._failure(
                component_id,
                "staging_path_escape",
                "staged executable is outside staging directory",
            )
        if not staged_exe.exists() or not staged_exe.is_file() or staged_exe.stat().st_size <= 0:
            return self._failure(component_id, "staged_exe_missing", "staged executable is missing or empty")
        if staged_exe.suffix.lower() != ".exe":
            return self._failure(component_id, "staged_exe_invalid", "staged file is not an exe")

        try:
            expected_target = entry.path.resolve()
            target_path = Path(self.target_path_resolver(entry)).resolve()
        except OSError as exc:
            return self._failure(component_id, "target_path_invalid", f"failed to resolve target path: {exc}")

        if not self._target_matches_manifest(target_path, expected_target):
            return self._failure(component_id, "target_path_mismatch", "target path does not resolve to the expected component path")
        if target_path.suffix.lower() != ".exe":
            return self._failure(component_id, "target_not_exe", "target path is not an exe")

        return ComponentInstallPlan(
            component_id=component_id,
            target_path=target_path,
            staged_exe_path=staged_exe,
            staging_dir=staging_dir,
            backup_dir=self.backup_root.resolve(),
            expected_version=expected_version,
        )

    def ensure_process_free(self, target_path: Path, component_id: str) -> str | None:
        """Conservative process-use check hook; returns an error code when blocked."""
        if not target_path.exists():
            return None
        return "process_in_use" if self.is_process_using_file(target_path) else None

    def is_process_using_file(self, target_path: Path) -> bool:
        """Detect whether another process currently has ``target_path`` open.

        On Windows the preferred probe is ``CreateFileW`` with
        ``FILE_SHARE_NONE``. When a file is already held by another handle
        (for example the engine binary is currently being executed by the
        running M3U8D instance) the call fails with ``ERROR_SHARING_VIOLATION``
        / ``ERROR_LOCK_VIOLATION`` / ``ERROR_ACCESS_DENIED`` and we treat the
        target as in use. Any other failure (for example ``ERROR_FILE_NOT_FOUND``
        when the path disappeared mid-check) falls back to the legacy
        rename-rename probe so that transient or unexpected error codes still
        benefit from a best-effort detector.

        On non-Windows platforms the rename-rename probe remains the only
        available heuristic.
        """
        if os.name == "nt":
            windows_result = self._is_in_use_windows_createfile(target_path)
            if windows_result is not None:
                return windows_result
            # CreateFileW returned an unexpected error; fall through to the
            # legacy rename-rename probe as a best-effort secondary signal.
        return self._is_in_use_rename_probe(target_path)

    # ------------------------------------------------------------------
    # Internal helpers for is_process_using_file
    # ------------------------------------------------------------------

    @staticmethod
    def _is_in_use_windows_createfile(target_path: Path) -> bool | None:
        """Probe ``target_path`` using Win32 ``CreateFileW`` + ``FILE_SHARE_NONE``.

        Returns:
            ``True`` when the file is held by another process,
            ``False`` when an exclusive handle was opened successfully,
            ``None`` when the probe could not make a definite determination
            (caller should fall back to another heuristic).
        """
        try:
            import ctypes
            from ctypes import wintypes  # noqa: F401  (imported for side effects)
        except Exception:
            return None

        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except Exception:
            return None

        GENERIC_READ = 0x80000000
        FILE_SHARE_NONE = 0
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        ERROR_SHARING_VIOLATION = 32
        ERROR_LOCK_VIOLATION = 33
        ERROR_ACCESS_DENIED = 5

        CreateFileW = kernel32.CreateFileW
        CreateFileW.restype = ctypes.c_void_p
        CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        CloseHandle = kernel32.CloseHandle
        CloseHandle.restype = ctypes.c_int
        CloseHandle.argtypes = [ctypes.c_void_p]
        GetLastError = kernel32.GetLastError
        GetLastError.restype = ctypes.c_uint32

        handle = CreateFileW(
            str(target_path),
            GENERIC_READ,
            FILE_SHARE_NONE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            last_error = GetLastError()
            if last_error in (
                ERROR_SHARING_VIOLATION,
                ERROR_LOCK_VIOLATION,
                ERROR_ACCESS_DENIED,
            ):
                return True
            # Unknown error (missing file, IO error, etc.) — let the caller
            # fall back to the rename-rename probe for a best-effort signal.
            return None
        try:
            return False
        finally:
            CloseHandle(handle)

    @staticmethod
    def _is_in_use_rename_probe(target_path: Path) -> bool:
        """Legacy rename-rename probe retained for non-Windows and fallback."""
        try:
            probe_path = target_path.with_name(f".{target_path.name}.lock_probe_{os.getpid()}")
            os.rename(target_path, probe_path)
            os.rename(probe_path, target_path)
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return ComponentUpdateInstaller._classify_os_error(exc, default="") in (
                "permission_denied",
                "process_in_use",
            )

    def backup_current(self, target_path: Path, component_id: str, old_version: str | None = None) -> Path | None | ComponentInstallResult:
        """Copy an existing target executable to the backup directory."""
        if not target_path.exists():
            return None
        try:
            self.backup_root.mkdir(parents=True, exist_ok=True)
            backup_name = self._backup_name(component_id, old_version)
            backup_path = self._safe_child(self.backup_root, backup_name)
            shutil.copy2(target_path, backup_path)
            if not backup_path.exists() or backup_path.stat().st_size <= 0:
                return self._failure(component_id, "backup_failed", "backup file was not created or is empty")
            return backup_path
        except OSError as exc:
            return self._failure(component_id, self._classify_os_error(exc, default="backup_failed"), f"backup failed: {exc}")

    def replace_atomically(self, new_file: Path, target_path: Path) -> tuple[str, str] | None:
        """Atomically replace target_path by first copying to a temp file beside target_path.

        Windows rename/replace operations cannot cross volumes. Staged component
        files intentionally live under the user-data temp tree, while installed
        applications may reside on another drive (for example C: -> G:). The
        final atomic step therefore must replace from a file located in the same
        directory/volume as the target.
        """
        temp_path: Path | None = None
        try:
            temp_path = self._same_volume_temp_path(target_path)
            shutil.copy2(new_file, temp_path)
            verify_error = self._verify_copied_file(new_file, temp_path)
            if verify_error is not None:
                return verify_error
            self.replace_func(temp_path, target_path)
            temp_path = None
            return None
        except OSError as exc:
            code = self._classify_os_error(exc, default="replace_failed")
            hint = "may require administrator privileges or installing M3U8D to a writable directory"
            action = "same-volume temp copy" if temp_path is not None and temp_path.exists() else "atomic replace"
            return code, f"{action} failed: {exc}; {hint}"
        finally:
            if temp_path is not None:
                self._cleanup_temp_file(temp_path)

    def verify_after_replace(
        self,
        entry: DependencyEntry,
        expected_version: str | None = None,
        target_path: Path | None = None,
    ) -> ComponentInstallResult:
        """Run mandatory file checks and configurable post-replacement version checks."""
        resolved_target = target_path or entry.path
        basic_error = self._verify_target_file_present(resolved_target)
        if basic_error is not None:
            return self._failure(entry.id, "post_check_failed", basic_error, target_path=resolved_target, post_check_error=basic_error)

        version_required = self._is_post_install_version_required(entry)
        try:
            info = self.version_probe.probe(entry)
        except Exception as exc:
            error = f"version probe raised after replace: {exc}"
            if version_required:
                return self._failure(entry.id, "post_check_failed", error, target_path=resolved_target, post_check_error=str(exc))
            return self._success_with_warning(entry, resolved_target, error)
        if not info.exists:
            return self._failure(entry.id, "post_check_failed", "component is missing after replace", target_path=resolved_target, post_check_error=info.error)
        if info.error:
            if version_required:
                return self._failure(entry.id, "post_check_failed", info.error, target_path=resolved_target, new_version=info.version, post_check_error=info.error)
            return self._success_with_warning(entry, resolved_target, info.error, new_version=info.version)
        if expected_version and info.version and not self._versions_compatible(info.version, expected_version):
            if version_required:
                return self._failure(
                    entry.id,
                    "version_mismatch",
                    f"installed version {info.version!r} does not match expected {expected_version!r}",
                    target_path=resolved_target,
                    new_version=info.version,
                    post_check_error="installed version does not match expected version",
                )
            return self._success_with_warning(
                entry,
                resolved_target,
                f"installed version {info.version!r} does not match expected {expected_version!r}",
                new_version=info.version,
            )
        return ComponentInstallResult(
            component_id=entry.id,
            success=True,
            code="ok",
            message="post-install version check passed",
            target_path=str(resolved_target),
            new_version=info.version,
        )

    def rollback(self, backup_path: Path | None, target_path: Path) -> tuple[bool, str | None]:
        """Restore target_path from backup_path when available."""
        if backup_path is None:
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError as exc:
                    return False, f"failed to remove newly installed file: {exc}"
            return True, None
        replace_error = self.replace_atomically(backup_path, target_path)
        if replace_error is None:
            return True, None
        return False, f"rollback failed: {replace_error[1]}"

    def _check_disk_space(self, plan: ComponentInstallPlan) -> ComponentInstallResult | None:
        """R17.2 / R17.3: require ``free >= need * 1.2`` on the target volume.

        Returns a structured ``insufficient_disk`` failure when the target
        parent directory's volume does not have enough free bytes to safely
        stage, rename-to-``.bak``, and replace the target. Returns ``None`` on
        success so the caller can continue to the next install step.
        """
        try:
            need = int(plan.staged_exe_path.stat().st_size)
        except OSError as exc:
            return self._failure_from_plan(
                plan,
                "staged_exe_missing",
                f"failed to stat staged executable for disk pre-check: {exc}",
            )
        if need <= 0:
            return self._failure_from_plan(
                plan,
                "staged_exe_missing",
                "staged executable has zero length during disk pre-check",
            )
        probe_dir = plan.target_path.parent
        try:
            if not probe_dir.exists():
                probe_dir.parent.mkdir(parents=True, exist_ok=True)
                probe_dir = probe_dir if probe_dir.exists() else probe_dir.parent
            free = int(shutil.disk_usage(str(probe_dir)).free)
        except OSError as exc:
            return self._failure_from_plan(
                plan,
                self._classify_os_error(exc, default="insufficient_disk"),
                f"disk pre-check failed: {exc}",
                details={"need": need},
            )
        required = need + need // 5  # need * 1.2 in integer math
        if free < required:
            return self._failure_from_plan(
                plan,
                "insufficient_disk",
                f"target volume has {free} bytes free; need {required} bytes (staged={need}, margin=20%)",
                details={"need": need, "free": free, "required": required},
            )
        return None

    @staticmethod
    def _sibling_bak_path(target_path: Path) -> Path:
        """Return ``target_path + ".bak"`` sibling (lives on the same volume)."""
        return target_path.with_name(target_path.name + ".bak")

    @staticmethod
    def _remove_stale_sibling_bak(bak_path: Path) -> None:
        """Remove a leftover sibling ``.bak`` before a fresh install run."""
        try:
            if bak_path.exists():
                bak_path.unlink()
        except OSError:
            # Best effort: leave the stale .bak in place; the upcoming
            # os.replace will overwrite it atomically on Windows / POSIX.
            pass

    def cleanup_stale_backup_files(self, bin_dir: Path | None = None) -> list[Path]:
        """R17.4 startup hook: remove sibling ``*.bak`` files left next to
        installed executables by a previous install run.

        The short-lived ``.bak`` sibling produced by ``install_staged_update``
        is intentionally kept on disk after a successful replace so that an
        abrupt crash right after ``os.replace(staging, target)`` still leaves
        an operator-recoverable rollback point. By the time the application
        restarts, either the install fully succeeded (``.bak`` is stale) or
        it failed and was already rolled back (``.bak`` is also stale). In
        both cases the safe action is to remove any leftover ``*.bak`` files.

        Returns the list of removed paths (absolute) for observability.
        """
        if bin_dir is None:
            try:
                from core.app_paths import get_bin_dir  # local import avoids cycles in tests
                bin_dir = get_bin_dir()
            except Exception:
                return []
        return self.cleanup_stale_backup_files_static(bin_dir)

    @staticmethod
    def cleanup_stale_backup_files_static(bin_dir: Path) -> list[Path]:
        """Static variant of :meth:`cleanup_stale_backup_files`.

        Exposed so the startup path in :func:`core.app_paths.initialize_runtime_directories`
        can run the cleanup without constructing an installer (which would
        otherwise pull in the state store / version probe eagerly).
        """
        try:
            bin_dir = Path(bin_dir)
            if not bin_dir.exists() or not bin_dir.is_dir():
                return []
            entries = list(bin_dir.iterdir())
        except OSError:
            return []
        removed: list[Path] = []
        for entry in entries:
            try:
                # Only remove sibling "<name>.exe.bak" style artifacts that
                # sit directly under bin/. Subdirectories and non-.bak files
                # are left alone so unrelated assets are never collected.
                if not entry.is_file():
                    continue
                if not entry.name.endswith(".bak"):
                    continue
                entry.unlink()
                removed.append(entry)
            except OSError:
                # Best effort cleanup; leave files in place on error.
                continue
        return removed

    def _verify_staged_sha256(
        self,
        download_result: ComponentDownloadStageResult,
        plan: ComponentInstallPlan,
    ) -> ComponentInstallResult | None:
        """Recompute sha256 of the staged file and compare to the expected digest.

        The expected digest is sourced, in order, from:

        1. ``download_result.sha256`` captured during the current download pass.
        2. ``state.json``'s ``components[id].last_download.sha256`` entry when
           the in-memory download result carries no digest (defence-in-depth
           against a download layer that skipped recording the hash).

        When no expected digest is available (neither path yields a value)
        **and** the download result explicitly marks itself as
        ``weak_validation``, re-verification is skipped so that the Stage 1
        ``missing_checksum`` gate remains the single source of truth for
        weakly-verified assets (see downloader R1 invariants). If the download
        result claimed strong validation but carries no digest, the install
        refuses to proceed rather than silently bypassing the check.
        """
        expected = self._expected_staged_sha256(download_result, plan.component_id)
        if expected is None:
            if download_result.weak_validation:
                return None
            return self._failure_from_plan(
                plan,
                "staging_tampered",
                "no expected sha256 available to re-verify staged executable",
            )
        try:
            actual = self._sha256_file(plan.staged_exe_path)
        except OSError as exc:
            return self._failure_from_plan(
                plan,
                "staging_tampered",
                f"failed to hash staged executable for re-verification: {exc}",
            )
        if actual.lower() != expected.lower():
            return self._failure_from_plan(
                plan,
                "staging_tampered",
                "staged executable sha256 does not match expected digest",
            )
        return None

    def _expected_staged_sha256(
        self,
        download_result: ComponentDownloadStageResult,
        component_id: str,
    ) -> str | None:
        """Return the expected sha256 for the staged file or ``None`` if unknown."""
        candidate = (download_result.sha256 or "").strip()
        if candidate:
            return candidate
        try:
            state = self.state_store.get_component_state(component_id)
        except Exception:
            return None
        last_download = state.get("last_download") if isinstance(state, dict) else None
        if not isinstance(last_download, dict):
            return None
        fallback = last_download.get("sha256")
        if not isinstance(fallback, str) or not fallback.strip():
            return None
        return fallback.strip()

    def _verify_target_file_present(self, target_path: Path) -> str | None:
        try:
            if not target_path.exists():
                return "component is missing after replace"
            if not target_path.is_file():
                return "component target is not a file after replace"
            if target_path.stat().st_size <= 0:
                return "component target is empty after replace"
            return None
        except OSError as exc:
            return f"component target check failed after replace: {exc}"

    @staticmethod
    def _is_post_install_version_required(entry: DependencyEntry) -> bool:
        if entry.update is not None and entry.update.post_install_version_required is not None:
            return entry.update.post_install_version_required
        if entry.version is not None:
            return entry.version.post_install_required
        return True

    @staticmethod
    def _versions_compatible(installed: str, expected: str) -> bool:
        """Return True when ``installed`` may be considered a match for ``expected``.

        Post-install version check used to insist on strict string
        equality, which fails in two common real-world cases:

        * Upstream emits a "marketing" version (e.g. gyan.dev's
          ``release-version`` endpoint returns ``8.1.1``) while the binary
          itself reports a longer build tag (``8.1.1-essentials_build-www.gyan.dev``).
        * Rolling-release binaries whose ``-v`` output includes a build
          suffix (``1.37.0-release-1.37.0`` style) where the normalized
          semver prefix still matches the manifest's tag.

        The compatibility rules below are intentionally permissive on the
        "version looks right but carries extra metadata" side and strict
        on the "completely different major/minor" side:

        1. Byte-for-byte equality after trimming whitespace and a leading
           ``v``/``V`` prefix.
        2. Dotted prefix match — ``installed`` begins with
           ``expected + <separator>`` where the separator is one of
           ``" .-_+"``. This catches the FFmpeg case described above.
        3. Dotted prefix match the other way — ``expected`` begins with
           ``installed + <separator>`` (useful when the manifest carries
           a longer string than the binary reports).

        All three rules only fire when both strings have been stripped of
        whitespace and a ``v``/``V`` prefix so the rest of the installer
        still controls the canonical form.
        """

        a = (installed or "").strip().lstrip("vV")
        b = (expected or "").strip().lstrip("vV")
        if not a or not b:
            return False
        if a == b:
            return True
        separators = (" ", ".", "-", "_", "+")
        if a.startswith(b) and a[len(b):][:1] in separators:
            return True
        if b.startswith(a) and b[len(a):][:1] in separators:
            return True
        return False

    @staticmethod
    def _success_with_warning(entry: DependencyEntry, target_path: Path, warning: str, new_version: str | None = None) -> ComponentInstallResult:
        return ComponentInstallResult(
            component_id=entry.id,
            success=True,
            code="ok_with_warning",
            message="component installed successfully; version check warning",
            target_path=str(target_path),
            new_version=new_version,
            post_check_error=warning,
            warning=warning,
        )

    def _probe_version(self, entry: DependencyEntry) -> str | None:
        try:
            info = self.version_probe.probe(entry)
            return info.version
        except Exception:
            return None

    def _target_matches_manifest(self, target_path: Path, expected_target: Path) -> bool:
        return target_path == expected_target

    def _same_volume_temp_path(self, target_path: Path) -> Path:
        timestamp_ms = int(self.time_func() * 1000)
        suffix = f".update-tmp-{os.getpid()}-{timestamp_ms}{target_path.suffix}"
        candidate = target_path.with_suffix(suffix)
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = target_path.with_suffix(f".update-tmp-{os.getpid()}-{timestamp_ms}-{counter}{target_path.suffix}")
        return candidate

    def _verify_copied_file(self, source_path: Path, copied_path: Path) -> tuple[str, str] | None:
        try:
            source_stat = source_path.stat()
            copied_stat = copied_path.stat()
            if copied_stat.st_size <= 0 or copied_stat.st_size != source_stat.st_size:
                return "replace_failed", "same-volume temp copy verification failed: copied file size mismatch"
            source_hash = self._sha256_file(source_path)
            copied_hash = self._sha256_file(copied_path)
            if copied_hash != source_hash:
                return "replace_failed", "same-volume temp copy verification failed: copied file hash mismatch"
            return None
        except OSError as exc:
            code = self._classify_os_error(exc, default="replace_failed")
            return code, f"same-volume temp copy verification failed: {exc}"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_temp_file(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _backup_name(self, component_id: str, old_version: str | None) -> str:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.time_func()))
        version = self._safe_name(old_version) if old_version else "original"
        component = self._safe_name(component_id)
        return f"{component}_{timestamp}_{version}.exe"

    @staticmethod
    def _safe_name(value: str | None) -> str:
        text = str(value or "").strip()
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in text)
        return safe or "original"

    @staticmethod
    def _safe_child(root: Path, name: str) -> Path:
        root_resolved = root.resolve()
        candidate = root_resolved.joinpath(Path(name).name).resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise ValueError("refusing to write outside backup directory")
        return candidate

    @staticmethod
    def _classify_os_error(exc: OSError, default: str) -> str:
        winerror = getattr(exc, "winerror", None)
        if isinstance(exc, PermissionError) or winerror in (5, 32, 33) or exc.errno in (errno.EACCES, errno.EPERM):
            if winerror in (32, 33):
                return "process_in_use"
            return "permission_denied"
        if exc.errno in (errno.EBUSY, errno.ETXTBSY):
            return "process_in_use"
        return default

    @staticmethod
    def _failure(
        component_id: str,
        code: str,
        message: str,
        target_path: Path | str | None = None,
        staged_exe_path: Path | str | None = None,
        staging_dir: Path | str | None = None,
        backup_path: Path | str | None = None,
        old_version: str | None = None,
        new_version: str | None = None,
        post_check_error: str | None = None,
    ) -> ComponentInstallResult:
        return ComponentInstallResult(
            component_id=component_id,
            success=False,
            code=code,
            message=message,
            target_path=str(target_path) if target_path else None,
            staged_exe_path=str(staged_exe_path) if staged_exe_path else None,
            staging_dir=str(staging_dir) if staging_dir else None,
            backup_path=str(backup_path) if backup_path else None,
            old_version=old_version,
            new_version=new_version,
            post_check_error=post_check_error,
        )

    def _failure_from_plan(
        self,
        plan: ComponentInstallPlan,
        code: str,
        message: str,
        backup_path: Path | None = None,
        old_version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ComponentInstallResult:
        return ComponentInstallResult(
            component_id=plan.component_id,
            success=False,
            code=code,
            message=message,
            target_path=str(plan.target_path),
            staged_exe_path=str(plan.staged_exe_path),
            staging_dir=str(plan.staging_dir),
            backup_path=str(backup_path) if backup_path else None,
            old_version=old_version,
            details=details,
        )

    def _copy_failure_with_plan(
        self,
        failure: ComponentInstallResult,
        plan: ComponentInstallPlan,
        old_version: str | None = None,
    ) -> ComponentInstallResult:
        return ComponentInstallResult(
            component_id=plan.component_id,
            success=False,
            code=failure.code,
            message=failure.message,
            target_path=str(plan.target_path),
            staged_exe_path=str(plan.staged_exe_path),
            staging_dir=str(plan.staging_dir),
            backup_path=failure.backup_path,
            old_version=old_version,
            post_check_error=failure.post_check_error,
        )
