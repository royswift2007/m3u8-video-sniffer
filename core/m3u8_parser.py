"""
M3U8 Parser Utility
Handles fetching and parsing of M3U8 master playlists to extract quality variants.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from utils.config_manager import config
from utils.logger import logger


class M3U8FetchThread(QThread):
    """Background thread to fetch and parse M3U8 playlist."""

    finished = pyqtSignal(list)

    def __init__(self, url: str, headers: dict = None):
        super().__init__()
        self.url = url
        self.headers = headers or {}
        self._last_response_info = {}
        feature_flags = config.get("features", {}) or {}
        self._max_nested_depth = max(1, min(5, int(feature_flags.get("m3u8_nested_depth", 3))))
        self._verify_tls = bool(feature_flags.get("network_verify_tls", True))
        self._tls_warning_emitted = False

    def run(self):
        try:
            logger.info(f"Analyzing M3U8 playlist: {self.url}")

            headers = self.headers.copy()
            if "User-Agent" not in headers and "user-agent" not in headers:
                headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )

            content = self._fetch_with_retry(self.url, headers)
            logger.info(f"M3U8 Content Sample (First 500 chars):\n{content[:500]}")

            # Some sites return a plain URL body as pseudo redirect.
            if content.strip().startswith("http") and "#EXTM3U" not in content:
                redirect_url = content.strip()
                logger.info(f"Detected URL in response body, following pseudo-redirect to: {redirect_url}")
                try:
                    content = self._fetch_once(redirect_url, headers)
                    self.url = redirect_url
                    logger.info(f"New M3U8 Content Sample (First 500 chars):\n{content[:500]}")
                except Exception as e:
                    logger.warning(
                        f"Failed to follow pseudo-redirect: {e}",
                        event="m3u8_pseudo_redirect_failed",
                        stage="fetch_redirect",
                        error_type=type(e).__name__,
                        url=redirect_url,
                    )

            if not self._is_master_playlist(content):
                logger.info("Detected media playlist (no variants in master playlist)")
                self.finished.emit([])
                return

            variants = self._parse_m3u8_variants(content, self.url)
            if variants:
                variants = self._resolve_nested_variants(variants, headers, depth=0, visited={self.url})
                logger.info(f"Found {len(variants)} variants in M3U8")
            else:
                logger.info("No variants found in M3U8 (master playlist empty)")

            self.finished.emit(variants)

        except Exception as e:
            if self._last_response_info:
                logger.error(
                    f"Failed to parse M3U8: {e} | "
                    f"status={self._last_response_info.get('status_code')} "
                    f"url={self._last_response_info.get('url')}",
                    event="m3u8_parse_failed",
                    stage="run",
                    error_type=type(e).__name__,
                )
            else:
                logger.error(
                    f"Failed to parse M3U8: {e}",
                    event="m3u8_parse_failed",
                    stage="run",
                    error_type=type(e).__name__,
                )
            self.finished.emit([])

    def _fetch_once(self, url: str, headers: dict) -> str:
        if not self._verify_tls and not self._tls_warning_emitted:
            logger.warning("[M3U8] TLS verification disabled by config")
            self._tls_warning_emitted = True
        response = requests.get(url, headers=headers, timeout=10, verify=self._verify_tls)
        response.raise_for_status()
        self._last_response_info = {
            "status_code": getattr(response, "status_code", None),
            "url": getattr(response, "url", url),
            "headers": dict(getattr(response, "headers", {}) or {}),
        }
        return response.text

    def _fetch_with_retry(self, url: str, headers: dict) -> str:
        try:
            return self._fetch_once(url, headers)
        except Exception as e:
            logger.warning(
                f"M3U8 initial fetch failed, retry with referer/origin: {e}",
                event="m3u8_fetch_retry",
                stage="fetch_playlist",
                error_type=type(e).__name__,
                url=url,
            )
            retry_headers = headers.copy()
            if "Referer" not in retry_headers and "referer" not in retry_headers:
                retry_headers["Referer"] = url
            if "Origin" not in retry_headers and "origin" not in retry_headers:
                try:
                    parsed = urlparse(url)
                    if parsed.scheme and parsed.netloc:
                        retry_headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
                except Exception as parse_err:
                    logger.debug(
                        f"[M3U8] origin parse failed: {parse_err}",
                        event="m3u8_origin_parse_failed",
                        stage="fetch_playlist",
                        error_type=type(parse_err).__name__,
                    )
            return self._fetch_once(url, retry_headers)

    @staticmethod
    def _is_master_playlist(content: str) -> bool:
        return "#EXT-X-STREAM-INF" in content

    def _resolve_nested_variants(self, variants: list, headers: dict, depth: int = 0, visited: set | None = None) -> list:
        """Resolve nested master playlists with depth limit and loop detection."""
        visited = visited or set()
        if depth >= self._max_nested_depth:
            logger.warning(
                "[M3U8] nested depth limit reached",
                event="m3u8_nested_depth_limit",
                stage="parse_nested",
                depth=depth,
                max_depth=self._max_nested_depth,
            )
            return variants

        resolved = []
        for variant in variants:
            variant_url = (variant.get("url") or "").strip()
            if ".m3u8" not in variant_url.lower():
                resolved.append(variant)
                continue

            if variant_url in visited:
                logger.warning(
                    f"[M3U8] nested loop detected: {variant_url}",
                    event="m3u8_nested_loop_detected",
                    stage="parse_nested",
                    depth=depth,
                )
                resolved.append(variant)
                continue

            try:
                content = self._fetch_once(variant_url, headers)
                if self._is_master_playlist(content):
                    nested = self._parse_m3u8_variants(content, variant_url)
                    if nested:
                        next_visited = set(visited)
                        next_visited.add(variant_url)
                        resolved.extend(
                            self._resolve_nested_variants(
                                nested,
                                headers,
                                depth=depth + 1,
                                visited=next_visited,
                            )
                        )
                    else:
                        resolved.append(variant)
                else:
                    resolved.append(variant)
            except Exception as e:
                logger.warning(
                    f"Nested m3u8 fetch failed: {variant_url} - {e}",
                    event="m3u8_nested_fetch_failed",
                    stage="parse_nested",
                    depth=depth,
                    error_type=type(e).__name__,
                    url=variant_url,
                )
                resolved.append(variant)

        resolved.sort(key=lambda x: x.get("height", 0), reverse=True)
        return resolved

    def _parse_m3u8_variants(self, content: str, base_url: str) -> list:
        """Parse master playlist variants."""
        variants = []
        pattern = re.compile(r"#EXT-X-STREAM-INF:([^\n]+)(?:\n#(?!EXT).*)*\n\s*([^\n#]+)", re.MULTILINE)
        matches = pattern.findall(content)
        logger.debug(f"Regex matches found: {len(matches)}")

        for info_str, url_line in matches:
            url_line = url_line.strip()
            if not url_line:
                continue

            variant_url = urljoin(base_url, url_line)

            bandwidth = 0
            resolution = None
            height = 0
            width = 0

            bw_match = re.search(r"BANDWIDTH=(\d+)", info_str)
            if bw_match:
                bandwidth = int(bw_match.group(1))

            res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", info_str)
            if res_match:
                width = int(res_match.group(1))
                height = int(res_match.group(2))
                resolution = f"{width}x{height}"

            variants.append(
                {
                    "format_id": f"{height}p" if height else "auto",
                    "url": variant_url,
                    "height": height,
                    "width": width,
                    "resolution": resolution,
                    "tbr": round(bandwidth / 1024) if bandwidth else 0,
                    "filesize_str": f"{round(bandwidth / 8 / 1024 / 1024, 2)}MB/min" if bandwidth else "N/A",
                    "ext": "m3u8",
                    "vcodec": "H.264",
                    "fps": 30,
                }
            )

        variants.sort(key=lambda x: x["height"], reverse=True)
        return variants
