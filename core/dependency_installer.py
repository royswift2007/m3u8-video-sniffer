"""
Dependency download and installation helpers.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.app_paths import (
    get_bin_dir,
    get_dependency_manifest_path,
    get_temp_dir,
    resolve_app_path,
)
from core.dependency_manifest import CATEGORY_LABELS, DependencyEntry, MANIFEST_CATEGORIES
from utils.logger import logger

DEFAULT_DOWNLOAD_TIMEOUT = 300
DOWNLOAD_USER_AGENT = "M3U8D Dependency Installer/1.0"


@dataclass(frozen=True)
class DependencyInstallSpec:
    """Installable dependency definition loaded from deps.json."""

    id: str
    label: str
    relative_path: str
    category: str
    download: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        """Return absolute target path for this dependency."""
        return resolve_app_path(self.relative_path)


@dataclass(frozen=True)
class DependencyInstallItemResult:
    """Installation result for a single dependency."""

    entry_id: str
    label: str
    target_path: str
    success: bool
    skipped: bool = False
    error: str | None = None


@dataclass(frozen=True)
class DependencyProgressEvent:
    """Progress event emitted during dependency installation."""

    event: str
    category: str
    category_label: str
    current_index: int
    total_count: int
    entry_id: str | None = None
    label: str | None = None
    target_path: str | None = None
    detail: str | None = None
    bytes_downloaded: int | None = None
    total_bytes: int | None = None


DependencyProgressCallback = Callable[[DependencyProgressEvent], None]


@dataclass
class DependencyInstallBatchResult:
    """Batch installation summary."""

    category: str
    results: list[DependencyInstallItemResult] = field(default_factory=list)

    @property
    def requested_count(self) -> int:
        return len(self.results)

    @property
    def succeeded_results(self) -> list[DependencyInstallItemResult]:
        return [result for result in self.results if result.success and not result.skipped]

    @property
    def skipped_results(self) -> list[DependencyInstallItemResult]:
        return [result for result in self.results if result.skipped]

    @property
    def failed_results(self) -> list[DependencyInstallItemResult]:
        return [result for result in self.results if not result.success]

    @property
    def success_count(self) -> int:
        return len(self.succeeded_results)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_results)

    @property
    def failed_count(self) -> int:
        return len(self.failed_results)

    @property
    def ok(self) -> bool:
        return self.failed_count == 0

    def get_error_messages(self) -> list[str]:
        """Return user-facing error lines for failed items."""
        messages: list[str] = []
        for result in self.failed_results:
            if result.error:
                messages.append(f"{result.label} ({result.target_path}): {result.error}")
            else:
                messages.append(f"{result.label} ({result.target_path}): 安装失败")
        return messages


@dataclass
class DependencyInstallRunResult:
    """Aggregated installation summary for multiple categories."""

    categories: tuple[str, ...]
    batch_results: list[DependencyInstallBatchResult] = field(default_factory=list)

    @property
    def requested_count(self) -> int:
        return sum(result.requested_count for result in self.batch_results)

    @property
    def success_count(self) -> int:
        return sum(result.success_count for result in self.batch_results)

    @property
    def skipped_count(self) -> int:
        return sum(result.skipped_count for result in self.batch_results)

    @property
    def failed_count(self) -> int:
        return sum(result.failed_count for result in self.batch_results)

    @property
    def ok(self) -> bool:
        return self.failed_count == 0

    def get_error_messages(self) -> list[str]:
        """Return user-facing error lines for failed items."""
        messages: list[str] = []
        for batch_result in self.batch_results:
            messages.extend(batch_result.get_error_messages())
        return messages


class DependencyInstaller:
    """Install dependencies described in deps.json."""

    def __init__(
        self,
        manifest_path: Path | None = None,
        progress_callback: DependencyProgressCallback | None = None,
    ):
        self.manifest_path = manifest_path or get_dependency_manifest_path()
        self.progress_callback = progress_callback
        self._specs_by_id = self._load_specs_by_id()

    def install_category(
        self,
        category: str,
        entries: Sequence[DependencyEntry] | None = None,
    ) -> DependencyInstallBatchResult:
        """Install dependencies from one manifest category."""
        normalized_category = self._normalize_category(category)
        specs = self._resolve_specs(normalized_category, entries)
        return self._install_specs(normalized_category, specs)

    def install_required(
        self,
        entries: Sequence[DependencyEntry] | None = None,
    ) -> DependencyInstallBatchResult:
        """Install required dependencies, optionally limited to specific entries."""
        return self.install_category("required", entries=entries)

    def install_recommended(
        self,
        entries: Sequence[DependencyEntry] | None = None,
    ) -> DependencyInstallBatchResult:
        """Install recommended dependencies, optionally limited to specific entries."""
        return self.install_category("recommended", entries=entries)

    def install_categories(
        self,
        categories: Sequence[str],
    ) -> DependencyInstallRunResult:
        """Install dependencies from multiple manifest categories."""
        normalized_categories = self._normalize_categories(categories)
        batch_results: list[DependencyInstallBatchResult] = []
        for category in normalized_categories:
            batch_results.append(self.install_category(category))
        return DependencyInstallRunResult(
            categories=tuple(normalized_categories),
            batch_results=batch_results,
        )

    @staticmethod
    def _normalize_category(category: str) -> str:
        normalized_category = str(category).strip().lower()
        if normalized_category not in MANIFEST_CATEGORIES:
            raise ValueError(f"不支持的依赖分类: {category}")
        return normalized_category

    def _normalize_categories(self, categories: Sequence[str]) -> list[str]:
        normalized_categories: list[str] = []
        seen: set[str] = set()
        for category in categories:
            normalized_category = self._normalize_category(category)
            if normalized_category in seen:
                continue
            seen.add(normalized_category)
            normalized_categories.append(normalized_category)

        if not normalized_categories:
            raise ValueError("至少需要一个依赖分类")

        return normalized_categories

    def _load_specs_by_id(self) -> dict[str, DependencyInstallSpec]:
        with open(self.manifest_path, "r", encoding="utf-8") as manifest_file:
            payload = json.load(manifest_file)

        if not isinstance(payload, dict):
            raise ValueError("依赖清单顶层必须是对象")

        specs_by_id: dict[str, DependencyInstallSpec] = {}
        for category in MANIFEST_CATEGORIES:
            raw_entries = payload.get(category, [])
            if raw_entries is None:
                continue
            if not isinstance(raw_entries, list):
                raise ValueError(f"依赖分类必须是数组: {category}")

            for index, raw_entry in enumerate(raw_entries):
                if not isinstance(raw_entry, dict):
                    raise ValueError(f"依赖项必须是对象: {category}[{index}]")

                dependency_id = str(raw_entry.get("id", "")).strip()
                relative_path = str(raw_entry.get("path", "")).strip()
                label = str(raw_entry.get("label") or dependency_id).strip()
                if not dependency_id or not relative_path:
                    raise ValueError(f"依赖项缺少 id 或 path: {category}[{index}]")

                download = raw_entry.get("download")
                if download is not None and not isinstance(download, dict):
                    raise ValueError(f"download 配置必须是对象: {category}[{index}]")

                specs_by_id[dependency_id] = DependencyInstallSpec(
                    id=dependency_id,
                    label=label,
                    relative_path=relative_path,
                    category=category,
                    download=download,
                )
        return specs_by_id

    def _resolve_specs(
        self,
        category: str,
        entries: Sequence[DependencyEntry] | None,
    ) -> list[DependencyInstallSpec]:
        if entries is None:
            return [
                spec
                for spec in self._specs_by_id.values()
                if spec.category == category
            ]

        specs: list[DependencyInstallSpec] = []
        for entry in entries:
            spec = self._specs_by_id.get(entry.id)
            if spec is None:
                specs.append(
                    DependencyInstallSpec(
                        id=entry.id,
                        label=entry.label,
                        relative_path=entry.relative_path,
                        category=entry.category,
                        download=None,
                    )
                )
                continue
            specs.append(spec)
        return specs

    def _install_specs(
        self,
        category: str,
        specs: Sequence[DependencyInstallSpec],
    ) -> DependencyInstallBatchResult:
        result = DependencyInstallBatchResult(category=category)
        get_bin_dir().mkdir(parents=True, exist_ok=True)
        self._get_download_temp_dir().mkdir(parents=True, exist_ok=True)

        total_count = len(specs)
        for index, spec in enumerate(specs, start=1):
            result.results.append(self._install_one(spec, category, index, total_count))
        return result

    def _install_one(
        self,
        spec: DependencyInstallSpec,
        category: str,
        current_index: int,
        total_count: int,
    ) -> DependencyInstallItemResult:
        target_path = spec.path
        target_text = str(target_path)

        if target_path.exists():
            logger.info(
                "依赖已存在，跳过下载",
                dependency=spec.id,
                target=target_text,
            )
            self._emit_progress(
                event="item_skipped",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                detail="目标文件已存在",
            )
            return DependencyInstallItemResult(
                entry_id=spec.id,
                label=spec.label,
                target_path=target_text,
                success=True,
                skipped=True,
            )

        if not spec.download:
            message = "deps.json 未提供 download 配置"
            logger.error(
                "依赖安装失败",
                dependency=spec.id,
                target=target_text,
                error=message,
            )
            self._emit_progress(
                event="item_failed",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                detail=message,
            )
            return DependencyInstallItemResult(
                entry_id=spec.id,
                label=spec.label,
                target_path=target_text,
                success=False,
                error=message,
            )

        try:
            download_type = str(spec.download.get("type") or "").strip().lower()
            download_url = self._resolve_download_url(spec.download)

            logger.info(
                "开始安装依赖",
                dependency=spec.id,
                download_type=download_type,
                target=target_text,
                url=download_url,
            )
            self._emit_progress(
                event="item_started",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                detail=f"type={download_type} url={download_url}",
            )

            if download_type == "file":
                self._download_to_target(spec, download_url, category, current_index, total_count)
            elif download_type == "zip":
                member_name = str(
                    spec.download.get("member") or Path(spec.relative_path).name
                ).strip()
                if not member_name:
                    raise ValueError("zip 下载缺少 member 配置")
                self._download_zip_member_to_target(
                    spec,
                    download_url,
                    member_name,
                    category,
                    current_index,
                    total_count,
                )
            else:
                raise ValueError(f"不支持的 download.type: {download_type or '<empty>'}")
        except Exception as exc:
            logger.error(
                "依赖安装失败",
                dependency=spec.id,
                target=target_text,
                error=exc,
            )
            self._emit_progress(
                event="item_failed",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                detail=str(exc),
            )
            return DependencyInstallItemResult(
                entry_id=spec.id,
                label=spec.label,
                target_path=target_text,
                success=False,
                error=str(exc),
            )

        if not target_path.exists():
            message = "下载完成后未找到目标文件"
            logger.error(
                "依赖安装失败",
                dependency=spec.id,
                target=target_text,
                error=message,
            )
            self._emit_progress(
                event="item_failed",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                detail=message,
            )
            return DependencyInstallItemResult(
                entry_id=spec.id,
                label=spec.label,
                target_path=target_text,
                success=False,
                error=message,
            )

        logger.info(
            "依赖安装完成",
            dependency=spec.id,
            target=target_text,
        )
        self._emit_progress(
            event="item_completed",
            category=category,
            current_index=current_index,
            total_count=total_count,
            spec=spec,
            detail="下载并写入完成",
        )
        return DependencyInstallItemResult(
            entry_id=spec.id,
            label=spec.label,
            target_path=target_text,
            success=True,
        )

    def _resolve_download_url(self, download: dict[str, Any]) -> str:
        source = str(download.get("source") or "direct").strip().lower()
        if source == "direct":
            url = str(download.get("url") or "").strip()
            if not url:
                raise ValueError("download.url 不能为空")
            return url

        if source == "github_release":
            repo = str(download.get("repo") or "").strip()
            asset_pattern = str(download.get("asset_pattern") or "").strip()
            if not repo or not asset_pattern:
                raise ValueError("github_release 下载缺少 repo 或 asset_pattern")
            return self._resolve_latest_github_asset_url(repo, asset_pattern)

        raise ValueError(f"不支持的 download.source: {source or '<empty>'}")

    def _resolve_latest_github_asset_url(self, repo: str, asset_pattern: str) -> str:
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        request = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": DOWNLOAD_USER_AGENT,
            },
        )

        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)

        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise ValueError(f"GitHub release 响应缺少 assets: {repo}")

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            asset_name = str(asset.get("name") or "").strip()
            if not fnmatch.fnmatch(asset_name, asset_pattern):
                continue
            asset_url = str(asset.get("browser_download_url") or "").strip()
            if asset_url:
                return asset_url

        raise FileNotFoundError(
            f"未找到匹配的 GitHub 发布资源: {repo} / {asset_pattern}"
        )

    def _emit_progress(
        self,
        event: str,
        category: str,
        current_index: int,
        total_count: int,
        spec: DependencyInstallSpec | None = None,
        detail: str | None = None,
        bytes_downloaded: int | None = None,
        total_bytes: int | None = None,
    ) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback(
            DependencyProgressEvent(
                event=event,
                category=category,
                category_label=CATEGORY_LABELS.get(category, category),
                current_index=current_index,
                total_count=total_count,
                entry_id=spec.id if spec else None,
                label=spec.label if spec else None,
                target_path=str(spec.path) if spec else None,
                detail=detail,
                bytes_downloaded=bytes_downloaded,
                total_bytes=total_bytes,
            )
        )

    def _download_to_target(
        self,
        spec: DependencyInstallSpec,
        url: str,
        category: str,
        current_index: int,
        total_count: int,
    ) -> None:
        temp_path = self._create_temp_file(
            spec.id,
            self._guess_download_suffix(url, fallback=spec.path.suffix or ".bin"),
        )
        try:
            self._download_file(
                url,
                temp_path,
                timeout=self._get_timeout(spec.download),
                spec=spec,
                category=category,
                current_index=current_index,
                total_count=total_count,
            )
            spec.path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, spec.path)
        finally:
            self._cleanup_file(temp_path)

    def _download_zip_member_to_target(
        self,
        spec: DependencyInstallSpec,
        url: str,
        member_name: str,
        category: str,
        current_index: int,
        total_count: int,
    ) -> None:
        archive_path = self._create_temp_file(spec.id, ".zip")
        extracted_path = self._create_temp_file(
            spec.id,
            spec.path.suffix or ".bin",
        )
        try:
            self._download_file(
                url,
                archive_path,
                timeout=self._get_timeout(spec.download),
                spec=spec,
                category=category,
                current_index=current_index,
                total_count=total_count,
            )
            self._extract_zip_member(archive_path, member_name, extracted_path)
            spec.path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(extracted_path, spec.path)
        finally:
            self._cleanup_file(archive_path)
            self._cleanup_file(extracted_path)

    def _download_file(
        self,
        url: str,
        destination: Path,
        timeout: int,
        spec: DependencyInstallSpec,
        category: str,
        current_index: int,
        total_count: int,
    ) -> None:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "*/*",
                "User-Agent": DOWNLOAD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total_bytes = self._parse_content_length(response.headers.get("Content-Length"))
            bytes_downloaded = 0
            next_report_threshold = 0
            chunk_size = 1024 * 256
            with open(destination, "wb") as output_file:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    output_file.write(chunk)
                    bytes_downloaded += len(chunk)
                    should_report = total_bytes is None or bytes_downloaded >= next_report_threshold
                    if should_report:
                        self._emit_progress(
                            event="item_progress",
                            category=category,
                            current_index=current_index,
                            total_count=total_count,
                            spec=spec,
                            bytes_downloaded=bytes_downloaded,
                            total_bytes=total_bytes,
                        )
                        if total_bytes is not None:
                            next_report_threshold = min(
                                total_bytes,
                                bytes_downloaded + max(total_bytes // 20, chunk_size),
                            )
                        else:
                            next_report_threshold = bytes_downloaded + (1024 * 1024)

            self._emit_progress(
                event="item_progress",
                category=category,
                current_index=current_index,
                total_count=total_count,
                spec=spec,
                bytes_downloaded=bytes_downloaded,
                total_bytes=total_bytes,
            )

    def _extract_zip_member(
        self,
        archive_path: Path,
        member_name: str,
        destination: Path,
    ) -> None:
        with zipfile.ZipFile(archive_path, "r") as archive_file:
            matched_member = self._find_zip_member(archive_file, member_name)
            if matched_member is None:
                raise FileNotFoundError(f"压缩包内未找到 {member_name}")

            with archive_file.open(matched_member, "r") as source_file:
                with open(destination, "wb") as output_file:
                    shutil.copyfileobj(source_file, output_file, length=1024 * 1024)

    @staticmethod
    def _find_zip_member(
        archive_file: zipfile.ZipFile,
        member_name: str,
    ) -> str | None:
        normalized_expected = member_name.replace("\\", "/").lower()
        exact_match: str | None = None
        fallback_match: str | None = None

        for zip_name in archive_file.namelist():
            normalized_name = zip_name.replace("\\", "/").lower()
            if normalized_name.endswith("/"):
                continue
            if normalized_name == normalized_expected:
                exact_match = zip_name
                break
            if normalized_name.endswith(f"/{normalized_expected}"):
                fallback_match = zip_name

        return exact_match or fallback_match

    def _create_temp_file(self, dependency_id: str, suffix: str) -> Path:
        temp_dir = self._get_download_temp_dir()
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"{dependency_id}_",
            suffix=suffix,
            delete=False,
            dir=temp_dir,
        )
        temp_file.close()
        return Path(temp_file.name)

    @staticmethod
    def _cleanup_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _guess_download_suffix(url: str, fallback: str) -> str:
        parsed = urllib.parse.urlparse(url)
        suffix = Path(parsed.path).suffix.strip()
        return suffix or fallback

    @staticmethod
    def _parse_content_length(raw_value: Any) -> int | None:
        if raw_value in (None, ""):
            return None
        try:
            content_length = int(raw_value)
        except (TypeError, ValueError):
            return None
        return content_length if content_length > 0 else None

    @staticmethod
    def _get_timeout(download: dict[str, Any] | None) -> int:
        if not download:
            return DEFAULT_DOWNLOAD_TIMEOUT
        raw_timeout = download.get("timeout")
        if raw_timeout in (None, ""):
            return DEFAULT_DOWNLOAD_TIMEOUT
        try:
            return max(int(raw_timeout), 30)
        except (TypeError, ValueError):
            return DEFAULT_DOWNLOAD_TIMEOUT

    @staticmethod
    def _get_download_temp_dir() -> Path:
        return get_temp_dir() / "dependency_downloads"


def install_required_dependencies(
    entries: Sequence[DependencyEntry] | None = None,
    manifest_path: Path | None = None,
    progress_callback: DependencyProgressCallback | None = None,
) -> DependencyInstallBatchResult:
    """Install required dependencies declared in deps.json."""
    installer = DependencyInstaller(
        manifest_path=manifest_path,
        progress_callback=progress_callback,
    )
    return installer.install_required(entries=entries)



def install_recommended_dependencies(
    entries: Sequence[DependencyEntry] | None = None,
    manifest_path: Path | None = None,
    progress_callback: DependencyProgressCallback | None = None,
) -> DependencyInstallBatchResult:
    """Install recommended dependencies declared in deps.json."""
    installer = DependencyInstaller(
        manifest_path=manifest_path,
        progress_callback=progress_callback,
    )
    return installer.install_recommended(entries=entries)



def install_dependency_categories(
    categories: Sequence[str],
    manifest_path: Path | None = None,
    progress_callback: DependencyProgressCallback | None = None,
) -> DependencyInstallRunResult:
    """Install one or more dependency categories declared in deps.json."""
    installer = DependencyInstaller(
        manifest_path=manifest_path,
        progress_callback=progress_callback,
    )
    return installer.install_categories(categories=categories)
