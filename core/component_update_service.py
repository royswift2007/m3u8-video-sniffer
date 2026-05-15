"""
Component update service: local/remote checks plus download, staging, install, and state persistence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.component_release_client import ComponentReleaseClient
from core.component_update_downloader import ComponentDownloadStageResult, ComponentUpdateDownloader
from core.component_update_installer import ComponentInstallResult, ComponentUpdateInstaller
from core.component_update_models import (
    ComponentBatchUpdateResult,
    ComponentUpdateProgressEvent,
    ComponentUpdateResult,
    ComponentUpdateStatus,
    ComponentVersionInfo,
    RemoteReleaseInfo,
)
from core.component_update_state import ComponentUpdateStateStore, utc_now_iso
from core.component_version_probe import ComponentVersionProbe
from core.dependency_manifest import DependencyEntry, DependencyManifest, load_dependency_manifest


class ComponentUpdateService:
    """Coordinate component update metadata, checks, staging, installation, and state."""

    def __init__(
        self,
        manifest: DependencyManifest | None = None,
        state_store: ComponentUpdateStateStore | None = None,
        progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None,
        release_client: ComponentReleaseClient | None = None,
        downloader: ComponentUpdateDownloader | None = None,
        installer: ComponentUpdateInstaller | None = None,
        version_probe: ComponentVersionProbe | None = None,
    ):
        self.manifest = manifest or load_dependency_manifest()
        self.state_store = state_store or ComponentUpdateStateStore()
        self.progress_callback = progress_callback
        self.version_probe = version_probe or ComponentVersionProbe()
        self.release_client = release_client or ComponentReleaseClient(self.state_store)
        self.downloader = downloader or ComponentUpdateDownloader()
        self.installer = installer or ComponentUpdateInstaller(version_probe=self.version_probe)

    def list_components(
        self,
        include_recommended: bool = True,
        include_optional: bool = False,
    ) -> list[DependencyEntry]:
        """Return update-manageable components from the manifest."""
        return self.manifest.get_update_enabled_entries(
            include_recommended=include_recommended,
            include_optional=include_optional,
        )

    def get_manifest_statuses(self, component_ids: list[str] | None = None) -> list[ComponentUpdateStatus]:
        """Return quick manifest-backed component rows without executing version commands."""
        wanted = set(component_ids or [])
        checked_at = utc_now_iso()
        statuses: list[ComponentUpdateStatus] = []
        for entry in self.list_components(include_recommended=True, include_optional=False):
            if wanted and entry.id not in wanted:
                continue
            local = ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=str(entry.path),
                exists=entry.path.exists(),
                error=None if entry.path.exists() else "component file is missing",
            )
            statuses.append(
                ComponentUpdateStatus(
                    component_id=entry.id,
                    label=entry.label,
                    category=entry.category,
                    local=local,
                    remote=None,
                    update_available=False,
                    status="manifest_listed",
                    message="component listed from manifest; local version check is pending",
                    last_checked_at=checked_at,
                )
            )
        return statuses

    def get_component(self, component_id: str) -> DependencyEntry | None:
        """Return one update-enabled component by id, or None when not present."""
        for entry in self.list_components(include_recommended=True, include_optional=True):
            if entry.id == component_id:
                return entry
        return None

    def get_local_status(self, component_ids: list[str] | None = None) -> list[ComponentUpdateStatus]:
        """Return local component statuses without any remote network access."""
        wanted = set(component_ids or [])
        statuses: list[ComponentUpdateStatus] = []
        checked_at = utc_now_iso()
        for entry in self.list_components(include_recommended=True, include_optional=False):
            if wanted and entry.id not in wanted:
                continue
            local = self.version_probe.probe(entry)
            if not local.exists:
                status = "missing"
                message = "component file is missing"
            elif local.error:
                status = "local_check_failed"
                message = local.error
            else:
                status = "local_checked"
                message = None
            statuses.append(
                ComponentUpdateStatus(
                    component_id=entry.id,
                    label=entry.label,
                    category=entry.category,
                    local=local,
                    remote=None,
                    update_available=False,
                    status=status,
                    message=message,
                    last_checked_at=checked_at,
                )
            )
            self.state_store.update_component_state(
                entry.id,
                {
                    "last_checked_at": checked_at,
                    "local_version": local.version,
                    "local_exists": local.exists,
                    "local_error": local.error,
                    "update_available": False,
                    "status": status,
                    "message": message,
                },
            )
        return statuses

    def refresh_local_status(self, component_ids: list[str] | None = None) -> list[ComponentUpdateStatus]:
        """Alias for local-only refresh used by later UI/worker layers."""
        return self.get_local_status(component_ids=component_ids)

    def check_updates(self, component_ids: list[str] | None = None, force: bool = False) -> list[ComponentUpdateStatus]:
        """Probe local versions, query remote release metadata, compare, and persist read-only status."""
        wanted = set(component_ids or [])
        statuses: list[ComponentUpdateStatus] = []
        for entry in self.list_components(include_recommended=True, include_optional=False):
            if wanted and entry.id not in wanted:
                continue
            statuses.append(self._check_one(entry, force=force))
        return statuses

    def check_startup_updates(self) -> list[ComponentUpdateStatus]:
        """Read-only startup check; later phases may add throttling config."""
        return self.check_updates(force=False)

    def update_component(
        self,
        component_id: str,
        *,
        force: bool = False,
        progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None,
    ) -> ComponentUpdateResult:
        """Check, download to staging, install, verify, persist state, and return a structured result."""
        entry = self.get_component(component_id)
        if entry is None:
            result = ComponentUpdateResult(
                component_id=component_id,
                success=False,
                status="failed",
                code="component_not_found",
                error="component is not update-enabled or does not exist in manifest",
                message="component is not update-enabled or does not exist in manifest",
            )
            self.state_store.update_component_state(component_id, self._state_patch_for_result(result))
            return result

        emit = lambda event, detail=None, **kw: self._emit_progress_event(entry, event, detail, progress_callback, **kw)
        emit("checking", "checking local and remote component versions")
        status = self._check_one(entry, force=force, status_override="checking")
        local = status.local
        remote = status.remote

        if remote is None:
            return self._finish_failure(
                entry,
                "remote_missing",
                "remote release result is missing",
                local=local,
                remote=remote,
                progress_callback=progress_callback,
            )
        if remote.error:
            return self._finish_failure(
                entry,
                "remote_check_failed",
                remote.error,
                local=local,
                remote=remote,
                progress_callback=progress_callback,
            )
        if not remote.asset_url:
            return self._finish_failure(
                entry,
                "asset_missing",
                "remote release has no downloadable asset",
                local=local,
                remote=remote,
                progress_callback=progress_callback,
            )

        comparison = self.compare_versions(local.version, remote.latest_version)
        should_update = force or status.update_available or comparison == -1 or not local.exists
        if not should_update:
            result = ComponentUpdateResult(
                component_id=entry.id,
                label=entry.label,
                success=True,
                skipped=True,
                status="latest",
                code="latest",
                message="component is already latest",
                old_version=local.version,
                new_version=local.version,
                local_version=local.version,
                remote_version=remote.latest_version,
                asset_name=remote.asset_name,
                asset_url=remote.asset_url,
            )
            self.state_store.update_component_state(entry.id, self._state_patch_for_result(result, local=local, remote=remote, status="latest"))
            self.state_store.record_update_result(result)
            emit("latest", "component is already latest", percent=100)
            return result

        emit("downloading", "downloading update asset", percent=0)
        self._patch_status(entry.id, "downloading", local=local, remote=remote, last_error=None)

        # Throttle per-chunk progress emissions so the Qt log panel
        # doesn't get flooded when a chunk is ~ 256 KiB and the asset is
        # 100 MB+. Emit at most every 2% step (or every 1 MB for
        # unknown-total streams).
        last_reported_percent: dict[str, int] = {"value": -1}
        last_reported_bytes: dict[str, int] = {"value": -1}

        def _on_chunk(bytes_downloaded: int, total_bytes: int | None) -> None:
            if total_bytes and total_bytes > 0:
                percent = min(99, (bytes_downloaded * 100) // total_bytes)
                if percent < 0:
                    percent = 0
                if percent - last_reported_percent["value"] < 2:
                    return
                last_reported_percent["value"] = percent
                emit(
                    "downloading",
                    "downloading update asset",
                    percent=percent,
                    bytes_downloaded=bytes_downloaded,
                    total_bytes=total_bytes,
                )
            else:
                # Chunked transfer / unknown size: report every 1 MiB.
                step = 1024 * 1024
                if bytes_downloaded - last_reported_bytes["value"] < step:
                    return
                last_reported_bytes["value"] = bytes_downloaded
                emit(
                    "downloading",
                    "downloading update asset",
                    bytes_downloaded=bytes_downloaded,
                )

        try:
            download_result = self.downloader.download_and_stage(
                entry, remote, progress_callback=_on_chunk
            )
        except Exception as exc:
            download_result = ComponentDownloadStageResult(
                component_id=entry.id,
                success=False,
                code="download_exception",
                message=f"download raised exception: {exc}",
            )
        if not download_result.success:
            return self._finish_failure(
                entry,
                download_result.code,
                download_result.message,
                local=local,
                remote=remote,
                download=download_result,
                status="failed",
                progress_callback=progress_callback,
            )

        emit("staged", "asset downloaded and staged", bytes_downloaded=download_result.bytes_downloaded)
        self._patch_status(entry.id, "staged", local=local, remote=remote, download=download_result, last_error=None)

        emit("installing", "installing staged component")
        self._patch_status(entry.id, "installing", local=local, remote=remote, download=download_result, last_error=None)
        try:
            install_result = self.installer.install_staged_update(download_result, entry, expected_version=remote.latest_version)
        except Exception as exc:
            install_result = ComponentInstallResult(
                component_id=entry.id,
                success=False,
                code="install_exception",
                message=f"install raised exception: {exc}",
            )
        if not install_result.success:
            return self._finish_failure(
                entry,
                install_result.code,
                install_result.message,
                local=local,
                remote=remote,
                download=download_result,
                install=install_result,
                status="failed",
                progress_callback=progress_callback,
            )

        result = ComponentUpdateResult(
            component_id=entry.id,
            label=entry.label,
            success=True,
            status="updated",
            code=install_result.code if install_result.warning else "ok",
            message=install_result.message if install_result.warning else "component updated successfully",
            warning=install_result.warning,
            old_version=install_result.old_version or local.version,
            new_version=install_result.new_version or remote.latest_version,
            backup_path=install_result.backup_path,
            local_version=local.version,
            remote_version=remote.latest_version,
            asset_name=remote.asset_name,
            asset_url=remote.asset_url,
            download_path=download_result.download_path,
            staging_dir=download_result.staging_dir,
            staged_exe_path=download_result.staged_exe_path,
            bytes_downloaded=download_result.bytes_downloaded,
            sha256=download_result.sha256,
            weak_validation=download_result.weak_validation,
            install_code=install_result.code,
            install_message=install_result.message,
            details={"remote": remote.to_dict(), "download": download_result.to_dict(), "install": install_result.to_dict()},
        )
        self.state_store.update_component_state(entry.id, self._state_patch_for_result(result, local=local, remote=remote, download=download_result, install=install_result, status="updated"))
        self.state_store.record_update_result(result)
        emit("updated", result.message or "component updated successfully", percent=100)
        return result

    def update_components(
        self,
        component_ids: list[str] | None = None,
        *,
        force: bool = False,
        progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None,
    ) -> ComponentBatchUpdateResult:
        """Update components sequentially and return success/failure/skipped summary."""
        entries = self.list_components(include_recommended=True, include_optional=False)
        if component_ids is not None:
            wanted = set(component_ids)
            ordered_ids = [entry.id for entry in entries if entry.id in wanted]
            ordered_ids.extend(component_id for component_id in component_ids if component_id not in ordered_ids)
        else:
            ordered_ids = [entry.id for entry in entries]
        results = [self.update_component(component_id, force=force, progress_callback=progress_callback) for component_id in ordered_ids]
        return ComponentBatchUpdateResult(results=results)

    def compare_versions(
        self,
        local_version: str | None,
        remote_version: str | None,
        strategy: str = "semantic_or_date",
    ) -> int | None:
        """Compare versions conservatively.

        Returns -1 when local is older, 0 when equal, 1 when local is newer,
        and None when versions cannot be compared safely.
        """
        local = self.normalize_version(local_version)
        remote = self.normalize_version(remote_version)
        if not local or not remote:
            return None
        if local == remote:
            return 0

        local_date = self._parse_date_version(local)
        remote_date = self._parse_date_version(remote)
        if local_date is not None and remote_date is not None:
            return self._cmp_tuple(local_date, remote_date)

        local_semver = self._parse_semantic_version(local)
        remote_semver = self._parse_semantic_version(remote)
        if local_semver is not None and remote_semver is not None:
            return self._cmp_tuple(local_semver, remote_semver)

        return None

    @staticmethod
    def normalize_version(version: str | None) -> str | None:
        """Normalize common version forms by trimming spaces and stripping v/V prefix."""
        if version is None:
            return None
        normalized = str(version).strip().lstrip("vV")
        return normalized or None

    def should_check_on_startup(self) -> bool:
        """Startup checks are read-only and safe."""
        return True

    def _check_one(self, entry: DependencyEntry, force: bool = False, status_override: str | None = None) -> ComponentUpdateStatus:
        checked_at = utc_now_iso()
        local = self.version_probe.probe(entry)
        remote = self.release_client.fetch_latest(entry, force=force)
        comparison = self.compare_versions(local.version, remote.latest_version)
        comparable = comparison in (-1, 0, 1) and not remote.error and local.version and remote.latest_version
        update_available = comparison == -1 and bool(remote.asset_url) and not remote.error
        if not local.exists:
            status = "missing"
            message = "component file is missing"
        elif local.error:
            status = "local_check_failed"
            message = local.error
        elif remote.error:
            status = "remote_check_failed"
            message = remote.error
        elif not remote.asset_url:
            status = "asset_missing"
            message = "remote release has no downloadable asset"
        elif comparison is None:
            status = "version_unknown"
            message = "version comparison is unknown"
        elif update_available:
            status = "update_available"
            message = "remote version is newer"
        elif comparable:
            status = "latest"
            message = None
        else:
            status = "version_unknown"
            message = "version comparison is conservative or incomplete"

        status_obj = ComponentUpdateStatus(
            component_id=entry.id,
            label=entry.label,
            category=entry.category,
            local=local,
            remote=remote,
            update_available=update_available,
            status=status,
            message=message,
            last_checked_at=checked_at,
        )
        self.state_store.update_component_state(
            entry.id,
            self._status_patch(status_override or status, local=local, remote=remote, update_available=update_available, message=message, checked_at=checked_at),
        )
        return status_obj

    def _finish_failure(
        self,
        entry: DependencyEntry,
        code: str,
        message: str,
        *,
        local: ComponentVersionInfo | None = None,
        remote: RemoteReleaseInfo | None = None,
        download: ComponentDownloadStageResult | None = None,
        install: ComponentInstallResult | None = None,
        status: str = "failed",
        progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None,
    ) -> ComponentUpdateResult:
        result = ComponentUpdateResult(
            component_id=entry.id,
            label=entry.label,
            success=False,
            status=status,
            code=code,
            message=message,
            error=message,
            old_version=(install.old_version if install else None) or (local.version if local else None),
            new_version=install.new_version if install else None,
            backup_path=install.backup_path if install else None,
            local_version=local.version if local else None,
            remote_version=remote.latest_version if remote else None,
            asset_name=(download.asset_name if download else None) or (remote.asset_name if remote else None),
            asset_url=remote.asset_url if remote else None,
            download_path=download.download_path if download else None,
            staging_dir=(download.staging_dir if download else None) or (install.staging_dir if install else None),
            staged_exe_path=(download.staged_exe_path if download else None) or (install.staged_exe_path if install else None),
            bytes_downloaded=download.bytes_downloaded if download else None,
            sha256=download.sha256 if download else None,
            weak_validation=download.weak_validation if download else False,
            install_code=install.code if install else None,
            install_message=install.message if install else None,
            rollback_attempted=install.rollback_attempted if install else False,
            rollback_success=install.rollback_success if install else None,
            rollback_error=install.rollback_error if install else None,
            details=self._details(remote=remote, download=download, install=install),
        )
        self.state_store.update_component_state(entry.id, self._state_patch_for_result(result, local=local, remote=remote, download=download, install=install, status=status))
        self.state_store.record_update_result(result)
        self._emit_progress_event(entry, "failed", message, progress_callback)
        return result

    def _state_patch_for_result(
        self,
        result: ComponentUpdateResult,
        *,
        local: ComponentVersionInfo | None = None,
        remote: RemoteReleaseInfo | None = None,
        download: ComponentDownloadStageResult | None = None,
        install: ComponentInstallResult | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "status": status or result.status,
            "last_error": result.error,
            "last_warning": result.warning,
            "message": result.message,
            "update_available": False if result.success else None,
            "last_checked_at": utc_now_iso(),
            "last_update_at": utc_now_iso() if result.success and not result.skipped else None,
            "latest_version": result.remote_version,
            "local_version": result.new_version or result.local_version,
            "remote_version": result.remote_version,
            "asset_name": result.asset_name,
            "asset_url": result.asset_url,
        }
        if local is not None:
            patch.update({"local_exists": local.exists, "local_error": local.error, "local_version": result.new_version or local.version})
        if remote is not None:
            patch.update(
                {
                    "latest_version": remote.latest_version,
                    "remote_version": remote.latest_version,
                    "remote_release_url": remote.release_url,
                    "remote_published_at": remote.published_at,
                    "remote_asset_name": remote.asset_name,
                    "remote_asset_url": remote.asset_url,
                    "remote_error": remote.error,
                }
            )
        if download is not None:
            patch["last_download"] = download.to_dict()
        if install is not None:
            patch["last_install"] = install.to_dict()
        return {key: value for key, value in patch.items() if value is not None}

    def _status_patch(
        self,
        status: str,
        *,
        local: ComponentVersionInfo,
        remote: RemoteReleaseInfo,
        update_available: bool,
        message: str | None,
        checked_at: str,
    ) -> dict[str, Any]:
        return {
            "last_checked_at": checked_at,
            "local_version": local.version,
            "local_exists": local.exists,
            "local_error": local.error,
            "latest_version": remote.latest_version,
            "remote_version": remote.latest_version,
            "remote_release_url": remote.release_url,
            "remote_published_at": remote.published_at,
            "remote_asset_name": remote.asset_name,
            "remote_asset_url": remote.asset_url,
            "remote_error": remote.error,
            "version_comparison": self.compare_versions(local.version, remote.latest_version) if self.compare_versions(local.version, remote.latest_version) is not None else "unknown",
            "update_available": update_available,
            "status": status,
            "message": message,
            "last_error": message if status.endswith("failed") or status in ("remote_check_failed", "local_check_failed", "asset_missing") else None,
        }

    def _patch_status(
        self,
        component_id: str,
        status: str,
        *,
        local: ComponentVersionInfo | None = None,
        remote: RemoteReleaseInfo | None = None,
        download: ComponentDownloadStageResult | None = None,
        last_error: str | None = None,
    ) -> None:
        patch: dict[str, Any] = {"status": status, "last_error": last_error, "last_checked_at": utc_now_iso()}
        if local is not None:
            patch.update({"local_version": local.version, "local_exists": local.exists, "local_error": local.error})
        if remote is not None:
            patch.update({"latest_version": remote.latest_version, "remote_version": remote.latest_version, "remote_asset_name": remote.asset_name, "remote_asset_url": remote.asset_url, "remote_error": remote.error})
        if download is not None:
            patch["last_download"] = download.to_dict()
        self.state_store.update_component_state(component_id, patch)

    def _details(
        self,
        *,
        remote: RemoteReleaseInfo | None = None,
        download: ComponentDownloadStageResult | None = None,
        install: ComponentInstallResult | None = None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if remote is not None:
            details["remote"] = remote.to_dict()
        if download is not None:
            details["download"] = download.to_dict()
        if install is not None:
            details["install"] = install.to_dict()
        return details

    def _emit_progress_event(
        self,
        entry: DependencyEntry,
        event: str,
        detail: str | None = None,
        progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None,
        **kwargs: Any,
    ) -> None:
        progress = ComponentUpdateProgressEvent(event=event, component_id=entry.id, label=entry.label, detail=detail, **kwargs)
        self._emit_progress(progress, progress_callback=progress_callback)

    def _emit_progress(
        self, event: ComponentUpdateProgressEvent, progress_callback: Callable[[ComponentUpdateProgressEvent], None] | None = None) -> None:
        """Forward progress events when a callback is configured."""
        callback = progress_callback or self.progress_callback
        if callback:
            callback(event)

    @staticmethod
    def _parse_date_version(version: str) -> tuple[int, int, int] | None:
        match = re.fullmatch(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})", version)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return year, month, day

    @staticmethod
    def _parse_semantic_version(version: str) -> tuple[int, ...] | None:
        base = re.split(r"[-+]", version, maxsplit=1)[0]
        if not re.fullmatch(r"\d+(?:\.\d+){1,3}", base):
            return None
        parts = tuple(int(part) for part in base.split("."))
        while len(parts) < 3:
            parts = parts + (0,)
        return parts

    @staticmethod
    def _cmp_tuple(left: tuple[int, ...], right: tuple[int, ...]) -> int:
        max_len = max(len(left), len(right))
        padded_left = left + (0,) * (max_len - len(left))
        padded_right = right + (0,) * (max_len - len(right))
        if padded_left == padded_right:
            return 0
        return -1 if padded_left < padded_right else 1
