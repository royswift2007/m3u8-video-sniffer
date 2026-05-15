"""
Dependency manifest loading helpers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.app_paths import get_dependency_manifest_path, resolve_app_path

MANIFEST_CATEGORIES = ("required", "recommended", "optional")
CATEGORY_LABELS = {
    "required": "必须依赖",
    "recommended": "建议依赖",
    "optional": "可选依赖",
}


@dataclass(frozen=True)
class DependencyDownloadSpec:
    """Download metadata for first install or later update reuse."""

    source: str
    type: str
    url: str | None = None
    repo: str | None = None
    asset_pattern: str | None = None
    member: str | None = None
    timeout: int | None = None


@dataclass(frozen=True)
class DependencyVersionSpec:
    """Local version probing metadata."""

    command: list[str]
    regex: str
    normalize: str | None = None
    timeout: int = 15
    post_install_required: bool = True


@dataclass(frozen=True)
class DependencyUpdateSpec:
    """Remote update and installation metadata."""

    enabled: bool = False
    release_source: str = ""
    repo: str | None = None
    latest_url: str | None = None
    # Optional separate endpoint that returns a plain-text version
    # string. Used by ``release_source="direct"`` components whose
    # ``latest_url`` points at the download asset itself (and therefore
    # carries no version information in the path) — e.g. gyan.dev
    # serves the asset zip on one URL and the version number on
    # ``<builds>/release-version``. When set, it is fetched first and
    # its body (passed through ``version_regex`` if configured) becomes
    # the ``latest_version``; ``latest_url`` stays the download target.
    version_url: str | None = None
    asset_pattern: str | None = None
    version_source: str = "tag_name"
    version_regex: str | None = None
    checksum: dict[str, Any] | None = None
    install_strategy: str = "replace_file"
    requires_process_free: bool = True
    post_install_version_required: bool | None = None


@dataclass(frozen=True)
class DependencyEntry:
    """Single dependency definition from the manifest."""

    id: str
    label: str
    relative_path: str
    category: str
    download: DependencyDownloadSpec | None = None
    version: DependencyVersionSpec | None = None
    update: DependencyUpdateSpec | None = None

    @property
    def path(self) -> Path:
        """Return absolute filesystem path for this dependency."""
        return resolve_app_path(self.relative_path)


class DependencyManifest:
    """Loaded dependency manifest with category helpers."""

    def __init__(self, manifest_path: Path | None = None):
        self.manifest_path = manifest_path or get_dependency_manifest_path()
        self._entries_by_category = self._load_entries()

    def _load_entries(self) -> dict[str, list[DependencyEntry]]:
        with open(self.manifest_path, "r", encoding="utf-8") as manifest_file:
            payload = json.load(manifest_file)

        if not isinstance(payload, dict):
            raise ValueError("依赖清单顶层必须是对象")

        entries_by_category: dict[str, list[DependencyEntry]] = {}
        for category in MANIFEST_CATEGORIES:
            entries_by_category[category] = self._parse_category(payload.get(category, []), category)
        return entries_by_category

    def _parse_category(self, raw_entries: Any, category: str) -> list[DependencyEntry]:
        if raw_entries is None:
            return []
        if not isinstance(raw_entries, list):
            raise ValueError(f"依赖分类必须是数组: {category}")

        entries: list[DependencyEntry] = []
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                raise ValueError(f"依赖项必须是对象: {category}[{index}]")

            context = f"{category}[{index}]"
            dependency_id = str(raw_entry.get("id", "")).strip()
            relative_path = str(raw_entry.get("path", "")).strip()
            label = str(raw_entry.get("label") or dependency_id).strip()
            if not dependency_id or not relative_path:
                raise ValueError(f"依赖项缺少 id 或 path: {context}")

            entries.append(
                DependencyEntry(
                    id=dependency_id,
                    label=label,
                    relative_path=relative_path,
                    category=category,
                    download=self._parse_download_spec(raw_entry.get("download"), context),
                    version=self._parse_version_spec(raw_entry.get("version"), context),
                    update=self._parse_update_spec(raw_entry.get("update"), context),
                )
            )
        return entries

    def _parse_download_spec(self, raw_download: Any, context: str) -> DependencyDownloadSpec | None:
        """Parse optional download metadata while accepting old manifests."""
        if raw_download in (None, ""):
            return None
        if not isinstance(raw_download, dict):
            raise ValueError(f"download 字段必须是对象: {context}")
        source = str(raw_download.get("source") or "direct").strip().lower()
        download_type = str(raw_download.get("type") or "file").strip().lower()
        if not source or not download_type:
            raise ValueError(f"download 字段缺少 source 或 type: {context}")
        return DependencyDownloadSpec(
            source=source,
            type=download_type,
            url=self._optional_string(raw_download.get("url")),
            repo=self._optional_string(raw_download.get("repo")),
            asset_pattern=self._optional_string(raw_download.get("asset_pattern")),
            member=self._optional_string(raw_download.get("member")),
            timeout=self._optional_int(raw_download.get("timeout")),
        )

    def _parse_version_spec(self, raw_version: Any, context: str) -> DependencyVersionSpec | None:
        """Parse optional local version probing metadata."""
        if raw_version in (None, ""):
            return None
        if not isinstance(raw_version, dict):
            raise ValueError(f"version 字段必须是对象: {context}")
        raw_command = raw_version.get("command")
        if isinstance(raw_command, str):
            command = [raw_command]
        elif isinstance(raw_command, list) and all(isinstance(item, str) for item in raw_command):
            command = [item for item in raw_command if item]
        else:
            raise ValueError(f"version.command 必须是字符串数组: {context}")
        regex = str(raw_version.get("regex") or "").strip()
        if not command or not regex:
            raise ValueError(f"version 字段缺少 command 或 regex: {context}")
        timeout = self._optional_int(raw_version.get("timeout")) or 15
        post_install_required = self._optional_bool(raw_version.get("post_install_required"), default=True)
        return DependencyVersionSpec(
            command=command,
            regex=regex,
            normalize=self._optional_string(raw_version.get("normalize")),
            timeout=max(1, timeout),
            post_install_required=True if post_install_required is None else post_install_required,
        )

    def _parse_update_spec(self, raw_update: Any, context: str) -> DependencyUpdateSpec | None:
        """Parse optional update metadata; absence keeps legacy manifests valid."""
        if raw_update in (None, ""):
            return None
        if not isinstance(raw_update, dict):
            raise ValueError(f"update 字段必须是对象: {context}")
        checksum = raw_update.get("checksum")
        if checksum is not None and not isinstance(checksum, dict):
            raise ValueError(f"update.checksum 必须是对象或 null: {context}")
        return DependencyUpdateSpec(
            enabled=bool(raw_update.get("enabled", False)),
            release_source=str(raw_update.get("release_source") or "").strip().lower(),
            repo=self._optional_string(raw_update.get("repo")),
            latest_url=self._optional_string(raw_update.get("latest_url")),
            version_url=self._optional_string(raw_update.get("version_url")),
            asset_pattern=self._optional_string(raw_update.get("asset_pattern")),
            version_source=str(raw_update.get("version_source") or "tag_name").strip().lower(),
            version_regex=self._optional_string(raw_update.get("version_regex")),
            checksum=checksum,
            install_strategy=str(raw_update.get("install_strategy") or "replace_file").strip().lower(),
            requires_process_free=bool(raw_update.get("requires_process_free", True)),
            post_install_version_required=self._optional_bool(raw_update.get("post_install_version_required"), default=None),
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"字段必须是整数: {value!r}") from exc

    @staticmethod
    def _optional_bool(value: Any, default: bool | None = None) -> bool | None:
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
        if isinstance(value, int):
            return bool(value)
        raise ValueError(f"字段必须是布尔值: {value!r}")

    def get_entries(self, category: str) -> list[DependencyEntry]:
        """Return dependency entries for a category."""
        return list(self._entries_by_category.get(category, []))

    def get_required_entries(self) -> list[DependencyEntry]:
        """Return required dependency entries."""
        return self.get_entries("required")

    def get_recommended_entries(self) -> list[DependencyEntry]:
        """Return recommended dependency entries."""
        return self.get_entries("recommended")

    def get_optional_entries(self) -> list[DependencyEntry]:
        """Return optional dependency entries."""
        return self.get_entries("optional")

    def get_update_enabled_entries(
        self,
        include_recommended: bool = True,
        include_optional: bool = False,
    ) -> list[DependencyEntry]:
        """Return dependencies that declare enabled component update metadata."""
        categories = ["required"]
        if include_recommended:
            categories.append("recommended")
        if include_optional:
            categories.append("optional")
        entries: list[DependencyEntry] = []
        for category in categories:
            entries.extend(
                entry for entry in self.get_entries(category) if entry.update and entry.update.enabled
            )
        return entries

    def get_category_label(self, category: str) -> str:
        """Return user-facing label for a dependency category."""
        normalized_category = str(category).strip().lower()
        return CATEGORY_LABELS.get(normalized_category, normalized_category or "未知分类")

    def format_category_entries(self, category: str) -> list[str]:
        """Return user-facing item lines for a category."""
        entries = self.get_entries(category)
        return [f"{entry.label} ({entry.relative_path})" for entry in entries]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Return a serializable snapshot including optional extended metadata."""
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for category in MANIFEST_CATEGORIES:
            category_entries: list[dict[str, Any]] = []
            for entry in self.get_entries(category):
                item: dict[str, Any] = {
                    "id": entry.id,
                    "label": entry.label,
                    "path": entry.relative_path,
                }
                if entry.download is not None:
                    item["download"] = self._strip_none(asdict(entry.download))
                if entry.version is not None:
                    item["version"] = self._strip_none(asdict(entry.version))
                if entry.update is not None:
                    item["update"] = self._strip_none(asdict(entry.update))
                category_entries.append(item)
            snapshot[category] = category_entries
        return snapshot

    @staticmethod
    def _strip_none(payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if value is not None}


def load_dependency_manifest(manifest_path: Path | None = None) -> DependencyManifest:
    """Load the dependency manifest from disk."""
    return DependencyManifest(manifest_path=manifest_path)
