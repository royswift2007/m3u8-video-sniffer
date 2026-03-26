"""
Dependency manifest loading helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
class DependencyEntry:
    """Single dependency definition from the manifest."""

    id: str
    label: str
    relative_path: str
    category: str

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
            entries_by_category[category] = self._parse_category(
                payload.get(category, []),
                category,
            )
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

            dependency_id = str(raw_entry.get("id", "")).strip()
            relative_path = str(raw_entry.get("path", "")).strip()
            label = str(raw_entry.get("label") or dependency_id).strip()
            if not dependency_id or not relative_path:
                raise ValueError(f"依赖项缺少 id 或 path: {category}[{index}]")

            entries.append(
                DependencyEntry(
                    id=dependency_id,
                    label=label,
                    relative_path=relative_path,
                    category=category,
                )
            )
        return entries

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

    def get_category_label(self, category: str) -> str:
        """Return user-facing label for a dependency category."""
        normalized_category = str(category).strip().lower()
        return CATEGORY_LABELS.get(normalized_category, normalized_category or "未知分类")

    def format_category_entries(self, category: str) -> list[str]:
        """Return user-facing item lines for a category."""
        entries = self.get_entries(category)
        return [f"{entry.label} ({entry.relative_path})" for entry in entries]

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        """Return a serializable snapshot."""
        snapshot: dict[str, list[dict[str, str]]] = {}
        for category in MANIFEST_CATEGORIES:
            snapshot[category] = [
                {
                    "id": entry.id,
                    "label": entry.label,
                    "path": entry.relative_path,
                }
                for entry in self.get_entries(category)
            ]
        return snapshot


def load_dependency_manifest(manifest_path: Path | None = None) -> DependencyManifest:
    """Load the dependency manifest from disk."""
    return DependencyManifest(manifest_path=manifest_path)
