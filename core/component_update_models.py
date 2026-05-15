"""
Data models for component update status, probing, and install flows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ManifestEntry:
    """Minimal manifest projection consumed by ``ComponentUpdateDownloader.download``.

    This type is intentionally decoupled from :class:`DependencyEntry` /
    :class:`RemoteReleaseInfo` so the downloader's R1 invariants (sha256 /
    signature / atomic staging) can be reasoned about in isolation.

    ``signature`` carries either the literal string ``"authenticode"`` to
    request a Windows ``WinVerifyTrust`` verification against the file
    itself, or a base64 / hex encoded detached ECDSA P-256 signature over
    the raw bytes (verified with the pinned public key in
    ``resources/update_signing_key.pem``).
    """

    name: str
    url: str
    filename: str
    sha256: str | None = None
    signature: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ManifestEntry":
        """Build a :class:`ManifestEntry` from a dict, tolerating missing ``signature``.

        Legacy manifests never carried a ``signature`` key; omitting it here
        simply leaves ``signature=None`` and falls through to the sha256-only
        path in the downloader. Unknown keys are ignored for forward
        compatibility.
        """

        def _optional_str(value: Any) -> str | None:
            if value in (None, ""):
                return None
            text = str(value).strip()
            return text or None

        name = _optional_str(payload.get("name")) or _optional_str(payload.get("id"))
        url = _optional_str(payload.get("url"))
        filename = (
            _optional_str(payload.get("filename"))
            or _optional_str(payload.get("asset_name"))
            or _optional_str(payload.get("target_name"))
        )
        if not name or not url or not filename:
            raise ValueError(
                "ManifestEntry requires non-empty 'name', 'url', and 'filename' fields"
            )
        return cls(
            name=name,
            url=url,
            filename=filename,
            sha256=_optional_str(payload.get("sha256")),
            signature=_optional_str(payload.get("signature")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentVersionInfo:
    """Local component version probing result."""

    component_id: str
    label: str
    path: str
    exists: bool
    version: str | None = None
    raw_output: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemoteReleaseInfo:
    """Remote release metadata returned by read-only release checks."""

    component_id: str
    latest_version: str | None = None
    release_url: str | None = None
    published_at: str | None = None
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int | None = None
    etag: str | None = None
    rate_limit_remaining: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentUpdateStatus:
    """Combined component status for UI or CLI display."""

    component_id: str
    label: str
    category: str
    local: ComponentVersionInfo
    remote: RemoteReleaseInfo | None = None
    update_available: bool = False
    status: str = "local_checked"
    message: str | None = None
    last_checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentUpdatePlan:
    """A component update execution plan."""

    entry: Any
    local_version: str | None
    remote_version: str | None
    asset_url: str
    asset_name: str | None
    install_type: str
    target_path: Path


@dataclass(frozen=True)
class ComponentUpdateResult:
    """Structured result of a component update execution."""

    component_id: str
    success: bool
    old_version: str | None = None
    new_version: str | None = None
    backup_path: str | None = None
    error: str | None = None
    warning: str | None = None
    status: str | None = None
    code: str | None = None
    message: str | None = None
    label: str | None = None
    skipped: bool = False
    local_version: str | None = None
    remote_version: str | None = None
    asset_name: str | None = None
    asset_url: str | None = None
    download_path: str | None = None
    staging_dir: str | None = None
    staged_exe_path: str | None = None
    bytes_downloaded: int | None = None
    sha256: str | None = None
    weak_validation: bool = False
    install_code: str | None = None
    install_message: str | None = None
    rollback_attempted: bool = False
    rollback_success: bool | None = None
    rollback_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentBatchUpdateResult:
    """Summary result for updating multiple components sequentially."""

    results: list[ComponentUpdateResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.results if result.success and not result.skipped)

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.results if not result.success and not result.skipped)

    @property
    def skipped_count(self) -> int:
        return sum(1 for result in self.results if result.skipped)

    @property
    def success(self) -> bool:
        return self.failure_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "total": self.total,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "skipped_count": self.skipped_count,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class ComponentUpdateProgressEvent:
    """Progress event for worker bridge."""

    event: str
    component_id: str
    label: str
    detail: str | None = None
    bytes_downloaded: int | None = None
    total_bytes: int | None = None
    percent: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
