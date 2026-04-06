"""
M3U8 resource sniffer for detecting video resources from network requests.
"""
from datetime import datetime
from typing import Callable, List, Optional
from urllib.parse import urlparse

from core.site_rule_utils import set_header_if_missing, site_rule_matches
from core.task_model import M3U8Resource
from utils.config_manager import config
from utils.logger import logger
from utils.notification import notify_resource_found
from utils.i18n import TR


class M3U8Sniffer:
    """M3U8/视频资源嗅探器"""

    def __init__(self):
        self.resources: List[M3U8Resource] = []
        self.on_resource_found: Callable | None = None
        self._seen_urls = set()
        self._site_rules = config.get("site_rules", []) or []
        self._features = config.get("features", {}) or {}

    def add_resource(self, url: str, headers: dict, page_url: str, page_title: str = "") -> Optional[M3U8Resource]:
        """Add a detected resource, deduping by URL and merging context when duplicated."""
        headers = headers or {}
        url_lower = (url or "").lower()
        is_m3u8 = ".m3u8" in url_lower
        candidate_score = 0

        # 仅对 m3u8 进行 headers 规范化与站点规则补全
        if is_m3u8:
            headers = self._normalize_m3u8_headers(headers, page_url)
            if self._features.get("sniffer_rules_enabled", True):
                headers = self._apply_site_rules(url, page_url, headers)
            candidate_score = self._score_m3u8_candidate(url, headers, page_url)

        if self._features.get("sniffer_dedup_enabled", True) and url in self._seen_urls:
            existing = self._find_resource_by_url(url)
            if existing:
                merged = self._merge_resource_context(
                    existing,
                    headers,
                    page_url,
                    page_title,
                    candidate_score,
                )
                logger.debug(
                    TR("log_resource_merged") if merged else TR("log_resource_exists"), 
                    event="sniffer_dedup", 
                    url=url, 
                    merged=merged
                )
                return existing
            logger.debug(TR("log_resource_exists"), event="sniffer_dedup", url=url)
            return None

        resource = M3U8Resource(
            url=url,
            headers=headers,
            page_url=page_url,
            page_title=page_title,
            candidate_score=candidate_score,
        )

        self.resources.append(resource)
        self._seen_urls.add(url)

        logger.info(f"[FOUND] {TR('log_new_resource_found')}", event="sniffer_hit", title=resource.title)
        logger.debug(TR("log_resource_detail"), url=url, page=page_url)

        notify_resource_found(resource.title)

        if self.on_resource_found:
            self.on_resource_found(resource)

        return resource

    def clear_resources(self):
        """Clear all resources."""
        self.resources.clear()
        self._seen_urls.clear()
        logger.info(TR("log_resources_cleared"))

    def remove_resource(self, resource: M3U8Resource):
        """Remove one resource."""
        if resource in self.resources:
            self.resources.remove(resource)
            self._seen_urls.discard(resource.url)
            logger.info(f"{TR('log_resource_removed')}: {resource.title}")

    def get_resources(self) -> List[M3U8Resource]:
        """Get a copy of all resources."""
        return self.resources.copy()

    def get_resource_count(self) -> int:
        """Get resource count."""
        return len(self.resources)

    def _find_resource_by_url(self, url: str) -> Optional[M3U8Resource]:
        for resource in self.resources:
            if resource.url == url:
                return resource
        return None

    def _merge_resource_context(
        self,
        resource: M3U8Resource,
        headers: dict,
        page_url: str,
        page_title: str = "",
        candidate_score: int = 0,
    ) -> bool:
        """Merge new capture context into existing resource."""
        changed = False
        headers = headers or {}

        if not isinstance(resource.headers, dict):
            resource.headers = {}

        # 认证关键头优先采用最新的非空值
        preferred_keys = ("cookie", "authorization", "referer", "origin", "user-agent")
        for key in preferred_keys:
            value = headers.get(key)
            if not value:
                continue
            if resource.headers.get(key) != value:
                resource.headers[key] = value
                changed = True

        # 其他头仅补缺
        for key, value in headers.items():
            if not key or value in (None, ""):
                continue
            if key not in resource.headers:
                resource.headers[key] = value
                changed = True

        # page_url/page_title 仅在旧值缺失或明显占位时更新
        if page_url and page_url != resource.page_url:
            if not resource.page_url or resource.page_url == resource.url:
                resource.page_url = page_url
                changed = True

        if page_title and page_title != resource.page_title:
            if not resource.page_title:
                resource.page_title = page_title
                changed = True

        if changed:
            resource.timestamp = datetime.now()

        if candidate_score and candidate_score > getattr(resource, "candidate_score", 0):
            resource.candidate_score = candidate_score
            changed = True

        return changed

    @staticmethod
    def is_video_resource(url: str) -> bool:
        """Check if URL looks like a video resource."""
        url_lower = (url or "").lower()

        if ".m3u8" in url_lower:
            return True

        video_exts = (
            ".mp4",
            ".flv",
            ".mkv",
            ".avi",
            ".mov",
            ".wmv",
            ".webm",
            ".m4v",
            ".3gp",
            ".mpg",
            ".mpeg",
            ".f4v",
            ".m4s",
        )
        url_without_params = url_lower.split("?")[0]
        if url_without_params.endswith(video_exts):
            return True

        if ".mpd" in url_lower:
            return True

        return False

    def _normalize_m3u8_headers(self, headers: dict, page_url: str) -> dict:
        """Normalize m3u8 request headers (lowercase keys + defaults)."""
        normalized = {}
        for key, value in (headers or {}).items():
            if key is None:
                continue
            key_lower = str(key).strip().lower()
            if not key_lower:
                continue
            normalized[key_lower] = value

        if page_url and not normalized.get("referer"):
            normalized["referer"] = page_url

        if not normalized.get("user-agent"):
            normalized["user-agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        if not normalized.get("origin") and normalized.get("referer"):
            try:
                from urllib.parse import urlparse

                parsed = urlparse(normalized.get("referer"))
                if parsed.scheme and parsed.netloc:
                    normalized["origin"] = f"{parsed.scheme}://{parsed.netloc}"
            except Exception:
                pass

        return normalized

    def _apply_site_rules(self, url: str, page_url: str, headers: dict) -> dict:
        """Apply site_rules-based header completion."""
        if not self._site_rules:
            return headers

        for rule in self._site_rules:
            if not site_rule_matches(rule, url, page_url):
                continue

            set_header_if_missing(headers, "referer", rule.get("referer"))
            set_header_if_missing(headers, "user-agent", rule.get("user_agent"))

            extra_headers = rule.get("headers", {}) or {}
            for key, value in extra_headers.items():
                set_header_if_missing(headers, key, value)

            logger.info(f"[RULE] {TR('log_apply_rule')}: {rule.get('name', 'unknown')}")
            break

        return headers

    def _score_m3u8_candidate(self, url: str, headers: dict, page_url: str) -> int:
        """Heuristic scoring for m3u8 candidate quality (higher is better)."""
        score = 0
        url_lower = (url or "").lower()
        headers = headers or {}

        if url_lower.startswith("https://"):
            score += 20
        if ".m3u8" in url_lower:
            score += 40
        if any(k in url_lower for k in ("/hls/", "playlist", "index.m3u8", "media.m3u8")):
            score += 20
        if "master.m3u8" in url_lower:
            score -= 5
        if any(k in url_lower for k in ("ad", "ads", "promo", "tracker")):
            score -= 25

        if headers.get("referer"):
            score += 15
        if headers.get("origin"):
            score += 8
        if headers.get("cookie"):
            score += 25
        if headers.get("authorization"):
            score += 10

        try:
            host = urlparse(url).hostname or ""
            page_host = urlparse(page_url).hostname or ""
            if host and page_host and host == page_host:
                score += 8
        except Exception:
            pass

        return score
