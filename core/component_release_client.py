"""
Read-only remote release querying for component update checks.

This module intentionally does not download assets. It only reads release
metadata, matches a configured asset, and returns structured results.
"""

from __future__ import annotations

import fnmatch
import json
import re
import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.component_update_models import RemoteReleaseInfo
from core.component_update_state import ComponentUpdateStateStore, utc_now_iso
from core.dependency_manifest import DependencyEntry


class ComponentReleaseClient:
    """Fetch remote component release metadata without downloading assets."""

    def __init__(
        self,
        state_store: ComponentUpdateStateStore,
        opener: Callable[..., Any] | None = None,
        user_agent: str = "M3U8D Component Updater/1.0",
    ):
        self.state_store = state_store
        self.opener = opener or urlopen
        self.user_agent = user_agent

    def fetch_latest(self, entry: DependencyEntry, force: bool = False) -> RemoteReleaseInfo:
        """Fetch latest release metadata based on manifest update configuration."""
        if entry.update is None or not entry.update.enabled:
            return RemoteReleaseInfo(component_id=entry.id, error="component update is not configured")

        source = (entry.update.release_source or "").lower()
        if source == "github_latest":
            return self._fetch_github_latest(entry, force=force)
        if source == "direct":
            return self._fetch_direct_latest(entry)
        return RemoteReleaseInfo(component_id=entry.id, error=f"unsupported release source: {source or 'unknown'}")

    def _fetch_github_latest(self, entry: DependencyEntry, force: bool) -> RemoteReleaseInfo:
        """Fetch GitHub latest release JSON and match the configured asset."""
        update = entry.update
        if update is None or not update.repo:
            return RemoteReleaseInfo(component_id=entry.id, error="github repo is not configured")

        url = update.latest_url or f"https://api.github.com/repos/{update.repo}/releases/latest"
        etag = None if force else self.state_store.get_etag(entry.id, url)
        request = self._build_request(url, etag=etag)
        timeout = self._timeout_for(entry)

        try:
            payload, headers = self._open_json(request, timeout=timeout)
        except HTTPError as exc:
            headers = self._headers_to_dict(exc.headers)
            if exc.code == 304:
                return self._remote_from_cached(entry, url, etag, headers)
            if self._is_rate_limited(exc.code, headers):
                remaining = self._parse_int(headers.get("X-RateLimit-Remaining"))
                reset_at = headers.get("X-RateLimit-Reset")
                error = "github rate limit exceeded"
                if reset_at:
                    error = f"{error}; reset={reset_at}"
                self._record_remote_error(entry.id, error, remaining)
                return RemoteReleaseInfo(
                    component_id=entry.id,
                    rate_limit_remaining=remaining,
                    error=error,
                )
            error = f"github release request failed: HTTP {exc.code}"
            self._record_remote_error(entry.id, error, self._parse_int(headers.get("X-RateLimit-Remaining")))
            return RemoteReleaseInfo(
                component_id=entry.id,
                rate_limit_remaining=self._parse_int(headers.get("X-RateLimit-Remaining")),
                error=error,
            )
        except (TimeoutError, socket.timeout):
            error = "network timeout while querying remote release"
            self._record_remote_error(entry.id, error, None)
            return RemoteReleaseInfo(component_id=entry.id, error=error)
        except URLError as exc:
            error = f"network unavailable while querying remote release: {exc.reason}"
            self._record_remote_error(entry.id, error, None)
            return RemoteReleaseInfo(component_id=entry.id, error=error)
        except OSError as exc:
            error = f"network error while querying remote release: {exc}"
            self._record_remote_error(entry.id, error, None)
            return RemoteReleaseInfo(component_id=entry.id, error=error)
        except Exception as exc:
            error = f"remote release query failed: {exc}"
            self._record_remote_error(entry.id, error, None)
            return RemoteReleaseInfo(component_id=entry.id, error=error)

        return self._remote_from_payload(entry, url, payload, headers)

    def _fetch_direct_latest(self, entry: DependencyEntry) -> RemoteReleaseInfo:
        """Return metadata for direct URLs.

        When ``update.version_url`` is configured, it is fetched and its
        plain-text body (run through ``version_regex`` if set) becomes
        the remote version — this is the only way ``release_source=direct``
        components like gyan.dev FFmpeg can report a real version number,
        because their ``latest_url`` is the download asset itself and
        carries no version in the path. On fetch / parse failure we fall
        back to the pre-existing "extract from latest_url text" path so a
        transient version-endpoint outage doesn't block an update check.
        """

        update = entry.update
        if update is None or not update.latest_url:
            return RemoteReleaseInfo(component_id=entry.id, error="direct latest_url is not configured")

        version: str | None = None
        version_error: str | None = None

        if update.version_url:
            version, version_error = self._fetch_direct_version_from_url(entry, update.version_url)

        if not version:
            version = self._extract_version_from_text(update.latest_url, update.version_regex)

        asset_name = update.asset_pattern or update.latest_url.rsplit("/", 1)[-1] or None
        if version:
            error = None
        elif version_error:
            error = version_error
        else:
            error = "remote version is unknown for direct source"
        return RemoteReleaseInfo(
            component_id=entry.id,
            latest_version=version,
            release_url=update.latest_url,
            asset_name=asset_name,
            asset_url=update.latest_url,
            error=error,
        )

    def _fetch_direct_version_from_url(
        self, entry: DependencyEntry, version_url: str
    ) -> tuple[str | None, str | None]:
        """Fetch ``version_url`` and return ``(version, error_or_none)``.

        Enforces the same conservative limits we apply to sha256
        sidecars elsewhere: short timeout, small response cap, graceful
        fallback on any transport / decode error. The response body is
        passed through ``update.version_regex`` if configured; otherwise
        its trimmed first line becomes the version string.
        """

        update = entry.update
        timeout = 15
        try:
            request = self._build_request(version_url)
            # Use a plain ``urlopen``-shaped call so callers with a
            # custom ``opener`` keep working the same way as for the
            # GitHub JSON path. Reuse ``_open_json`` machinery by doing
            # a minimal bespoke open here — we only need the bytes.
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(8 * 1024)
        except (HTTPError, URLError, TimeoutError) as exc:
            return None, f"version_url fetch failed: {type(exc).__name__}"
        except Exception as exc:  # pragma: no cover — defensive
            return None, f"version_url unexpected error: {type(exc).__name__}"

        try:
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return None, "version_url decode failed"

        # Trim to first non-empty line — gyan.dev's ``release-version``
        # is exactly one line but other endpoints may add trailing
        # whitespace or comments.
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not first_line:
            return None, "version_url returned empty body"

        version = self._extract_version_from_text(first_line, update.version_regex if update else None)
        if version:
            return version, None
        return None, "version_url regex did not match"

    def _build_request(self, url: str, etag: str | None = None) -> Request:
        """Build an HTTP request with GitHub-friendly read-only headers."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag
        return Request(url, headers=headers, method="GET")

    def _open_json(self, request: Request, timeout: int) -> tuple[dict[str, Any], dict[str, str]]:
        """Open a JSON URL and return payload plus normalized headers."""
        with self.opener(request, timeout=timeout) as response:
            raw = response.read()
            headers = self._headers_to_dict(response.headers)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("release JSON root is not an object")
        return payload, headers

    def _remote_from_payload(
        self,
        entry: DependencyEntry,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> RemoteReleaseInfo:
        update = entry.update
        assets = payload.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        asset = self._match_asset(assets, update.asset_pattern if update else None)
        latest_version = self._parse_remote_version(payload, entry)
        etag = headers.get("ETag") or headers.get("etag")
        remaining = self._parse_int(headers.get("X-RateLimit-Remaining"))
        if etag:
            self.state_store.set_etag(entry.id, url, etag)
        release_url = self._optional_text(payload.get("html_url")) or url
        error = None
        if update and update.asset_pattern and asset is None:
            error = f"release asset does not match pattern: {update.asset_pattern}"
        info = RemoteReleaseInfo(
            component_id=entry.id,
            latest_version=latest_version,
            release_url=release_url,
            published_at=self._optional_text(payload.get("published_at")),
            asset_name=self._optional_text(asset.get("name")) if asset else None,
            asset_url=self._optional_text(asset.get("browser_download_url")) if asset else None,
            asset_size=self._parse_int(asset.get("size")) if asset else None,
            etag=etag,
            rate_limit_remaining=remaining,
            error=error,
        )
        self._record_remote_success(entry.id, url, payload, info)
        return info

    def _remote_from_cached(
        self,
        entry: DependencyEntry,
        url: str,
        etag: str | None,
        headers: dict[str, str],
    ) -> RemoteReleaseInfo:
        cached = self.state_store.get_component_state(entry.id).get("last_remote", {})
        if not isinstance(cached, dict):
            cached = {}
        remaining = self._parse_int(headers.get("X-RateLimit-Remaining"))
        if not cached:
            error = "remote release not modified but no cached payload is available"
            self._record_remote_error(entry.id, error, remaining)
            return RemoteReleaseInfo(component_id=entry.id, etag=etag, rate_limit_remaining=remaining, error=error)
        return RemoteReleaseInfo(
            component_id=entry.id,
            latest_version=self._optional_text(cached.get("latest_version")),
            release_url=self._optional_text(cached.get("release_url")) or url,
            published_at=self._optional_text(cached.get("published_at")),
            asset_name=self._optional_text(cached.get("asset_name")),
            asset_url=self._optional_text(cached.get("asset_url")),
            asset_size=self._parse_int(cached.get("asset_size")),
            etag=etag or self._optional_text(cached.get("etag")),
            rate_limit_remaining=remaining,
            error=None,
        )

    def _match_asset(self, assets: list[dict[str, Any]], asset_pattern: str | None) -> dict[str, Any] | None:
        """Return the first GitHub asset whose name matches the configured glob."""
        if not asset_pattern:
            return assets[0] if assets else None
        for asset in assets:
            name = self._optional_text(asset.get("name"))
            if name and fnmatch.fnmatch(name, asset_pattern):
                return asset
        return None

    def _parse_remote_version(self, payload: dict[str, Any], entry: DependencyEntry) -> str | None:
        """Extract and normalize the remote version from release metadata."""
        update = entry.update
        if update is None:
            return None
        source = update.version_source or "tag_name"
        if source == "tag_name":
            raw = self._optional_text(payload.get("tag_name"))
        elif source == "name":
            raw = self._optional_text(payload.get("name"))
        elif source == "body":
            raw = self._optional_text(payload.get("body"))
        elif source == "url":
            raw = self._optional_text(payload.get("html_url"))
        else:
            raw = self._optional_text(payload.get("tag_name"))
        return self._extract_version_from_text(raw, update.version_regex)

    def _extract_version_from_text(self, text: str | None, regex: str | None) -> str | None:
        if not text:
            return None
        if regex:
            match = re.search(regex, text, flags=re.MULTILINE)
            if not match:
                return None
            if "version" in match.groupdict():
                text = match.group("version")
            elif match.groups():
                text = match.group(1)
            else:
                text = match.group(0)
        return self.normalize_version(text)

    @staticmethod
    def normalize_version(version: str | None) -> str | None:
        """Normalize common release versions while preserving comparable content."""
        if version is None:
            return None
        normalized = version.strip()
        normalized = normalized.lstrip("vV")
        return normalized or None

    @staticmethod
    def _headers_to_dict(headers: Any) -> dict[str, str]:
        if headers is None:
            return {}
        if hasattr(headers, "items"):
            return {str(key): str(value) for key, value in headers.items()}
        return {}

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_rate_limited(status_code: int, headers: dict[str, str]) -> bool:
        remaining = headers.get("X-RateLimit-Remaining")
        return status_code in (403, 429) and remaining == "0"

    def _timeout_for(self, entry: DependencyEntry) -> int:
        if entry.download and entry.download.timeout:
            return max(1, int(entry.download.timeout))
        return 30

    def _record_remote_success(
        self,
        component_id: str,
        url: str,
        payload: dict[str, Any],
        info: RemoteReleaseInfo,
    ) -> None:
        self.state_store.update_component_state(
            component_id,
            {
                "last_remote_checked_at": utc_now_iso(),
                "last_remote_error": info.error,
                "last_remote": {
                    "latest_version": info.latest_version,
                    "release_url": info.release_url,
                    "published_at": info.published_at,
                    "asset_name": info.asset_name,
                    "asset_url": info.asset_url,
                    "asset_size": info.asset_size,
                    "etag": info.etag,
                    "source_url": url,
                    "tag_name": payload.get("tag_name"),
                },
            },
        )

    def _record_remote_error(self, component_id: str, error: str, remaining: int | None) -> None:
        self.state_store.update_component_state(
            component_id,
            {
                "last_remote_checked_at": utc_now_iso(),
                "last_remote_error": error,
                "rate_limit_remaining": remaining,
            },
        )
