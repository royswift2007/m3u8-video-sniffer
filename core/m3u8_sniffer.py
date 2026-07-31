"""
M3U8 resource sniffer for detecting video resources from network requests.
"""
from datetime import datetime
import re
from typing import Callable, List, Optional
from urllib.parse import urlparse

from core.download_context import (
    build_engine_select_context,
    RESOURCE_TYPE_DASH,
    RESOURCE_TYPE_HLS,
    RESOURCE_TYPE_SEGMENT,
)
from core.site_rule_utils import iter_matching_site_rules, set_header_if_missing
from core.task_model import M3U8Resource
from utils.config_manager import config
from utils.headers import normalized_forward_headers
from utils.logger import logger
from utils.notification import notify_resource_found
from utils.i18n import TR
from utils.ssrf_guard import SSRFBlocked, ensure_public
from utils.redact import redact_url


class M3U8Sniffer:
    """M3U8/视频资源嗅探器"""

    def __init__(self):
        self.resources: List[M3U8Resource] = []
        self.on_resource_found: Callable | None = None
        self._seen_urls = set()
        self._segment_group_counts: dict[str, int] = {}
        self._site_rules = config.get("site_rules", []) or []
        self._features = config.get("features", {}) or {}

    def refresh_config(self) -> None:
        """Refresh config-backed sniffer flags/rules.

        ISS-28: ``site_rules`` and ``features`` can be edited while a sniffer
        instance is already alive.  Do not keep using constructor-time copies;
        refresh them before each resource ingestion so rule toggles and newly
        learned rules take effect without recreating the browser/sniffer stack.
        """
        self._site_rules = config.get("site_rules", []) or []
        self._features = config.get("features", {}) or {}

    def add_resource(
        self,
        url: str,
        headers: dict,
        page_url: str,
        page_title: str = "",
        *,
        source: str = "unknown",
        resource_type: str = "",
        mime: str = "",
        master_url: str | None = None,
        media_url: str | None = None,
    ) -> Optional[M3U8Resource]:
        """Add a detected resource, deduping by URL and merging context when duplicated."""
        self.refresh_config()
        headers = headers or {}

        # R4 SSRF filter: reject resources whose host resolves to a
        # loopback/private/link-local/metadata address before we ever
        # store them. This prevents a malicious page (or a misbehaving
        # browser extension) from parking internal URLs in the resource
        # list where the downloader would later fetch them.
        try:
            # F-02: honour the security.allow_private_networks opt-in so
            # a trusted private-mirror deployment can still capture
            # resources; otherwise fail-closed on private/loopback.
            allow_private = bool(
                config.get("security.allow_private_networks", False)
            )
            if allow_private:
                ensure_public(url, allow_private=True)
                logger.warning(
                    "[SSRF] allow_private_networks is enabled; private resource accepted",
                    event="ssrf_private_allowed",
                    stage="ssrf",
                    url=redact_url(url),
                )
            else:
                ensure_public(url)
        except SSRFBlocked as exc:
            logger.warning(
                "[SSRF] resource rejected",
                event="sniffer_ssrf_blocked",
                stage="ssrf",
                reason=exc.reason,
                url=redact_url(url),
            )
            return None

        context = build_engine_select_context(
            url=url,
            page_url=page_url,
            page_title=page_title,
            headers=headers,
            source=source,
            resource_type=resource_type,
            mime=mime,
            master_url=master_url,
            media_url=media_url,
        )

        is_hls = context.resource_type == RESOURCE_TYPE_HLS
        is_segment = context.resource_type == RESOURCE_TYPE_SEGMENT
        candidate_score = 0
        segment_group_key = ""
        segment_group_count = 0
        suppress_segment = False

        # 仅对 HLS 进行 headers 规范化与站点规则补全
        if is_hls:
            headers = self._normalize_m3u8_headers(headers, context.page_url)
            if self._features.get("sniffer_rules_enabled", True):
                headers = self._apply_site_rules(url, context.page_url, headers, context.resource_type)
            candidate_score = self._score_m3u8_candidate(url, headers, context.page_url)
        elif is_segment:
            segment_group_key = self._segment_group_key(url, context.page_url)
            segment_group_count = self._record_segment_group(segment_group_key)
            suppress_segment = self._should_suppress_segment(
                url,
                context.page_url,
                segment_group_key,
                segment_group_count,
            )
            candidate_score = -80 if suppress_segment else -40

        cookie_len = len((headers or {}).get("cookie", "") or "")
        logger.info(
            "[SNIFFER] resource header 摘要: "
            f"source={context.source} has_cookie={cookie_len > 0} cookie_len={cookie_len}",
            event="sniffer_headers_summary",
            source=context.source,
            resource_type=context.resource_type,
            has_cookie=cookie_len > 0,
            cookie_len=cookie_len,
        )

        if self._features.get("sniffer_dedup_enabled", True) and url in self._seen_urls:
            existing = self._find_resource_by_url(url)
            if existing:
                merged = self._merge_resource_context(
                    existing,
                    headers,
                    context.page_url,
                    context.page_title,
                    candidate_score,
                    source=context.source,
                    resource_type=context.resource_type,
                    mime=context.mime,
                    master_url=context.master_url,
                    media_url=context.media_url,
                    is_suppressed=suppress_segment,
                    suppression_reason="segment_grouped" if suppress_segment else "",
                    segment_group_key=segment_group_key,
                    segment_group_count=segment_group_count,
                )
                logger.debug(
                    TR("log_resource_merged") if merged else TR("log_resource_exists"),
                    event="sniffer_dedup",
                    url=url,
                    merged=merged
                )
                if merged and self.on_resource_found:
                    self.on_resource_found(existing)
                return existing
            logger.debug(TR("log_resource_exists"), event="sniffer_dedup", url=url)
            return None

        resource = M3U8Resource(
            url=url,
            headers=headers,
            page_url=context.page_url,
            page_title=context.page_title,
            source=context.source,
            resource_type=context.resource_type,
            mime=context.mime,
            master_url=context.master_url,
            media_url=context.media_url,
            candidate_score=candidate_score,
            is_suppressed=suppress_segment,
            suppression_reason="segment_grouped" if suppress_segment else "",
            segment_group_key=segment_group_key,
            segment_group_count=segment_group_count,
        )

        self.resources.append(resource)
        self._seen_urls.add(url)

        logger.info(f"[FOUND] {TR('log_new_resource_found')}", event="sniffer_hit", title=resource.title)
        logger.debug(TR("log_resource_detail"), url=url, page=page_url)

        if suppress_segment:
            logger.info(
                "[SNIFFER] segment 资源已降噪",
                event="segment_suppressed",
                resource_type=context.resource_type,
                group_key=segment_group_key,
                group_count=segment_group_count,
                url=redact_url(url),
            )
        else:
            notify_resource_found(resource.title)

        if self.on_resource_found:
            self.on_resource_found(resource)

        return resource

    def clear_resources(self):
        """Clear all resources."""
        self.resources.clear()
        self._seen_urls.clear()
        self._segment_group_counts.clear()
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
        *,
        source: str = "unknown",
        resource_type: str = "unknown",
        mime: str = "",
        master_url: str | None = None,
        media_url: str | None = None,
        is_suppressed: bool = False,
        suppression_reason: str = "",
        segment_group_key: str = "",
        segment_group_count: int = 0,
    ) -> bool:
        """Merge new capture context into existing resource."""
        changed = False
        headers = headers or {}

        if not isinstance(resource.headers, dict):
            resource.headers = {}

        existing_score = self._score_m3u8_candidate(resource.url, resource.headers, resource.page_url)
        incoming_score = self._score_m3u8_candidate(resource.url, headers, page_url)
        incoming_is_more_complete = incoming_score >= existing_score

        # 认证关键头优先保留更完整的一份，避免 PWR-CAP 空/弱 headers 覆盖 PWR-REQ/PWR-RSP。
        preferred_keys = ("cookie", "authorization", "referer", "origin", "user-agent")
        for key in preferred_keys:
            value = headers.get(key)
            if not value:
                continue
            if resource.headers.get(key) != value and incoming_is_more_complete:
                resource.headers[key] = value
                changed = True

        # 其他头仅补缺；只有新 headers 整体更完整时才更新已有值。
        for key, value in headers.items():
            if not key or value in (None, ""):
                continue
            if key not in resource.headers:
                resource.headers[key] = value
                changed = True
            elif incoming_is_more_complete and resource.headers.get(key) != value:
                resource.headers[key] = value
                changed = True

        # page_url/page_title 仅在旧值缺失或明显占位时更新
        if page_url and page_url != resource.page_url:
            if not resource.page_url or resource.page_url == resource.url:
                resource.page_url = page_url
                changed = True

        if resource.apply_page_title(page_title):
            changed = True

        if candidate_score and candidate_score > getattr(resource, "candidate_score", 0):
            resource.candidate_score = candidate_score
            changed = True

        # Context field backfill — only upgrade when the old value is missing or unknown.
        old_source = getattr(resource, "source", "unknown") or "unknown"
        if (not old_source or old_source == "unknown") and source and source != "unknown":
            resource.source = source
            changed = True

        old_rt = getattr(resource, "resource_type", "unknown") or "unknown"
        if (not old_rt or old_rt == "unknown") and resource_type and resource_type != "unknown":
            resource.resource_type = resource_type
            changed = True

        old_mime = getattr(resource, "mime", "") or ""
        if not old_mime and mime:
            resource.mime = mime
            changed = True

        if master_url and not getattr(resource, "master_url", None):
            resource.master_url = master_url
            changed = True

        if media_url and not getattr(resource, "media_url", None):
            resource.media_url = media_url
            changed = True

        if is_suppressed and not getattr(resource, "is_suppressed", False):
            resource.is_suppressed = True
            resource.suppression_reason = suppression_reason or "segment_grouped"
            changed = True
        if segment_group_key and segment_group_key != getattr(resource, "segment_group_key", ""):
            resource.segment_group_key = segment_group_key
            changed = True
        if segment_group_count and segment_group_count > getattr(resource, "segment_group_count", 0):
            resource.segment_group_count = segment_group_count
            changed = True

        if changed:
            resource.timestamp = datetime.now()

        return changed

    def _record_segment_group(self, group_key: str) -> int:
        """Increment and return the number of seen segment URLs in a group."""
        if not group_key:
            return 0
        count = self._segment_group_counts.get(group_key, 0) + 1
        self._segment_group_counts[group_key] = count
        return count

    @staticmethod
    def _segment_group_key(url: str, page_url: str = "") -> str:
        """Build a stable grouping key for sequential HLS/DASH segments."""
        try:
            parsed = urlparse(url)
            page = urlparse(page_url or "")
        except ValueError:
            return url or ""

        resource_host = (parsed.hostname or "").lower()
        page_host = (page.hostname or "").lower()
        path = (parsed.path or "").lower()
        pattern = re.sub(r"\d+", "{n}", path)
        return f"{page_host}|{resource_host}|{pattern}"

    def _has_playlist_for_page(self, url: str, page_url: str = "") -> bool:
        """Return True when the same page/source has already yielded a playlist."""
        try:
            target_page_host = (urlparse(page_url or "").hostname or "").lower()
            target_host = (urlparse(url or "").hostname or "").lower()
        except ValueError:
            target_page_host = ""
            target_host = ""

        for resource in self.resources:
            if getattr(resource, "resource_type", "") not in {RESOURCE_TYPE_HLS, RESOURCE_TYPE_DASH}:
                continue
            if page_url and getattr(resource, "page_url", "") == page_url:
                return True
            try:
                resource_page_host = (urlparse(getattr(resource, "page_url", "") or "").hostname or "").lower()
                resource_host = (urlparse(getattr(resource, "url", "") or "").hostname or "").lower()
            except ValueError:
                continue
            if target_page_host and resource_page_host and target_page_host == resource_page_host:
                return True
            if target_host and resource_host and target_host == resource_host:
                return True
        return False

    def _should_suppress_segment(
        self,
        url: str,
        page_url: str,
        group_key: str,
        group_count: int,
    ) -> bool:
        """Decide whether a segment should be hidden/folded as noise."""
        if not self._features.get("segment_suppression_enabled", True):
            return False
        if self._has_playlist_for_page(url, page_url):
            return True
        threshold = int(self._features.get("segment_suppression_threshold", 3) or 3)
        return bool(group_key and group_count >= max(1, threshold))

    @staticmethod
    def is_video_resource(url: str) -> bool:
        """Check if URL looks like a video or segment resource."""
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
            ".aac",
            ".key",
        )
        url_without_params = url_lower.split("?")[0]
        if url_without_params.endswith(video_exts):
            return True

        if ".mpd" in url_lower:
            return True

        return False

    def _normalize_m3u8_headers(self, headers: dict, page_url: str) -> dict:
        """Normalize m3u8 request headers (lowercase keys + defaults)."""
        include_cookie = bool(self._features.get("forward_cookie_headers", True))
        include_authorization = bool(self._features.get("forward_authorization_headers", False))
        normalized = normalized_forward_headers(
            headers,
            include_cookie=include_cookie,
            include_authorization=include_authorization,
        )
        if include_authorization and normalized.get("authorization"):
            normalized["_allow_authorization_header"] = True

        cookie_len = len(normalized.get("cookie", "") or "")
        logger.debug(
            "m3u8_sniffer: normalized headers summary",
            event="sniffer_normalized_headers",
            has_cookie=cookie_len > 0,
            cookie_len=cookie_len,
        )

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
            except ValueError:
                # Malformed referer cannot produce an origin; keep normalized
                # as-is. Do not log the referer itself — it may carry tokens.
                logger.debug("m3u8_sniffer: referer→origin fallback skipped")

        return normalized

    def _apply_site_rules(self, url: str, page_url: str, headers: dict, resource_type: str = "") -> dict:
        """Apply site_rules-based header completion."""
        if not self._site_rules:
            return headers

        for rule in iter_matching_site_rules(self._site_rules, url, page_url, resource_type):
            set_header_if_missing(headers, "referer", rule.get("referer"))
            set_header_if_missing(headers, "user-agent", rule.get("user_agent"))

            extra_headers = rule.get("headers", {}) or {}
            for key, value in extra_headers.items():
                set_header_if_missing(headers, key, value)

            include_cookie = bool(self._features.get("forward_cookie_headers", True))
            include_authorization = bool(
                rule.get("allow_authorization", False)
                or rule.get("authorization_policy") == "prompt"
                or self._features.get("forward_authorization_headers", False)
            )
            headers = normalized_forward_headers(
                headers,
                include_cookie=include_cookie,
                include_authorization=include_authorization,
            )
            if include_authorization and headers.get("authorization"):
                headers["_allow_authorization_header"] = True

            logger.info(
                f"[RULE] {TR('log_apply_rule')}: {rule.get('name', 'unknown')}",
                event="sniffer_site_rule_applied",
                rule=rule.get("name", "unknown"),
                rule_priority=rule.get("priority", 0),
                resource_type=resource_type or "unknown",
            )
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
        except ValueError:
            # urlparse raises on malformed IPv6 / invalid percent-encoding.
            # Scoring is a best-effort heuristic; drop the bonus silently.
            logger.debug("m3u8_sniffer: host-match score skipped")

        return score
