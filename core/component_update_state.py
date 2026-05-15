"""
Persistent component update state store.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.app_paths import get_component_update_state_path
from core.component_update_models import ComponentUpdateResult
from utils.json_store import corrupt_path_for, write_json_atomic

STATE_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp using Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ComponentUpdateStateStore:
    """Safe reader/writer for component_updates.json."""

    def __init__(self, state_path: Path | None = None):
        self.state_path = Path(state_path) if state_path is not None else get_component_update_state_path()

    def default_state(self) -> dict[str, Any]:
        """Return a fresh default state object."""
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_startup_check_at": None,
            "components": {},
        }

    def load(self) -> dict[str, Any]:
        """Load state; quarantine corrupted files and return defaults instead of raising."""
        if not self.state_path.exists():
            return self.default_state()
        try:
            with open(self.state_path, "r", encoding="utf-8") as state_file:
                payload = json.load(state_file)
            return self._normalize_state(payload)
        except Exception:
            self._quarantine_corrupt_state()
            return self.default_state()

    def save(self, state: dict[str, Any]) -> None:
        """Atomically save normalized state and refresh sibling backup."""
        write_json_atomic(self.state_path, self._normalize_state(state), indent=2, ensure_ascii=False)

    def get_component_state(self, component_id: str) -> dict[str, Any]:
        """Return a copy of one component state."""
        state = self.load()
        component_state = state.get("components", {}).get(component_id, {})
        return deepcopy(component_state) if isinstance(component_state, dict) else {}

    def update_component_state(self, component_id: str, patch: dict[str, Any]) -> None:
        """Merge and save one component state patch."""
        state = self.load()
        components = state.setdefault("components", {})
        current = components.get(component_id)
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        components[component_id] = current
        self.save(state)

    def get_etag(self, component_id: str, url: str) -> str | None:
        """Return cached ETag for a component URL."""
        component_state = self.get_component_state(component_id)
        etags = component_state.get("etag_by_url", {})
        if not isinstance(etags, dict):
            return None
        etag = etags.get(url)
        return str(etag) if etag else None

    def set_etag(self, component_id: str, url: str, etag: str | None) -> None:
        """Set or clear cached ETag for a component URL."""
        component_state = self.get_component_state(component_id)
        etags = component_state.get("etag_by_url", {})
        if not isinstance(etags, dict):
            etags = {}
        if etag:
            etags[url] = etag
        else:
            etags.pop(url, None)
        component_state["etag_by_url"] = etags
        self.update_component_state(component_id, component_state)

    def record_update_result(self, result: ComponentUpdateResult) -> None:
        """Record a component update result for later display."""
        self.update_component_state(
            result.component_id,
            {
                "last_update_result": {
                    "success": result.success,
                    "old_version": result.old_version,
                    "new_version": result.new_version,
                    "backup_path": result.backup_path,
                    "error": result.error,
                    "at": utc_now_iso(),
                }
            },
        )

    def _normalize_state(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self.default_state()
        normalized = self.default_state()
        normalized.update(payload)
        normalized["schema_version"] = STATE_SCHEMA_VERSION
        if not isinstance(normalized.get("components"), dict):
            normalized["components"] = {}
        return normalized

    def _quarantine_corrupt_state(self) -> None:
        try:
            quarantine_path = corrupt_path_for(self.state_path)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.state_path, quarantine_path)
        except OSError:
            pass
