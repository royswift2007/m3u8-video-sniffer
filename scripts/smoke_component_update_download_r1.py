"""
Offline smoke checks for the R1 ``ComponentUpdateDownloader.download`` entry.

Covers:
* missing sha256 + missing signature -> failure(missing_checksum) and no staging write
* diagnostic escape hatch (config + env var both armed) -> allows weak verification
* sha256 mismatch -> failure(checksum_mismatch) and tmp file removed
* sha256 match -> success with staged file at ``staging_dir / entry.filename``
* signature request with cryptography unavailable / bad signature -> structured failure
* ManifestEntry.signature defaults to None from legacy dict (from_mapping)
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.component_update_downloader import (  # noqa: E402
    ComponentUpdateDownloader,
    DownloadResult,
)
from core.component_update_models import ManifestEntry  # noqa: E402


class FakeResponse:
    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


def _opener(payload: bytes):
    def _call(request, timeout):  # noqa: ARG001
        return FakeResponse(payload)

    return _call


def _payload() -> bytes:
    return b"MZfake-payload-for-r1-smoke"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _downloader(
    payload: bytes,
    *,
    allow_weak: bool = False,
) -> ComponentUpdateDownloader:
    provider = (lambda key: True if key == "security.allow_weak_manifest_verification" else None) if allow_weak else (lambda key: None)
    return ComponentUpdateDownloader(opener=_opener(payload), config_provider=provider)


def _expect(result: DownloadResult, *, code: str | None = None, success: bool) -> None:
    assert result.success is success, (result, code)
    if code is not None:
        assert result.error is not None and result.error.code == code, (result.error, code)


def assert_missing_checksum_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        entry = ManifestEntry(
            name="fake",
            url="https://example.invalid/a.exe",
            filename="a.exe",
        )
        result = _downloader(_payload()).download(entry, staging_dir=staging)
        _expect(result, code="missing_checksum", success=False)
        # Nothing must have been written into staging.
        assert not (staging / "a.exe").exists(), staging
        # No leftover .part files either.
        assert not any(staging.glob(".dl-*.part")), list(staging.iterdir())


def assert_missing_checksum_refused_without_env_even_if_config_true() -> None:
    # allow_weak=True alone must NOT bypass: the env var is also required.
    os.environ.pop("M3U8D_SECURITY_DIAGNOSTIC", None)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        entry = ManifestEntry(
            name="fake",
            url="https://example.invalid/a.exe",
            filename="a.exe",
        )
        result = _downloader(_payload(), allow_weak=True).download(entry, staging_dir=staging)
        _expect(result, code="missing_checksum", success=False)


def assert_diagnostic_dual_armed_allows_weak() -> None:
    os.environ["M3U8D_SECURITY_DIAGNOSTIC"] = "1"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            entry = ManifestEntry(
                name="fake",
                url="https://example.invalid/a.exe",
                filename="a.exe",
            )
            result = _downloader(_payload(), allow_weak=True).download(entry, staging_dir=staging)
            _expect(result, success=True)
            assert (staging / "a.exe").read_bytes() == _payload()
    finally:
        os.environ.pop("M3U8D_SECURITY_DIAGNOSTIC", None)


def assert_sha256_match_success() -> None:
    data = _payload()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        entry = ManifestEntry(
            name="fake",
            url="https://example.invalid/a.exe",
            filename="a.exe",
            sha256=_sha256(data),
        )
        result = _downloader(data).download(entry, staging_dir=staging)
        _expect(result, success=True)
        assert (staging / "a.exe").read_bytes() == data
        assert result.sha256 == _sha256(data)
        assert result.bytes_downloaded == len(data)
        # .part files cleaned.
        assert not any(staging.glob(".dl-*.part")), list(staging.iterdir())


def assert_sha256_mismatch_removes_tmp() -> None:
    data = _payload()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        entry = ManifestEntry(
            name="fake",
            url="https://example.invalid/a.exe",
            filename="a.exe",
            sha256="0" * 64,
        )
        result = _downloader(data).download(entry, staging_dir=staging)
        _expect(result, code="checksum_mismatch", success=False)
        assert not (staging / "a.exe").exists()
        assert not any(staging.glob(".dl-*.part")), list(staging.iterdir())
        assert result.error and result.error.details.get("expected") == "0" * 64
        assert result.error.details.get("got") == _sha256(data)


def assert_signature_invalid_hex_sig_removes_tmp() -> None:
    # Provide a sha256 (so missing_checksum does not short-circuit) AND a
    # detached signature that cannot verify (random hex bytes). If the
    # cryptography library is missing we expect verify_unavailable instead.
    data = _payload()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        entry = ManifestEntry(
            name="fake",
            url="https://example.invalid/a.exe",
            filename="a.exe",
            sha256=_sha256(data),
            signature="deadbeef" * 8,  # 32 bytes of bogus hex
        )
        result = _downloader(data).download(entry, staging_dir=staging)
        assert not result.success, result
        assert result.error is not None
        assert result.error.code in ("signature_invalid", "verify_unavailable"), result.error
        assert not (staging / "a.exe").exists()
        assert not any(staging.glob(".dl-*.part")), list(staging.iterdir())


def assert_manifest_entry_tolerates_missing_signature_key() -> None:
    legacy = {
        "name": "legacy",
        "url": "https://example.invalid/x.exe",
        "filename": "x.exe",
        "sha256": "a" * 64,
    }
    entry = ManifestEntry.from_mapping(legacy)
    assert entry.signature is None
    assert entry.sha256 == "a" * 64


def main() -> int:
    checks = [
        assert_missing_checksum_refused,
        assert_missing_checksum_refused_without_env_even_if_config_true,
        assert_diagnostic_dual_armed_allows_weak,
        assert_sha256_match_success,
        assert_sha256_mismatch_removes_tmp,
        assert_signature_invalid_hex_sig_removes_tmp,
        assert_manifest_entry_tolerates_missing_signature_key,
    ]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("component update R1 download offline smoke: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
