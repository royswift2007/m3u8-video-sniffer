"""HLS preflight probe utilities."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import requests


class HLSProbe:
    """Lightweight HLS probe: playlist -> key -> first segment."""

    @classmethod
    def probe(cls, url: str, headers: dict | None = None, timeout: int = 8) -> dict:
        headers = (headers or {}).copy()
        if "User-Agent" not in headers and "user-agent" not in headers:
            headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        result = {
            "ok": False,
            "stage": "playlist",
            "playlist_url": url,
            "key_url": "",
            "segment_url": "",
            "status_code": None,
            "error": "",
        }

        try:
            # 1) playlist fetch
            playlist_resp = requests.get(url, headers=headers, timeout=timeout, verify=False)
            result["status_code"] = getattr(playlist_resp, "status_code", None)
            playlist_resp.raise_for_status()
            playlist_text = playlist_resp.text or ""
            playlist_url = getattr(playlist_resp, "url", url) or url
            result["playlist_url"] = playlist_url

            # master playlist -> pick first variant
            if "#EXT-X-STREAM-INF" in playlist_text:
                first_variant = cls._pick_first_variant(playlist_text, playlist_url)
                if not first_variant:
                    result["stage"] = "playlist"
                    result["error"] = "master playlist has no resolvable variant"
                    return result
                playlist_resp = requests.get(first_variant, headers=headers, timeout=timeout, verify=False)
                result["status_code"] = getattr(playlist_resp, "status_code", None)
                playlist_resp.raise_for_status()
                playlist_text = playlist_resp.text or ""
                playlist_url = getattr(playlist_resp, "url", first_variant) or first_variant
                result["playlist_url"] = playlist_url

            # media playlist sanity
            first_segment = cls._pick_first_segment(playlist_text, playlist_url)
            if not first_segment:
                result["stage"] = "playlist"
                result["error"] = "media playlist has no segment"
                return result
            result["segment_url"] = first_segment

            # 2) optional key fetch
            key_url = cls._pick_key_url(playlist_text, playlist_url)
            if key_url:
                result["stage"] = "key"
                result["key_url"] = key_url
                key_resp = requests.get(key_url, headers=headers, timeout=timeout, verify=False)
                key_resp.raise_for_status()

            # 3) first segment fetch (small range)
            result["stage"] = "segment"
            seg_headers = headers.copy()
            if "Range" not in seg_headers and "range" not in seg_headers:
                seg_headers["Range"] = "bytes=0-2047"
            seg_resp = requests.get(first_segment, headers=seg_headers, timeout=timeout, verify=False, stream=True)
            seg_resp.raise_for_status()
            seg_resp.close()

            result["ok"] = True
            result["stage"] = "ready"
            return result

        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
            return result

    @staticmethod
    def _pick_first_variant(playlist_text: str, base_url: str) -> str:
        lines = [ln.strip() for ln in (playlist_text or "").splitlines()]
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    cand = lines[j]
                    if not cand or cand.startswith("#"):
                        continue
                    return urljoin(base_url, cand)
        return ""

    @staticmethod
    def _pick_key_url(playlist_text: str, base_url: str) -> str:
        match = re.search(r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', playlist_text or "", flags=re.IGNORECASE)
        if not match:
            return ""
        return urljoin(base_url, match.group(1).strip())

    @staticmethod
    def _pick_first_segment(playlist_text: str, base_url: str) -> str:
        lines = [ln.strip() for ln in (playlist_text or "").splitlines()]
        for line in lines:
            if not line or line.startswith("#"):
                continue
            return urljoin(base_url, line)
        return ""
