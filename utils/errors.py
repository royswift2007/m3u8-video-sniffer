"""Structured error primitives shared across the application.

This module is standard-library only and pure-function / pure-dataclass.
It is safe to import from any layer (utils / core / engines / ui) and must
not introduce side effects at import time.

Exports:
    StopReason      -- canonical stop-reason enum used by DownloadTask.
    StructuredError -- frozen dataclass representing a machine-parsable
                       failure (``code``, ``reason``, ``details``, ``stage``).
    STAGE_VALUES    -- allowed values for ``StructuredError.stage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


__all__ = (
    "StopReason",
    "StructuredError",
    "STAGE_VALUES",
)


class StopReason(str, Enum):
    """Canonical stop reasons for ``DownloadTask``.

    The enum inherits ``str`` so that existing code paths that compare a
    task's ``stop_reason`` against a literal (e.g. ``"paused"``) keep
    working during the migration defined in design 1.3 / 2.3 / 3.3.
    """

    PAUSED = "paused"
    CANCELLED = "cancelled"
    REMOVED = "removed"
    SHUTDOWN = "shutdown"
    ENGINE_SWITCH = "engine_switch"
    SSRF_BLOCKED = "ssrf_blocked"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    INSUFFICIENT_DISK = "insufficient_disk"
    PATH_TAMPERED = "path_tampered"
    OTHER = "other"


# Allowed values for ``StructuredError.stage``. Kept as a frozenset so that
# the constant itself is immutable and cheap to compare against.
STAGE_VALUES: frozenset = frozenset(
    {
        "network",
        "manifest",
        "fs",
        "process",
        "ssrf",
        "auth",
        "other",
    }
)


# Reusable empty mapping to avoid allocating a fresh dict on every default
# construction while still presenting an immutable Mapping to callers.
_EMPTY_DETAILS: Mapping[str, Any] = MappingProxyType({})


def _freeze_details(details: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable Mapping wrapping ``details``.

    Passing a plain ``dict`` keeps existing call sites ergonomic; the
    returned wrapper prevents downstream code from mutating shared state.
    """

    if details is None:
        return _EMPTY_DETAILS
    if isinstance(details, MappingProxyType):
        return details
    # Copy into a fresh dict so later mutations of the caller's object
    # cannot leak into the frozen error.
    return MappingProxyType(dict(details))


@dataclass(frozen=True)
class StructuredError:
    """Immutable, machine-parsable representation of a failure."""

    code: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_DETAILS)
    stage: str = "other"

    def __post_init__(self) -> None:  # pragma: no cover - trivial validation
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("StructuredError.code must be a non-empty string")
        if not isinstance(self.reason, str):
            raise ValueError("StructuredError.reason must be a string")
        if self.stage not in STAGE_VALUES:
            raise ValueError(
                f"StructuredError.stage must be one of {sorted(STAGE_VALUES)}, "
                f"got {self.stage!r}"
            )
        # Replace ``details`` with a read-only Mapping so callers cannot
        # mutate it after construction (frozen dataclass + frozen mapping).
        object.__setattr__(self, "details", _freeze_details(self.details))

    def to_dict(self) -> dict[str, Any]:
        """Return a plain ``dict`` snapshot suitable for JSON/logging."""

        return {
            "code": self.code,
            "reason": self.reason,
            "stage": self.stage,
            "details": dict(self.details),
        }
