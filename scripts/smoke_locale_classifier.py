"""
Stage 3 smoke: ``_classify_failure`` locale stability (R18.1 / R18.5 / R25.1).

Mirrors the manual checklist bullet "locale / 磁盘 mock / 并发在线调整 /
Playwright 20 次启停冒烟" from task 23.1: runs
:func:`core.download.classifier.classify_failure` — the same function that
``DownloadManager._classify_failure`` delegates to — under both the
``zh`` and ``en`` locales with a mixed grid of ``stop_reason``,
``structured_error.code`` and free-form ``error_message`` inputs, then
asserts the classification output is identical in both locales.

Rationale (design §2.7 / R18.1, R18.5):

* The classifier is meant to be **pure** (no I/O, no locale lookup). If a
  future refactor sneaks a locale-dependent branch in — e.g. calling
  ``utils.i18n.TR`` on the error message — the classifier's output would
  silently diverge between a Chinese user and an English user, and the
  Stage 3 retry / telemetry pipeline would behave differently per locale.
* This smoke catches that regression class without a property-based test
  harness. The optional PBT task 18.3 does the same over Hypothesis-
  generated grids; the smoke covers a fixed representative sample so the
  Stage 3 gate fails fast in CI-profile runs where Hypothesis may be
  skipped.

Scenarios:

1. **stop_reason dominates.** For every :class:`utils.errors.StopReason`
   value (``paused``/``cancelled``/``shutdown``/``ssrf_blocked``/…) plus
   an unmapped custom value, the classifier must return the same bucket
   under ``zh`` and ``en``. ``paused`` must classify as ``"paused"`` —
   never ``"failed"`` — and an unknown stop_reason must bucket as
   ``"other"`` (R18.3).
2. **structured error dominates over message.** When ``stop_reason`` is
   empty but ``task.structured_error.code`` is set, the classifier must
   surface the structured code verbatim regardless of locale — even when
   the ``error_message`` contains Chinese keywords that would otherwise
   trigger the legacy fallback (``用户取消`` / ``超时`` / ``401``).
3. **message keyword fallback is locale-independent.** The keyword table
   inside ``classify_message_keywords`` matches both Chinese and English
   tokens by design (R18.4); the smoke asserts that flipping the
   application locale does not change the classifier's output.

Runs headless in <1s. Exits 0 on pass; non-zero on any deviation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ``utils.i18n`` pulls in PyQt6 via QObject. Pin the offscreen plugin so
# the smoke is deterministic on headless CI hosts.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.download.classifier import classify_failure  # noqa: E402
from utils.errors import StopReason, StructuredError  # noqa: E402
from utils.i18n import I18nManager  # noqa: E402


# ---------------------------------------------------------------------------
# Lightweight DownloadTask stand-in.
# ---------------------------------------------------------------------------
#
# Building a real :class:`core.task_model.DownloadTask` here would drag in
# ``sanitize_title`` and a bunch of Qt-adjacent imports for no benefit —
# the classifier only reads ``stop_reason`` / ``structured_error`` /
# ``error_message`` plus the lock. A plain object with a ``threading.RLock``
# is observably indistinguishable to :func:`classify_failure`.


class _StubTask:
    __slots__ = ("stop_reason", "structured_error", "error_message", "lock")

    def __init__(
        self,
        *,
        stop_reason: str = "",
        structured_error: StructuredError | None = None,
        error_message: str = "",
    ) -> None:
        import threading

        self.stop_reason = stop_reason
        self.structured_error = structured_error
        self.error_message = error_message
        self.lock = threading.RLock()


# ---------------------------------------------------------------------------
# Test grid.
# ---------------------------------------------------------------------------


# (label, task factory, expected classification)
# The factory is a zero-arg callable so each scenario gets a fresh stub
# (classification is pure, but keeping scenarios side-effect-free makes
# failure traces easier to read).
_Scenario = tuple[str, Any, str]


def _known_stop_reason_cases() -> Iterable[_Scenario]:
    """Every canonical ``StopReason`` value plus one unknown bucket."""
    expectations = {
        StopReason.PAUSED: "paused",
        StopReason.CANCELLED: "cancelled",
        StopReason.REMOVED: "removed",
        StopReason.SHUTDOWN: "shutdown",
        StopReason.ENGINE_SWITCH: "engine_switch",
        StopReason.SSRF_BLOCKED: "ssrf_blocked",
        StopReason.CHECKSUM_MISMATCH: "checksum_mismatch",
        StopReason.INSUFFICIENT_DISK: "insufficient_disk",
        StopReason.PATH_TAMPERED: "path_tampered",
        # ``OTHER`` is not in STOP_REASON_CLASSIFICATION on purpose; any
        # non-empty unmapped stop_reason should bucket as ``"other"``.
        StopReason.OTHER: "other",
    }
    for reason, expected in expectations.items():
        label = f"stop_reason={reason.value}"
        yield label, lambda r=reason: _StubTask(stop_reason=r.value), expected

    # An ad-hoc, future stop_reason not in the enum must still win over
    # the message keyword path and bucket as ``"other"`` (R18.3).
    yield (
        "stop_reason=custom_unknown",
        lambda: _StubTask(stop_reason="custom_unknown"),
        "other",
    )


def _structured_error_cases() -> Iterable[_Scenario]:
    """``structured_error.code`` wins over message keywords."""
    err = StructuredError(
        code="checksum_mismatch",
        reason="sha256 digest did not match manifest",
        stage="fs",
    )
    # Mix in an error message that would otherwise classify as ``auth``
    # under the legacy keyword path; the structured code must dominate.
    yield (
        "structured_code beats zh message",
        lambda: _StubTask(
            structured_error=err,
            error_message="HTTP 401 未授权,用户取消",
        ),
        "checksum_mismatch",
    )
    yield (
        "structured_code beats en message",
        lambda: _StubTask(
            structured_error=err,
            error_message="HTTP 401 unauthorized, cancelled by user",
        ),
        "checksum_mismatch",
    )


def _message_fallback_cases() -> Iterable[_Scenario]:
    """Keyword fallback is locale-independent by design (R18.4)."""
    cases: tuple[tuple[str, str, str], ...] = (
        ("zh auth keyword", "下载失败:HTTP 401 未授权", "auth"),
        ("en auth keyword", "download failed: HTTP 401 Unauthorized", "auth"),
        ("zh cancel keyword", "用户取消了下载", "stopped"),
        ("en cancel keyword", "cancelled by user", "stopped"),
        ("zh timeout keyword", "连接超时 timeout", "timeout"),
        ("en timeout keyword", "read timed out after 30s", "timeout"),
        ("parse keyword", "nsig extraction failed: signature", "parse"),
        ("empty message", "", "unknown"),
        ("unrelated message", "something went sideways", "unknown"),
    )
    for label, msg, expected in cases:
        yield (
            f"message_fallback:{label}",
            lambda m=msg: _StubTask(error_message=m),
            expected,
        )


def _all_scenarios() -> list[_Scenario]:
    scenarios: list[_Scenario] = []
    scenarios.extend(_known_stop_reason_cases())
    scenarios.extend(_structured_error_cases())
    scenarios.extend(_message_fallback_cases())
    return scenarios


# ---------------------------------------------------------------------------
# Assertions.
# ---------------------------------------------------------------------------


def assert_classification_stable_across_locales() -> None:
    """Run the full grid under zh and en; outputs must match the baseline."""

    scenarios = _all_scenarios()
    i18n = I18nManager()
    original_lang = i18n.get_language()

    try:
        # Collect the expected result from the scenario table first. We do
        # not rely on the zh run producing the "truth" — the table is the
        # contract; both locales must agree with it.
        failures: list[str] = []

        for lang in ("zh", "en"):
            i18n.set_language(lang)
            for label, factory, expected in scenarios:
                task = factory()
                got = classify_failure(task)
                if got != expected:
                    failures.append(
                        f"locale={lang} scenario={label!r} "
                        f"expected={expected!r} got={got!r}"
                    )

        # Cross-locale check: for each scenario, the zh and en outputs
        # must match each other (stronger than each matching the table
        # individually — catches the case where both locales happen to
        # be wrong in the same way and the table tolerated it).
        for label, factory, _expected in scenarios:
            task_zh = factory()
            task_en = factory()
            i18n.set_language("zh")
            got_zh = classify_failure(task_zh)
            i18n.set_language("en")
            got_en = classify_failure(task_en)
            if got_zh != got_en:
                failures.append(
                    f"cross-locale mismatch on scenario={label!r}: "
                    f"zh={got_zh!r} en={got_en!r}"
                )

        if failures:
            joined = "\n  - ".join(failures)
            raise AssertionError(
                f"classify_failure returned locale-dependent output:\n  - {joined}"
            )
    finally:
        # Leave the singleton in its original state so any subsequent
        # smoke in the same process is not affected.
        i18n.set_language(original_lang)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run() -> None:
    checks = (assert_classification_stable_across_locales,)
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("locale classifier smoke passed")


if __name__ == "__main__":
    run()
