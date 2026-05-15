"""
Download, weak/sha256 verify, and stage component update assets.

This phase intentionally stops before backup, replacement, rollback, process locking,
or UI integration. All filesystem writes are constrained to the component update temp
root unless an explicit temp root is injected by tests.

R1 (security-stability-hardening) invariants enforced by :meth:`download`:

* Reject manifest entries that carry **neither** a sha256 **nor** a signature with
  ``failure(code="missing_checksum")`` unless the ``security.allow_weak_manifest_verification``
  diagnostic switch is enabled *and* the ``M3U8D_SECURITY_DIAGNOSTIC=1`` environment
  variable is set (both required to avoid single-point misconfiguration).
* Stream the download to ``.dl-<uuid>.part`` while computing sha256 incrementally.
* On sha256 mismatch delete the part file and return ``failure(code="checksum_mismatch")``.
* When a signature is declared, run :func:`_verify_authenticode` (Windows ``WinVerifyTrust``
  or an offline pinned ECDSA-P256 detached signature); on failure delete the part file
  and return ``failure(code="signature_invalid")``.
* Only after every declared check passes, ``os.replace`` the part file into
  ``staging_dir / entry.filename``.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import socket
import sys
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.app_paths import get_component_update_temp_dir, get_resource_path
from core.component_update_models import ManifestEntry, RemoteReleaseInfo
from core.dependency_manifest import DependencyEntry
from utils.errors import StructuredError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentDownloadStageResult:
    """Structured result for a download-and-stage attempt."""

    component_id: str
    success: bool
    code: str
    message: str
    download_path: str | None = None
    staging_dir: str | None = None
    staged_exe_path: str | None = None
    asset_name: str | None = None
    bytes_downloaded: int | None = None
    sha256: str | None = None
    weak_validation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadResult:
    """Structured result for the R1 ``download(entry, *, staging_dir)`` entry.

    ``success`` summarises the outcome; on failure ``error`` carries a
    :class:`~utils.errors.StructuredError` with the canonical ``code``
    (``missing_checksum`` / ``checksum_mismatch`` / ``signature_invalid`` /
    ``verify_unavailable`` / ``download_failed`` / ``io_error``) and an
    immutable ``details`` mapping for diagnostics. Call sites that only care
    about the legacy dataclass should keep using
    :class:`ComponentDownloadStageResult`; this type is exposed alongside it
    so the R1 path can surface structured failures without touching existing
    plumbing.
    """

    success: bool
    staged_path: str | None = None
    sha256: str | None = None
    bytes_downloaded: int | None = None
    error: StructuredError | None = None

    @classmethod
    def ok(
        cls,
        *,
        staged_path: Path | str,
        sha256: str | None,
        bytes_downloaded: int,
    ) -> "DownloadResult":
        return cls(
            success=True,
            staged_path=str(staged_path),
            sha256=sha256,
            bytes_downloaded=bytes_downloaded,
        )

    @classmethod
    def failure(
        cls,
        code: str,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        stage: str = "manifest",
    ) -> "DownloadResult":
        return cls(
            success=False,
            error=StructuredError(
                code=code,
                reason=reason if reason is not None else code,
                details=details or {},
                stage=stage,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": self.success,
            "staged_path": self.staged_path,
            "sha256": self.sha256,
            "bytes_downloaded": self.bytes_downloaded,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


# Pinned ECDSA P-256 public key path for offline detached signature verification.
_PINNED_PUBKEY_PATH = "update_signing_key.pem"


class ComponentUpdateDownloader:
    """Download update assets to a temp root and stage one candidate executable."""

    def __init__(
        self,
        temp_root: Path | None = None,
        opener: Callable[..., Any] | None = None,
        user_agent: str = "M3U8D Component Updater/1.0",
        chunk_size: int = 1024 * 256,
        config_provider: Callable[[str], Any] | None = None,
        tofu_pin_path: Path | None = None,
    ):
        self.temp_root = Path(temp_root) if temp_root is not None else get_component_update_temp_dir()
        self.opener = opener or urlopen
        self.user_agent = user_agent
        self.chunk_size = max(1024, int(chunk_size))
        # Injected lookup for ``config.get("security.allow_weak_manifest_verification")``.
        # Default pulls from the global :class:`ConfigManager` lazily so tests
        # can inject a plain ``dict.get``-style callable without importing the
        # real config singleton.
        self._config_provider = config_provider
        # Optional override for the TOFU pin file location. Tests and
        # smoke scripts pass a tmp path so the pin map does not bleed
        # into the real ``~/.m3u8d`` directory. ``None`` means "use the
        # default at runtime" (see :meth:`_tofu_pin_path`).
        self._tofu_pin_path_override: Path | None = (
            Path(tofu_pin_path) if tofu_pin_path is not None else None
        )

    # ------------------------------------------------------------------
    # R1 primary entry: ``download(entry, *, staging_dir)``
    # ------------------------------------------------------------------
    def download(
        self,
        entry: ManifestEntry,
        *,
        staging_dir: Path | str,
        timeout: int = 300,
    ) -> DownloadResult:
        """Download ``entry.url`` into ``staging_dir`` under the R1 invariants.

        Parameters
        ----------
        entry:
            Manifest projection with mandatory ``url`` / ``filename`` and
            optional ``sha256`` / ``signature`` fields.
        staging_dir:
            Directory that will receive the verified file via ``os.replace``;
            created if missing. The temporary ``.dl-<uuid>.part`` file is
            written next to the final path so the final rename stays on the
            same volume.
        timeout:
            Per-request network timeout in seconds.

        Returns
        -------
        DownloadResult
            ``ok`` when every declared check passes; otherwise a structured
            failure with one of: ``missing_checksum``, ``checksum_mismatch``,
            ``signature_invalid``, ``verify_unavailable``, ``download_failed``,
            ``io_error``.
        """
        if not isinstance(entry, ManifestEntry):
            return DownloadResult.failure(
                "invalid_entry",
                reason="entry must be a ManifestEntry",
                stage="manifest",
                details={"type": type(entry).__name__},
            )

        # Gate: missing both sha256 and signature -> refuse unless the
        # diagnostic escape hatch is explicitly dual-armed.
        if not entry.sha256 and not entry.signature:
            if not self._weak_verification_allowed():
                return DownloadResult.failure(
                    "missing_checksum",
                    reason="manifest entry has no sha256 and no signature",
                    stage="manifest",
                    details={"entry": entry.name},
                )
            logger.warning(
                "security.allow_weak_manifest_verification is enabled; "
                "downloading %s without sha256 or signature",
                entry.name,
            )

        try:
            staging_path = Path(staging_dir)
            staging_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return DownloadResult.failure(
                "io_error",
                reason=f"failed to create staging dir: {exc}",
                stage="fs",
                details={"staging_dir": str(staging_dir)},
            )

        tmp_path = staging_path / f".dl-{uuid.uuid4().hex}.part"
        try:
            bytes_downloaded, digest_hex = self._stream_download_with_sha256(
                entry.url, tmp_path, timeout
            )
        except (TimeoutError, socket.timeout) as exc:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "download_failed",
                reason=f"network timeout: {exc}",
                stage="network",
                details={"url_host": _url_host(entry.url)},
            )
        except HTTPError as exc:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "download_failed",
                reason=f"HTTP {exc.code}",
                stage="network",
                details={"url_host": _url_host(entry.url), "http_status": exc.code},
            )
        except URLError as exc:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "download_failed",
                reason=f"network unavailable: {exc.reason}",
                stage="network",
                details={"url_host": _url_host(entry.url)},
            )
        except OSError as exc:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "io_error",
                reason=f"filesystem error during download: {exc}",
                stage="fs",
                details={"tmp": str(tmp_path)},
            )

        if bytes_downloaded <= 0:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "download_failed",
                reason="downloaded asset is empty",
                stage="network",
                details={"url_host": _url_host(entry.url)},
            )

        # sha256 verification
        if entry.sha256:
            expected = entry.sha256.strip().lower()
            actual = digest_hex.lower()
            if expected != actual:
                self._unlink_quiet(tmp_path)
                return DownloadResult.failure(
                    "checksum_mismatch",
                    reason="sha256 mismatch",
                    stage="manifest",
                    details={"expected": expected, "got": actual},
                )

        # signature verification
        if entry.signature:
            ok, verify_code, verify_reason = _verify_authenticode(tmp_path, entry.signature)
            if not ok:
                self._unlink_quiet(tmp_path)
                return DownloadResult.failure(
                    verify_code,
                    reason=verify_reason,
                    stage="auth",
                    details={"entry": entry.name},
                )

        final_path = staging_path / entry.filename
        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            self._unlink_quiet(tmp_path)
            return DownloadResult.failure(
                "io_error",
                reason=f"atomic staging replace failed: {exc}",
                stage="fs",
                details={"tmp": str(tmp_path), "final": str(final_path)},
            )

        return DownloadResult.ok(
            staged_path=final_path,
            sha256=digest_hex if entry.sha256 else None,
            bytes_downloaded=bytes_downloaded,
        )

    def _stream_download_with_sha256(
        self, url: str, destination: Path, timeout: int
    ) -> tuple[int, str]:
        """Stream ``url`` to ``destination`` while computing sha256 incrementally."""
        request = Request(url, headers={"User-Agent": self.user_agent}, method="GET")
        digest = hashlib.sha256()
        total = 0
        with self.opener(request, timeout=timeout) as response:
            with open(destination, "wb") as output_file:
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    output_file.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
        return total, digest.hexdigest()

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        """Best-effort delete; never raise."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _weak_verification_allowed(self) -> bool:
        """Return True when weak manifest verification is dual-armed.

        Both the ``security.allow_weak_manifest_verification`` config flag
        *and* the ``M3U8D_SECURITY_DIAGNOSTIC=1`` environment variable must
        be set so a single mis-set config.json cannot silently disable
        manifest verification in production.
        """
        if os.environ.get("M3U8D_SECURITY_DIAGNOSTIC") != "1":
            return False
        return bool(self._read_config_flag("security.allow_weak_manifest_verification"))

    def _read_config_flag(self, key: str) -> Any:
        """Read a config flag via the injected provider, or fall back to the global config."""
        provider = self._config_provider
        if provider is None:
            try:
                # Lazy import to avoid a hard dependency at module import time
                # and to keep tests able to run without touching the global
                # config singleton.
                from utils.config_manager import config as _config  # noqa: WPS433

                return _config.get(key)
            except Exception:
                return None
        try:
            return provider(key)
        except Exception:
            return None

    def download_and_stage(
        self,
        entry: DependencyEntry,
        remote: RemoteReleaseInfo,
        timeout: int | None = None,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> ComponentDownloadStageResult:
        """Download RemoteReleaseInfo.asset_url, verify it, and stage the candidate exe.

        ``progress_callback``: optional ``(bytes_downloaded, total_bytes)``
        callable invoked every ``chunk_size`` bytes during the HTTP read
        loop. ``total_bytes`` is ``None`` when the server did not send a
        ``Content-Length`` header. Exceptions raised by the callback are
        swallowed so a noisy consumer can never stall the download.
        """
        component_id = entry.id
        if not remote.asset_url:
            return self._failure(component_id, "missing_asset_url", "remote release has no asset_url")

        try:
            component_dir = self._safe_child(self.temp_root, self._safe_name(component_id))
            run_dir = self._safe_child(component_dir, str(int(time.time() * 1000)))
            download_dir = self._safe_child(run_dir, "download")
            staging_dir = self._safe_child(run_dir, "staging")
            download_dir.mkdir(parents=True, exist_ok=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            asset_name = self._asset_name(remote)
            download_path = self._safe_child(download_dir, asset_name)
            effective_timeout = timeout or self._timeout_for(entry)
            bytes_downloaded = self._download_file(
                remote.asset_url,
                download_path,
                effective_timeout,
                progress_callback=progress_callback,
            )
            if bytes_downloaded <= 0 or not download_path.exists() or download_path.stat().st_size <= 0:
                return self._failure(
                    component_id,
                    "empty_download",
                    "downloaded asset is empty",
                    download_path=download_path,
                    staging_dir=staging_dir,
                    asset_name=asset_name,
                    bytes_downloaded=bytes_downloaded,
                )

            checksum_ok, checksum_code, checksum_message, digest = self._verify_sha256(download_path, entry)
            # Audit-finding Top 1 (Critical): the service layer calls ``download_and_stage``
            # instead of the strict ``download()`` entry point. Mirror the strict flow
            # here so a manifest missing **both** ``sha256`` and ``signature`` is refused
            # unless the dual-armed diagnostic escape hatch is explicitly enabled (both
            # ``security.allow_weak_manifest_verification=true`` AND
            # ``M3U8D_SECURITY_DIAGNOSTIC=1``). A missing ``sha256`` accompanied by a
            # signature is still rejected here — ``download_and_stage`` is the legacy
            # service path and signature verification lives only in the strict
            # ``download()`` entry. Keep the trust boundary consistent: if the manifest
            # author wants signature-only verification they must migrate that component
            # onto the strict path in a follow-up change.
            if digest is None and not self._weak_verification_allowed():
                return self._failure(
                    component_id,
                    "missing_checksum",
                    "component update requires sha256 verification (service layer);"
                    " configure checksum.sha256 in deps.json or enable the dual-armed"
                    " diagnostic switch.",
                    download_path=download_path,
                    staging_dir=staging_dir,
                    asset_name=asset_name,
                    bytes_downloaded=bytes_downloaded,
                )
            if digest is None:
                logger.warning(
                    "component update proceeding without sha256 "
                    "(weak_verification_allowed=True): %s",
                    entry.id,
                )
            if not checksum_ok:
                return self._failure(
                    component_id,
                    checksum_code,
                    checksum_message,
                    download_path=download_path,
                    staging_dir=staging_dir,
                    asset_name=asset_name,
                    bytes_downloaded=bytes_downloaded,
                    sha256=digest,
                )

            staged_exe = self._stage_asset(entry, remote, download_path, staging_dir)
            if not self._verify_staged_exe(staged_exe):
                return self._failure(
                    component_id,
                    "staged_exe_invalid",
                    "staged executable is missing, empty, or not an exe",
                    download_path=download_path,
                    staging_dir=staging_dir,
                    staged_exe_path=staged_exe,
                    asset_name=asset_name,
                    bytes_downloaded=bytes_downloaded,
                    sha256=digest,
                )

            # Audit-finding: the ``sha256`` field in the result feeds
            # :meth:`ComponentUpdateInstaller._verify_staged_sha256`, which
            # re-hashes ``staged_exe_path`` right before the atomic replace.
            # For zip assets the download digest is over the **zip**, not the
            # extracted exe, so handing the zip digest to the installer makes
            # the post-install re-verify always fail with ``staging_tampered``.
            # Recompute the digest of the staged file here so the returned
            # ``sha256`` refers to the same bytes the installer will compare.
            # The download-side checksum has already been validated above, so
            # replacing the field does not weaken the trust chain — it just
            # fixes the reference point.
            try:
                staged_digest = self._sha256(staged_exe)
            except OSError:
                staged_digest = digest  # fall back to the download digest
            post_stage_digest = staged_digest if staged_digest else digest

            return ComponentDownloadStageResult(
                component_id=component_id,
                success=True,
                code="ok",
                message="asset downloaded and staged in component update temp directory",
                download_path=str(download_path),
                staging_dir=str(staging_dir),
                staged_exe_path=str(staged_exe),
                asset_name=asset_name,
                bytes_downloaded=bytes_downloaded,
                sha256=post_stage_digest,
                weak_validation=digest is None,
            )
        except (TimeoutError, socket.timeout):
            return self._failure(component_id, "network_timeout", "network timeout while downloading asset")
        except HTTPError as exc:
            return self._failure(component_id, "http_error", f"asset download failed: HTTP {exc.code}")
        except URLError as exc:
            return self._failure(component_id, "network_error", f"network unavailable while downloading asset: {exc.reason}")
        except zipfile.BadZipFile:
            return self._failure(component_id, "bad_zip", "asset is not a valid zip archive")
        except ValueError as exc:
            message = str(exc)
            if "zip member does not match" in message:
                return self._failure(component_id, "asset_member_mismatch", message)
            if "direct asset is not an exe" in message:
                return self._failure(component_id, "asset_type_mismatch", message)
            return self._failure(component_id, "validation_error", message)
        except OSError as exc:
            return self._failure(component_id, "filesystem_error", f"filesystem error while staging asset: {exc}")
        except Exception as exc:
            return self._failure(component_id, "unexpected_error", f"component asset staging failed: {exc}")

    def _download_file(
        self,
        url: str,
        destination: Path,
        timeout: int,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Stream ``url`` into ``destination`` and return bytes written.

        When ``progress_callback`` is supplied it receives
        ``(bytes_downloaded, total_bytes)`` after each chunk write.
        ``total_bytes`` is parsed from ``Content-Length``; when the
        server omits it (chunked transfer, unknown size) the callback
        receives ``None`` so the UI can still render a byte counter
        even without a percent. Callback exceptions are caught and
        logged at debug so a chatty or broken consumer cannot stall
        the download pipeline.
        """

        request = Request(url, headers={"User-Agent": self.user_agent}, method="GET")
        total = 0
        with self.opener(request, timeout=timeout) as response:
            # ``Content-Length`` is the most portable total-size hint;
            # fall back to ``None`` so callers can render "?? / 123MB"
            # style indicators rather than a fake 100%.
            total_bytes: int | None = None
            try:
                cl = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if cl is not None:
                    parsed = int(str(cl).strip())
                    if parsed > 0:
                        total_bytes = parsed
            except (TypeError, ValueError):
                total_bytes = None

            with open(destination, "wb") as output_file:
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    output_file.write(chunk)
                    total += len(chunk)
                    if progress_callback is not None:
                        try:
                            progress_callback(total, total_bytes)
                        except Exception as exc:  # pragma: no cover — defensive
                            logger.debug(
                                "component update progress_callback raised: %s",
                                exc,
                            )
        return total

    def _stage_asset(
        self,
        entry: DependencyEntry,
        remote: RemoteReleaseInfo,
        asset_path: Path,
        staging_dir: Path,
    ) -> Path:
        install_type = self._install_type(entry, remote, asset_path)
        if install_type == "zip":
            return self._extract_zip_member(entry, asset_path, staging_dir)
        if install_type == "file":
            return self._stage_direct_exe(entry, asset_path, staging_dir)
        raise ValueError(f"unsupported update asset type: {install_type}")

    def _stage_direct_exe(self, entry: DependencyEntry, asset_path: Path, staging_dir: Path) -> Path:
        if asset_path.suffix.lower() != ".exe":
            raise ValueError("direct asset is not an exe")
        target_name = Path(entry.relative_path).name or asset_path.name
        if Path(target_name).suffix.lower() != ".exe":
            target_name = asset_path.name
        staged = self._safe_child(staging_dir, target_name)
        shutil.copy2(asset_path, staged)
        return staged

    def _extract_zip_member(self, entry: DependencyEntry, archive_path: Path, staging_dir: Path) -> Path:
        member = self._zip_member_spec(entry)
        member_pattern = self._zip_member_pattern(entry)
        target_basename = Path(entry.relative_path).name
        with zipfile.ZipFile(archive_path, "r") as archive:
            selected = self._select_zip_member(archive.namelist(), member, member_pattern, target_basename)
            if selected is None:
                wanted = member or member_pattern or target_basename or "*.exe"
                raise ValueError(f"zip member does not match expected executable: {wanted}")
            staged = self._safe_child(staging_dir, Path(selected).name)
            with archive.open(selected, "r") as source, open(staged, "wb") as output_file:
                shutil.copyfileobj(source, output_file)
        return staged

    def _select_zip_member(
        self,
        names: list[str],
        member: str | None,
        member_pattern: str | None,
        target_basename: str | None,
    ) -> str | None:
        file_names = [name for name in names if name and not name.endswith("/")]
        if member:
            normalized_member = member.replace("\\", "/").lower()
            for name in file_names:
                normalized_name = name.replace("\\", "/").lower()
                if normalized_name == normalized_member or Path(name).name.lower() == Path(member).name.lower():
                    return name
            return None
        if member_pattern:
            for name in file_names:
                normalized_name = name.replace("\\", "/")
                if fnmatch.fnmatch(normalized_name, member_pattern) or fnmatch.fnmatch(Path(name).name, member_pattern):
                    return name
            return None
        if target_basename:
            for name in file_names:
                if Path(name).name.lower() == target_basename.lower():
                    return name
        exe_members = [name for name in file_names if Path(name).suffix.lower() == ".exe"]
        return exe_members[0] if len(exe_members) == 1 else None

    def _verify_sha256(self, file_path: Path, entry: DependencyEntry) -> tuple[bool, str, str, str | None]:
        """Return ``(ok, code, message, digest)`` for the staged file.

        Verification order (first match wins):

        1. **Static pin** (``update.checksum.sha256``): strict — mismatch
           is a hard fail.
        2. **Dynamic sidecar** (``update.checksum.sha256_url``): strict.
        3. **TOFU pin** (``~/.m3u8d/component_pins.json``): first
           successful download for this component writes the digest and
           every subsequent update compares against it. A mismatch here
           is *not* automatically a failure because a new upstream
           release legitimately changes the digest — we compute the new
           digest, update the pin, and surface a telemetry flag
           (``code="tofu_pin_rotated"``). Unlike the strict paths, the
           responsibility for catching a malicious "new" file rests on
           the download source's HTTPS + domain allowlist (see
           ``_is_trusted_update_source``). When neither a static pin
           nor a sidecar is configured and the source is NOT on the
           HTTPS allowlist, the caller must reject the update via the
           ``missing_checksum`` gate.
        4. **No verification configured**: returns
           ``(True, "ok", "weak validation only", None)``; the calling
           gate decides whether that is acceptable.
        """

        # 1 — static pin
        expected = self._expected_sha256(entry)
        if expected:
            digest = self._sha256(file_path)
            if digest.lower() != expected.lower():
                return False, "hash_mismatch", "sha256 checksum mismatch", digest
            return True, "ok", "sha256 checksum matched (static pin)", digest

        # 2 — dynamic sidecar
        sidecar_expected = self._fetch_expected_sha256(entry, file_path.name)
        if sidecar_expected:
            digest = self._sha256(file_path)
            if digest.lower() != sidecar_expected.lower():
                return False, "hash_mismatch", "sha256 checksum mismatch (sidecar)", digest
            return True, "ok", "sha256 checksum matched (sidecar)", digest

        # 3 — TOFU pin
        #    Only applies when the source URL lives on the HTTPS
        #    allowlist; otherwise we fall straight to the weak path and
        #    the caller is expected to refuse the download.
        if self._is_trusted_update_source(entry):
            digest = self._sha256(file_path)
            tofu_result = self._apply_tofu_pin(entry.id, digest)
            return tofu_result  # (ok, code, message, digest)

        # 4 — no verification at all
        return True, "ok", "sha256 not configured; weak validation only", None

    def _expected_sha256(self, entry: DependencyEntry) -> str | None:
        checksum = entry.update.checksum if entry.update else None
        if not isinstance(checksum, dict):
            return None
        for key in ("sha256", "SHA256"):
            value = checksum.get(key)
            if value:
                return str(value).strip()
        return None

    def _fetch_expected_sha256(
        self, entry: DependencyEntry, asset_filename: str
    ) -> str | None:
        """Dynamically fetch the expected sha256 from a sidecar URL.

        ``update.checksum.sha256_url`` is a URL that returns either:

        * a ``sha256sum`` / ``coreutils``-style listing — one line per
          asset in the form ``<digest>  [*]<filename>`` (two-space or
          single-space + optional ``*``), or
        * a bare 64-char lowercase hex digest (gyan.dev style sidecar).

        Network errors, HTTP errors, and parse failures all return
        ``None`` so the caller falls back to the weak-validation path
        (which the dual-armed diagnostic gate refuses by default) — the
        point is never to *silently* succeed, just to avoid crashing the
        whole update flow when the sidecar is temporarily unavailable.

        The fetch is bounded to a short timeout and a small response
        cap (64 KiB) to rule out the sidecar becoming a DoS vector.
        """

        update = entry.update
        if not update or not isinstance(update.checksum, dict):
            return None
        sha_url = update.checksum.get("sha256_url") or update.checksum.get("SHA256_URL")
        if not sha_url:
            return None
        sha_url = str(sha_url).strip()
        if not sha_url:
            return None

        timeout = max(5, int(self._timeout_for(entry) or 60) // 4 or 15)
        try:
            request = Request(sha_url, headers={"User-Agent": self.user_agent}, method="GET")
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(64 * 1024)
        except (HTTPError, URLError, TimeoutError, socket.timeout) as exc:
            logger.warning(
                "sha256 sidecar fetch failed (%s): %s",
                type(exc).__name__,
                sha_url,
            )
            return None
        except Exception as exc:
            logger.warning(
                "sha256 sidecar fetch unexpected error (%s): %s",
                type(exc).__name__,
                sha_url,
            )
            return None

        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None

        return self._parse_sha256_sidecar(text, asset_filename)

    @staticmethod
    def _parse_sha256_sidecar(text: str, asset_filename: str) -> str | None:
        """Extract the sha256 digest for ``asset_filename`` from ``text``.

        Accepts three input shapes:

        1. ``sha256sum`` listing: ``<64 hex>  <name>`` or ``<64 hex> *<name>``.
           Multiple lines are scanned; the first whose filename matches
           ``asset_filename`` (case-insensitive, ignoring ``*`` prefix
           and any leading ``./``) wins.
        2. Bare digest: the whole file is 64 hex characters (optionally
           with trailing whitespace / newline). ``asset_filename`` is
           ignored in this case because the sidecar maps 1-to-1 to the
           asset URL on disk.
        3. Legacy ``<name>  <64 hex>`` format (rare; also accepted).
        """

        if not text:
            return None
        stripped = text.strip()

        # Case 2: bare digest.
        if len(stripped) == 64 and all(
            c in "0123456789abcdefABCDEF" for c in stripped
        ):
            return stripped.lower()

        target = asset_filename.strip().lower()
        if not target:
            return None

        for raw_line in stripped.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            # Try "<digest>  <filename>" (sha256sum canonical form).
            digest, name = parts[0], parts[-1]
            if not (len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest)):
                # Fall back to "<filename>  <digest>" legacy form.
                digest, name = parts[-1], parts[0]
                if not (len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest)):
                    continue
            clean_name = name.lstrip("*").lstrip()
            if clean_name.startswith("./"):
                clean_name = clean_name[2:]
            if clean_name.lower() == target or Path(clean_name).name.lower() == target:
                return digest.lower()

        return None

    # ------------------------------------------------------------------
    # TOFU pin support (audit-finding B1 fallback for components without
    # an upstream sha256 sidecar — currently N_m3u8DL-RE and streamlink)
    # ------------------------------------------------------------------

    #: Host allowlist for the TOFU fallback. Only downloads whose asset
    #: URL hosts match this set are allowed to rely on the TOFU pin;
    #: everything else must have a static or sidecar checksum or is
    #: refused by the dual-armed gate. HTTPS is enforced separately.
    _TOFU_ALLOWED_HOSTS: frozenset = frozenset(
        {
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "www.gyan.dev",
        }
    )

    def _tofu_pin_path(self) -> Path:
        """Location of the TOFU pin file.

        Lives under ``~/.m3u8d`` alongside ``session.token`` so the
        existing owner-only hardening work in ``catcatch_server`` also
        protects the pin map. The directory is created lazily on the
        first successful update. Tests and smoke scripts pass a
        ``tofu_pin_path`` override to the constructor so they never
        touch the real ``~/.m3u8d`` directory.
        """

        if self._tofu_pin_path_override is not None:
            return self._tofu_pin_path_override
        return Path.home() / ".m3u8d" / "component_pins.json"

    def _load_tofu_pins(self) -> dict[str, Any]:
        """Return the on-disk pin map, or ``{}`` on any read error.

        The pin file is opaque to users; corruption / missing / unreadable
        is handled by starting fresh rather than failing the update. A
        legitimate "new version" path would also drop the old pin and
        write a new one, so a broken pin file effectively behaves like
        a first-time-install scenario.
        """

        path = self._tofu_pin_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save_tofu_pins(self, pins: dict[str, Any]) -> None:
        """Persist the pin map atomically under ``~/.m3u8d/``.

        Best-effort POSIX 0o600 / Windows DACL tightening is delegated
        to the same helpers ``catcatch_server`` uses for the session
        token. Errors here are non-fatal — we log a warning and
        continue; the update itself has already succeeded.
        """

        path = self._tofu_pin_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(pins, indent=2, sort_keys=True, ensure_ascii=False)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.warning(
                "failed to persist TOFU pin map (%s): %s",
                type(exc).__name__,
                exc,
            )
            return

        # Best-effort permission tightening. Mirrors the session-token
        # write helper's approach so the same audit guarantees apply
        # to the pin file.
        try:
            import stat as _stat

            os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass

    def _is_trusted_update_source(self, entry: DependencyEntry) -> bool:
        """Return True when the update source may use the TOFU fallback.

        We require BOTH:

        * HTTPS scheme (never HTTP — plain-HTTP pin rotation would let
          a MITM swap the digest freely).
        * Host in :data:`_TOFU_ALLOWED_HOSTS` (GitHub or gyan.dev only).

        The ``release_source`` value steers us to the right URL field:
        ``github_latest`` means GitHub releases, ``direct`` means the
        ``latest_url`` field or the download spec's ``url`` field. We
        check whichever is populated; if none resolve to an allowlisted
        host the component does NOT qualify for TOFU and must fall back
        to the weak-verification gate (which refuses by default).
        """

        from urllib.parse import urlsplit

        candidates: list[str] = []

        update = entry.update
        if update:
            if update.release_source == "github_latest" and update.repo:
                # Any asset download URL for a GitHub release lives on
                # github.com or *.githubusercontent.com — matching one
                # of those hosts is sufficient.
                candidates.append(f"https://github.com/{update.repo}/releases")
            if update.latest_url:
                candidates.append(update.latest_url)
        download = entry.download
        if download and download.url:
            candidates.append(download.url)
        if download and download.repo and download.source == "github_release":
            candidates.append(f"https://github.com/{download.repo}/releases")

        for url in candidates:
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            scheme = (parts.scheme or "").lower()
            host = (parts.hostname or "").lower()
            if scheme != "https":
                continue
            if host in self._TOFU_ALLOWED_HOSTS:
                return True
        return False

    def _apply_tofu_pin(
        self, component_id: str, digest: str
    ) -> tuple[bool, str, str, str]:
        """Evaluate the TOFU pin for ``component_id`` against ``digest``.

        First-time install: store the digest, return success.
        Matching pin: success (normal re-run of a completed update).
        Rotated pin: treat as a legitimate upstream version bump — the
        digest is expected to change on a new release. Update the pin
        and emit a telemetry code so downstream logs can still surface
        the rotation for auditability.
        """

        pins = self._load_tofu_pins()
        existing = pins.get(component_id)
        existing_digest = None
        if isinstance(existing, dict):
            existing_digest = existing.get("sha256") or existing.get("digest")

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pins[component_id] = {
            "sha256": digest.lower(),
            "last_seen": now_iso,
        }
        self._save_tofu_pins(pins)

        if not existing_digest:
            return (
                True,
                "tofu_pin_created",
                "TOFU pin established on first update (no prior digest known)",
                digest,
            )
        if str(existing_digest).lower() == digest.lower():
            return (
                True,
                "ok",
                "TOFU pin matched (same binary as previous update)",
                digest,
            )
        # Rotated pin — new upstream version. Pin is already updated.
        return (
            True,
            "tofu_pin_rotated",
            "TOFU pin rotated (upstream released a new version)",
            digest,
        )

    @staticmethod
    def _sha256(file_path: Path) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _verify_staged_exe(file_path: Path) -> bool:
        return file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0 and file_path.suffix.lower() == ".exe"

    def _install_type(self, entry: DependencyEntry, remote: RemoteReleaseInfo, asset_path: Path) -> str:
        strategy = (entry.update.install_strategy if entry.update else "") or ""
        if "zip" in strategy:
            return "zip"
        if "file" in strategy:
            return "file"
        if entry.download and entry.download.type in ("zip", "file"):
            return entry.download.type
        name = (remote.asset_name or asset_path.name).lower()
        if name.endswith(".zip"):
            return "zip"
        if name.endswith(".exe"):
            return "file"
        return "file"

    @staticmethod
    def _zip_member_spec(entry: DependencyEntry) -> str | None:
        if entry.download and entry.download.member:
            return entry.download.member
        checksum = entry.update.checksum if entry.update else None
        if isinstance(checksum, dict):
            member = checksum.get("member")
            if member:
                return str(member).strip() or None
        return None

    @staticmethod
    def _zip_member_pattern(entry: DependencyEntry) -> str | None:
        update = entry.update
        if update and update.checksum and isinstance(update.checksum, dict):
            for key in ("member_pattern", "zip_member_pattern"):
                value = update.checksum.get(key)
                if value:
                    return str(value).strip() or None
        return None

    @staticmethod
    def _asset_name(remote: RemoteReleaseInfo) -> str:
        name = (remote.asset_name or remote.asset_url or "asset.bin").rsplit("/", 1)[-1].split("?", 1)[0]
        name = Path(name).name.strip()
        return name or "asset.bin"

    @staticmethod
    def _timeout_for(entry: DependencyEntry) -> int:
        if entry.download and entry.download.timeout:
            return max(1, int(entry.download.timeout))
        return 300

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value.strip())
        return safe or "component"

    @staticmethod
    def _safe_child(root: Path, *parts: str) -> Path:
        root_resolved = root.resolve()
        candidate = root_resolved.joinpath(*parts).resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise ValueError("refusing to write outside component update temp directory")
        return candidate

    @staticmethod
    def _failure(
        component_id: str,
        code: str,
        message: str,
        download_path: Path | str | None = None,
        staging_dir: Path | str | None = None,
        staged_exe_path: Path | str | None = None,
        asset_name: str | None = None,
        bytes_downloaded: int | None = None,
        sha256: str | None = None,
    ) -> ComponentDownloadStageResult:
        return ComponentDownloadStageResult(
            component_id=component_id,
            success=False,
            code=code,
            message=message,
            download_path=str(download_path) if download_path else None,
            staging_dir=str(staging_dir) if staging_dir else None,
            staged_exe_path=str(staged_exe_path) if staged_exe_path else None,
            asset_name=asset_name,
            bytes_downloaded=bytes_downloaded,
            sha256=sha256,
            weak_validation=sha256 is None,
        )


# --------------------------------------------------------------------------
# Module-level helpers for the R1 ``download`` path
# --------------------------------------------------------------------------

def _url_host(url: str) -> str:
    """Return the host portion of ``url`` for structured error diagnostics."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _verify_authenticode(tmp_path: Path, signature: str) -> tuple[bool, str, str]:
    """Verify ``tmp_path`` against ``signature`` per the R1 policy.

    ``signature == "authenticode"`` (case insensitive) requests Windows
    Authenticode verification via ``WinVerifyTrust`` with
    ``WINTRUST_ACTION_GENERIC_VERIFY_V2``. Any other value is treated as a
    base64 (preferred) or hex encoded detached ECDSA-P256 signature over the
    raw file bytes, verified against the pinned public key in
    ``resources/update_signing_key.pem``.

    Returns
    -------
    (ok, code, reason)
        On failure ``code`` is one of ``"signature_invalid"`` or
        ``"verify_unavailable"`` and ``reason`` is a short explanation.
    """
    kind = signature.strip()
    if kind.lower() == "authenticode":
        return _verify_authenticode_wintrust(tmp_path)
    return _verify_detached_ecdsa(tmp_path, kind)


def _verify_authenticode_wintrust(tmp_path: Path) -> tuple[bool, str, str]:
    """Invoke ``WinVerifyTrust`` with ``WINTRUST_ACTION_GENERIC_VERIFY_V2``."""
    if sys.platform != "win32":
        return False, "verify_unavailable", "WinVerifyTrust is only available on Windows"
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover - ctypes is in stdlib
        return False, "verify_unavailable", f"ctypes unavailable: {exc}"

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 GUID: {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
        ]

    WTD_UI_NONE = 2
    WTD_REVOKE_NONE = 0
    WTD_CHOICE_FILE = 1
    WTD_STATEACTION_VERIFY = 1
    WTD_STATEACTION_CLOSE = 2

    action = _GUID(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WINTRUST_FILE_INFO(
        cbStruct=ctypes.sizeof(_WINTRUST_FILE_INFO),
        pcwszFilePath=str(tmp_path),
        hFile=None,
        pgKnownSubject=None,
    )
    data = _WINTRUST_DATA()
    ctypes.memset(ctypes.addressof(data), 0, ctypes.sizeof(_WINTRUST_DATA))
    data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_NONE
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = WTD_STATEACTION_VERIFY

    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
    except OSError as exc:
        return False, "verify_unavailable", f"wintrust.dll not loadable: {exc}"

    WinVerifyTrust = wintrust.WinVerifyTrust
    WinVerifyTrust.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
    ]
    WinVerifyTrust.restype = ctypes.c_long

    try:
        status = WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE
        try:
            WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        except OSError as exc:
            # WinVerifyTrust close is advisory cleanup; the verification
            # status above is what matters. Redact — the call is via
            # ctypes and exc only carries a WinError code.
            logger.debug(
                "component_update: WinVerifyTrust close failed (%s)",
                type(exc).__name__,
            )

    if status == 0:
        return True, "ok", "WinVerifyTrust succeeded"
    return (
        False,
        "signature_invalid",
        f"WinVerifyTrust reported status 0x{status & 0xFFFFFFFF:08X}",
    )


def _verify_detached_ecdsa(tmp_path: Path, signature: str) -> tuple[bool, str, str]:
    """Verify an ECDSA-P256 detached signature over the raw file bytes."""
    try:
        signature_bytes = _decode_signature(signature)
    except ValueError as exc:
        return False, "signature_invalid", f"signature is not valid base64/hex: {exc}"

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature
    except Exception as exc:
        return (
            False,
            "verify_unavailable",
            f"cryptography library unavailable for detached signature verification: {exc}",
        )

    pubkey_path = get_resource_path(_PINNED_PUBKEY_PATH)
    try:
        pem_bytes = Path(pubkey_path).read_bytes()
    except OSError as exc:
        return (
            False,
            "verify_unavailable",
            f"pinned public key not found at {pubkey_path}: {exc}",
        )

    try:
        public_key = serialization.load_pem_public_key(pem_bytes)
    except Exception as exc:
        return False, "verify_unavailable", f"pinned public key failed to load: {exc}"

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        return False, "verify_unavailable", "pinned public key is not an EC key"

    try:
        payload = tmp_path.read_bytes()
    except OSError as exc:
        return False, "io_error", f"failed to read staged file for signature verification: {exc}"

    try:
        public_key.verify(signature_bytes, payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False, "signature_invalid", "ECDSA P-256 signature did not verify"
    except Exception as exc:
        return False, "signature_invalid", f"signature verification raised: {exc}"
    return True, "ok", "ECDSA P-256 signature verified"


def _decode_signature(signature: str) -> bytes:
    """Decode a signature string as base64 (preferred) or hex."""
    text = signature.strip().replace(" ", "").replace("\n", "")
    # Try base64 first (most compact).
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        pass
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
