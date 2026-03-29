"""
Dependency availability checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.dependency_manifest import (
    MANIFEST_CATEGORIES,
    DependencyEntry,
    DependencyManifest,
    load_dependency_manifest,
)


@dataclass(frozen=True)
class DependencyCheckResult:
    """Runtime status of a dependency."""

    entry: DependencyEntry
    exists: bool


class DependencyChecker:
    """Check dependency files declared in the manifest."""

    def __init__(self, manifest: DependencyManifest | None = None):
        self.manifest = manifest or load_dependency_manifest()

    @staticmethod
    def normalize_category(category: str) -> str:
        """Normalize and validate a manifest category."""
        from utils.i18n import TR
        normalized_category = str(category).strip().lower()
        if normalized_category not in MANIFEST_CATEGORIES:
            raise ValueError(f"{TR('log_dep_unsupported_category')}: {category}")
        return normalized_category

    def check_category(self, category: str) -> list[DependencyCheckResult]:
        """Check all dependencies in one category."""
        normalized_category = self.normalize_category(category)
        results: list[DependencyCheckResult] = []
        for entry in self.manifest.get_entries(normalized_category):
            results.append(
                DependencyCheckResult(
                    entry=entry,
                    exists=entry.path.exists(),
                )
            )
        return results

    def get_missing_entries(self, category: str) -> list[DependencyEntry]:
        """Return missing entries for a category."""
        return [result.entry for result in self.check_category(category) if not result.exists]

    def get_missing_entries_for_categories(
        self,
        categories: Sequence[str],
    ) -> dict[str, list[DependencyEntry]]:
        """Return missing entries grouped by category."""
        missing_entries_by_category: dict[str, list[DependencyEntry]] = {}
        seen: set[str] = set()
        for category in categories:
            normalized_category = self.normalize_category(category)
            if normalized_category in seen:
                continue
            seen.add(normalized_category)
            missing_entries_by_category[normalized_category] = self.get_missing_entries(
                normalized_category
            )
        return missing_entries_by_category

    def get_missing_required_entries(self) -> list[DependencyEntry]:
        """Return missing required dependencies."""
        return self.get_missing_entries("required")

    def has_missing_required_dependencies(self) -> bool:
        """Return True when any required dependency is missing."""
        return bool(self.get_missing_required_entries())


def get_missing_required_dependencies(
    manifest: DependencyManifest | None = None,
) -> list[DependencyEntry]:
    """Convenience helper for startup checks."""
    return DependencyChecker(manifest=manifest).get_missing_required_entries()



def get_missing_dependencies_for_categories(
    categories: Sequence[str],
    manifest: DependencyManifest | None = None,
) -> dict[str, list[DependencyEntry]]:
    """Convenience helper for grouped dependency checks."""
    return DependencyChecker(manifest=manifest).get_missing_entries_for_categories(
        categories
    )
