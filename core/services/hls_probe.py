"""HLS preflight probe utilities."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

import requests

from utils.config_manager import config
from utils.logger import logger
from utils.redact import redact_url
from utils.proxy_config import requests_proxies
from utils.ssrf_guard import (
    SSRFBlocked,
    ensure_public,
    make_pinned_session,
)


class HLSProbe:
    """Lightweight HLS probe: playlist -> key -> first segment."""

    SOFT_SEGMENT_STATUS_CODES = {403, 429}
    SOFT_KEY_STATUS_CODES = {429}
    SOFT_RATE_LIMIT_ERROR_KEYWORDS = (
        "too many requests",
        "rate limit",
        "rate-limit",
        "ratelimit",
    )
    SOFT_SEGMENT_ERROR_KEYWORDS = (
        *SOFT_RATE_LIMIT_ERROR_KEYWORDS,
        "forbidden",
        "hotlink",
        "anti-leech",
        "anti leech",
        "referer",
        "referrer",
    )

    @classmethod
    def probe(cls, url: str, headers: dict | None = None, timeout: int = 8) -> dict:
        headers = (headers or {}).copy()
        verify_tls = bool(config.get("features.network_verify_tls", True))
        allow_segment_probe_soft_fail = bool(
            config.get("features.allow_segment_probe_soft_fail", True)
        )
        extended_soft_fail = bool(
            config.get("features.hls_probe_extended_soft_fail_enabled", True)
        )
        if "User-Agent" not in headers and "user-agent" not in headers:
            headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        if not verify_tls:
            logger.warning("[HLSProbe] TLS verification disabled by config")

        result = {
            "ok": False,
            "stage": "playlist",
            "playlist_url": url,
            "key_url": "",
            "segment_url": "",
            "status_code": None,
            "error": "",
            "warning": "",
            "soft_fail": False,
            "hard_fail": True,
            "severity": "error",
            # F-08: best-effort byte estimate derived from the media
            # playlist (target duration × segment count × a conservative
            # bitrate). ``None`` means "no estimate"; callers fall back
            # to the 500 MiB default in download_manager.
            "estimated_bytes": None,
            "probe_action": "playlist_fetch",
            "range_retry": False,
            "stage_diagnostics": [],
        }

        def record_stage(
            stage: str,
            action: str,
            target_url: str = "",
            *,
            status_code: int | None = None,
            ok: bool | None = None,
            error: str = "",
            soft_fail: bool = False,
        ) -> None:
            """Append one structured probe-stage diagnostic entry."""
            diagnostics = result.setdefault("stage_diagnostics", [])
            if not isinstance(diagnostics, list):
                diagnostics = []
                result["stage_diagnostics"] = diagnostics
            diagnostics.append(
                {
                    "stage": stage,
                    "action": action,
                    "url": target_url,
                    "status_code": status_code,
                    "ok": ok,
                    "error": error,
                    "soft_fail": soft_fail,
                }
            )

        try:
            # 1) playlist fetch. Playlist/master failures stay hard failures because the
            # real downloader cannot start without a readable manifest.
            # R4 SSRF filter: reject non-public manifest hosts before touching the network.
            # F-01/F-02: the actual pinning + opt-in happens inside
            # _get_no_redirects; this pre-check is a cheap fast-fail for
            # hard-blocked schemes/hosts so we never construct a session.
            result["probe_action"] = "playlist_fetch"
            cls._ensure_public_opt(url)
            playlist_resp = cls._get_no_redirects(
                url,
                headers=headers,
                timeout=timeout,
                verify=verify_tls,
            )
            playlist_resp = cls._follow_redirects(
                playlist_resp,
                url,
                headers=headers,
                timeout=timeout,
                verify=verify_tls,
            )
            result["status_code"] = getattr(playlist_resp, "status_code", None)
            record_stage(
                "playlist",
                result["probe_action"],
                url,
                status_code=result["status_code"],
                ok=cls._status_ok(result["status_code"]),
            )
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
                # R4 SSRF filter: re-check the variant URL (different host from the master is legal).
                result["probe_action"] = "variant_fetch"
                cls._ensure_public_opt(first_variant)
                playlist_resp = cls._get_no_redirects(
                    first_variant,
                    headers=headers,
                    timeout=timeout,
                    verify=verify_tls,
                )
                playlist_resp = cls._follow_redirects(
                    playlist_resp,
                    first_variant,
                    headers=headers,
                    timeout=timeout,
                    verify=verify_tls,
                )
                result["status_code"] = getattr(playlist_resp, "status_code", None)
                record_stage(
                    "variant",
                    result["probe_action"],
                    first_variant,
                    status_code=result["status_code"],
                    ok=cls._status_ok(result["status_code"]),
                )
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

            # 2) optional key fetch. Key failures remain hard failures: an encrypted
            # stream normally cannot be decrypted without a readable key.
            key_url = cls._pick_key_url(playlist_text, playlist_url)
            if key_url:
                result["stage"] = "key"
                result["key_url"] = key_url
                # R4 SSRF filter: key URL is often hosted on a different host.
                result["probe_action"] = "key_fetch"
                cls._ensure_public_opt(key_url)
                key_resp = None
                try:
                    key_resp = cls._get_no_redirects(
                        key_url,
                        headers=headers,
                        timeout=timeout,
                        verify=verify_tls,
                    )
                    key_resp = cls._follow_redirects(
                        key_resp,
                        key_url,
                        headers=headers,
                        timeout=timeout,
                        verify=verify_tls,
                    )
                    result["status_code"] = getattr(key_resp, "status_code", None)
                    record_stage(
                        "key",
                        result["probe_action"],
                        key_url,
                        status_code=result["status_code"],
                        ok=cls._status_ok(result["status_code"]),
                    )
                    key_resp.raise_for_status()
                except Exception as e:
                    if extended_soft_fail and cls._is_soft_key_failure(e, result.get("status_code")):
                        warning = f"key probe soft-failed: {e}"
                        result.update(
                            {
                                "ok": False,
                                "soft_fail": True,
                                "hard_fail": False,
                                "severity": "warning",
                                "warning": warning,
                                "error": str(e),
                            }
                        )
                        record_stage(
                            "key",
                            result["probe_action"],
                            key_url,
                            status_code=result.get("status_code"),
                            ok=False,
                            error=str(e),
                            soft_fail=True,
                        )
                        logger.warning(
                            "[HLSProbe] key probe soft-fail, allowing engine attempt",
                            event="hls_probe_key_soft_fail",
                            url=redact_url(url),
                            key=redact_url(key_url),
                            status_code=result.get("status_code"),
                            error=str(e),
                            probe_action=result.get("probe_action"),
                        )
                        return result
                    raise
                finally:
                    if key_resp is not None:
                        key_resp.close()

            # 3) first segment fetch (small range). Segment probes are intentionally
            # conservative: many CDNs rate-limit ad-hoc probe requests while the real
            # downloader may still succeed with retries, cookies, and normal cadence.
            result["stage"] = "segment"
            seg_headers = headers.copy()
            has_explicit_range = "Range" in seg_headers or "range" in seg_headers
            if not has_explicit_range:
                seg_headers["Range"] = "bytes=0-2047"
            # R4 SSRF filter: segment URL may resolve differently from the playlist.
            result["probe_action"] = "segment_range_probe"
            cls._ensure_public_opt(first_segment)
            seg_resp = None
            try:
                try:
                    seg_resp = cls._fetch_segment_probe(
                        first_segment,
                        headers=seg_headers,
                        timeout=timeout,
                        verify=verify_tls,
                    )
                    result["status_code"] = getattr(seg_resp, "status_code", None)
                    record_stage(
                        "segment",
                        result["probe_action"],
                        first_segment,
                        status_code=result["status_code"],
                        ok=cls._status_ok(result["status_code"]),
                    )
                    seg_resp.raise_for_status()
                except Exception as range_error:
                    if (
                        extended_soft_fail
                        and not has_explicit_range
                        and cls._is_range_probe_retryable(range_error, result.get("status_code"))
                    ):
                        close_response = getattr(seg_resp, "close", None)
                        if callable(close_response):
                            close_response()
                        full_headers = headers.copy()
                        result["probe_action"] = "segment_full_probe_after_range"
                        result["range_retry"] = True
                        logger.warning(
                            "[HLSProbe] segment range probe failed, retrying without Range",
                            event="hls_probe_segment_range_retry",
                            url=redact_url(url),
                            segment=redact_url(first_segment),
                            status_code=result.get("status_code"),
                            error=str(range_error),
                        )
                        seg_resp = cls._fetch_segment_probe(
                            first_segment,
                            headers=full_headers,
                            timeout=timeout,
                            verify=verify_tls,
                        )
                        result["status_code"] = getattr(seg_resp, "status_code", None)
                        record_stage(
                            "segment",
                            result["probe_action"],
                            first_segment,
                            status_code=result["status_code"],
                            ok=cls._status_ok(result["status_code"]),
                        )
                        seg_resp.raise_for_status()
                    else:
                        raise
            except Exception as e:
                if allow_segment_probe_soft_fail and cls._is_soft_segment_failure(e, result.get("status_code")):
                    warning = f"segment probe soft-failed: {e}"
                    result.update(
                        {
                            "ok": False,
                            "soft_fail": True,
                            "hard_fail": False,
                            "severity": "warning",
                            "warning": warning,
                            "error": str(e),
                        }
                    )
                    record_stage(
                        "segment",
                        result.get("probe_action", "segment_range_probe"),
                        first_segment,
                        status_code=result.get("status_code"),
                        ok=False,
                        error=str(e),
                        soft_fail=True,
                    )
                    logger.warning(
                        "[HLSProbe] segment probe soft-fail, allowing engine attempt",
                        event="hls_probe_segment_soft_fail",
                        url=redact_url(url),
                        segment=redact_url(first_segment),
                        status_code=result.get("status_code"),
                        error=str(e),
                        probe_action=result.get("probe_action"),
                        range_retry=result.get("range_retry"),
                    )
                    return result
                raise
            finally:
                if seg_resp is not None:
                    seg_resp.close()

            result["ok"] = True
            result["stage"] = "ready"
            result["probe_action"] = "ready"
            result["hard_fail"] = False
            result["severity"] = "ok"
            # F-08: derive a conservative byte estimate from the media
            # playlist so the download precheck can size the disk guard
            # instead of always assuming 500 MiB.
            result["estimated_bytes"] = cls._estimate_bytes_from_playlist(playlist_text)
            return result

        except SSRFBlocked as exc:
            # Segment URL checks are a best-effort preflight only. Some
            # third-party media playlists contain CDN/ad segment hosts that
            # fail local DNS/SSRF probing even though the real downloader may
            # still succeed with its own resolver, retry cadence, or by moving
            # past transient probe-only failures. Do not block the actual
            # engine on segment-stage probe failures when soft segment probing
            # is enabled.
            if result.get("stage") == "segment" and allow_segment_probe_soft_fail:
                warning = f"segment probe ssrf soft-failed: {exc.reason}"
                result.update(
                    {
                        "ok": False,
                        "soft_fail": True,
                        "hard_fail": False,
                        "severity": "warning",
                        "warning": warning,
                        "error": f"ssrf_blocked: {exc.reason}",
                        "error_code": "ssrf_blocked_segment_soft",
                    }
                )
                record_stage(
                    "segment",
                    result.get("probe_action", "segment_range_probe"),
                    getattr(exc, "url", ""),
                    ok=False,
                    error=f"ssrf_blocked: {exc.reason}",
                    soft_fail=True,
                )
                logger.warning(
                    "[HLSProbe] segment SSRF/DNS probe soft-fail, allowing engine attempt",
                    event="hls_probe_segment_ssrf_soft_fail",
                    stage="ssrf",
                    probe_stage=result.get("stage"),
                    reason=exc.reason,
                    url=redact_url(exc.url),
                )
                return result

            # Map the SSRF policy refusal to a structured probe failure.
            # The ``stage`` field retains whatever step was in progress so
            # the UI can tell the user which URL was rejected.
            record_stage(
                str(result.get("stage") or "ssrf"),
                result.get("probe_action", "ssrf_check"),
                getattr(exc, "url", ""),
                ok=False,
                error=f"ssrf_blocked: {exc.reason}",
            )
            logger.warning(
                f"[SSRF] HLS probe blocked: {exc.reason}",
                event="hls_probe_ssrf_blocked",
                stage="ssrf",
                probe_stage=result.get("stage"),
                reason=exc.reason,
                url=redact_url(exc.url),
            )
            result["ok"] = False
            result["soft_fail"] = False
            result["hard_fail"] = True
            result["severity"] = "error"
            result["error"] = f"ssrf_blocked: {exc.reason}"
            result["error_code"] = "ssrf_blocked"
            return result

        except Exception as e:
            result["ok"] = False
            result["soft_fail"] = False
            result["hard_fail"] = True
            result["severity"] = "error"
            result["error"] = str(e)
            return result

    @staticmethod
    def _status_ok(status_code: object) -> bool:
        """Return True when a response status code represents success."""
        try:
            return status_code is not None and int(status_code) < 400
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _allow_private_networks() -> bool:
        """F-02 opt-in flag from ``security.allow_private_networks``."""
        try:
            return bool(config.get("security.allow_private_networks", False))
        except Exception:
            return False

    @classmethod
    def _ensure_public_opt(cls, url: str):
        """SSRF guard entry point honouring the F-02 opt-in.

        Mirrors :meth:`core.m3u8_parser.M3U8FetchThread._ensure_public_opt`:
        when the opt-in is on, a private target is accepted but a
        prominent ``ssrf_private_allowed`` warning is logged so audit
        trails surface the deviation.

        The ``allow_private`` kwarg is ONLY passed when the opt-in is on
        so that callers (and tests) that substitute a plain
        ``ensure_public(url)`` mock are not broken by an unexpected
        keyword argument.
        """
        allow_private = cls._allow_private_networks()
        if allow_private:
            try:
                resolved = ensure_public(url, allow_private=True)
            except SSRFBlocked:
                raise
            logger.warning(
                "[HLSProbe] allow_private_networks is enabled; private target accepted",
                event="ssrf_private_allowed",
                stage="ssrf",
                url=redact_url(url),
            )
            return resolved
        # Default path: do not pass allow_private so legacy
        # ``ensure_public(url)`` signatures (and test mocks) stay valid.
        return ensure_public(url)

    @classmethod
    def _get_no_redirects(cls, url: str, **kwargs):
        """F-01: pin the connection to a vetted IP before issuing the GET.

        The vetted IP is bound at the socket layer via a custom
        :class:`requests.adapters.HTTPAdapter` whose pool carries a
        ``source_address`` equal to ``resolved.ips[0]``. The original
        hostname is preserved in the URL so SNI / cert validation / CDN
        vhost routing still work AND so unit tests that monkeypatch
        ``requests.get`` with a URL-equality fake keep matching on the
        original URL.

        If the pinned adapter cannot be constructed (requests missing /
        resolve failed) the call degrades to a plain ``requests.get``
        against the original URL — still SSRF-safe because the caller
        already ran :func:`ensure_public`.
        """
        verify = kwargs.get("verify", True)
        session = None
        try:
            resolved = cls._ensure_public_opt(url)
            # Only build a pinned session when we actually have a vetted
            # IP to bind to. When the resolver/mock returns a non-
            # ResolvedHost value we fall back to the module-level
            # ``requests.get`` so existing monkeypatches keep working
            # (and the call is still SSRF-guarded by the pre-check above).
            if resolved is not None and getattr(resolved, "ips", None):
                session = make_pinned_session(resolved, verify=verify)
                # make_pinned_session may return None if requests is
                # unavailable; treat that as "no pinning".
                if session is None:
                    session = None
            else:
                session = None
        except SSRFBlocked:
            raise
        except Exception:
            session = None
        try:
            if "proxies" not in kwargs:
                proxies = requests_proxies()
                if proxies:
                    kwargs["proxies"] = proxies
            allow_redirects = kwargs.pop("allow_redirects", False)
            if session is not None:
                return session.get(url, allow_redirects=allow_redirects, **kwargs)
            return requests.get(url, allow_redirects=allow_redirects, **kwargs)
        except TypeError as exc:
            if "allow_redirects" not in str(exc):
                raise
            if session is not None:
                return session.get(url, **kwargs)
            return requests.get(url, **kwargs)

    @classmethod
    def _follow_redirects(cls, response, url: str, *, max_redirects: int = 5, **kwargs):
        current_response = response
        current_url = url
        for redirect_count in range(max_redirects + 1):
            status_code = getattr(current_response, "status_code", None)
            if status_code not in {301, 302, 303, 307, 308}:
                return current_response

            headers = getattr(current_response, "headers", {}) or {}
            location = headers.get("Location") or headers.get("location")
            if not location:
                return current_response
            if redirect_count >= max_redirects:
                raise requests.TooManyRedirects(f"Exceeded {max_redirects} redirects")

            next_url = urljoin(current_url, str(location).strip())
            # F-01 per-hop re-validation: each redirect target is re-
            # resolved + re-pinned so a rebinding 30x cannot pivot the
            # connection to a private address.
            cls._ensure_public_opt(next_url)
            close_response = getattr(current_response, "close", None)
            if callable(close_response):
                close_response()
            current_url = next_url
            current_response = cls._get_no_redirects(current_url, **kwargs)
        return current_response

    @classmethod
    def _fetch_segment_probe(cls, url: str, *, headers: dict, timeout: int, verify: bool):
        """Fetch one segment probe request with redirect re-validation."""
        response = cls._get_no_redirects(
            url,
            headers=headers,
            timeout=timeout,
            verify=verify,
            stream=True,
        )
        return cls._follow_redirects(
            response,
            url,
            headers=headers,
            timeout=timeout,
            verify=verify,
            stream=True,
        )

    @classmethod
    def _is_range_probe_retryable(cls, exc: Exception, status_code: int | None = None) -> bool:
        """Return True when a segment Range probe should be retried without Range."""
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        effective_status = status_code if status_code is not None else response_status
        if effective_status in {400, 403, 405, 416}:
            return True
        text = str(exc).lower()
        return any(
            keyword in text
            for keyword in (
                "requested range not satisfiable",
                "range not satisfiable",
                "invalid range",
                "range header",
                "http 400",
                "http 405",
                "http 416",
            )
        )

    @classmethod
    def _is_soft_key_failure(cls, exc: Exception, status_code: int | None = None) -> bool:
        """Return True for key probe failures that may be transient CDN rate limits."""
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        effective_status = status_code if status_code is not None else response_status
        if effective_status in cls.SOFT_KEY_STATUS_CODES:
            return True

        text = str(exc).lower()
        return any(keyword in text for keyword in cls.SOFT_RATE_LIMIT_ERROR_KEYWORDS)

    @classmethod
    def _is_soft_segment_failure(cls, exc: Exception, status_code: int | None = None) -> bool:
        """Return True for segment-only probe failures that should not block engine execution."""
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        effective_status = status_code if status_code is not None else response_status
        if effective_status in cls.SOFT_SEGMENT_STATUS_CODES:
            return True

        text = str(exc).lower()
        return any(keyword in text for keyword in cls.SOFT_SEGMENT_ERROR_KEYWORDS)

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

    @staticmethod
    def _estimate_bytes_from_playlist(playlist_text: str) -> Optional[int]:
        """F-08: conservative byte estimate for a media playlist.

        Combines ``#EXT-X-TARGETDURATION`` (max segment seconds) with
        the number of ``#EXTINF`` segments and a conservative bitrate
        assumption to produce an upper-bound-ish byte count without an
        extra network round-trip per segment. The bitrate default (2
        Mbps) errs on the high side so the disk precheck is more likely
        to flag a genuinely tight disk than to silently under-estimate.
        Live/sliding-window playlists (no ``#EXT-X-ENDLIST``) are
        treated as unbounded → ``None`` (caller falls back to default).

        Returns ``None`` when the inputs are missing/unparseable.
        """
        if not playlist_text:
            return None
        text = playlist_text or ""
        # Live playlists grow without bound; do not estimate.
        if "#EXT-X-ENDLIST" not in text:
            return None

        # Target duration (seconds). Fall back to the max EXTINF value
        # when the header is absent.
        target_duration = None
        m = re.search(r"#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)", text)
        if m:
            try:
                target_duration = float(m.group(1))
            except ValueError:
                target_duration = None
        if target_duration is None:
            durs = [
                float(x)
                for x in re.findall(r"#EXTINF:(\d+(?:\.\d+)?)", text)
            ]
            target_duration = max(durs) if durs else None
        if target_duration is None or target_duration <= 0:
            return None

        segment_count = len(re.findall(r"#EXTINF:", text))
        if segment_count <= 0:
            return None

        # Conservative default bitrate: 2 Mbit/s (covers 1080p H.264
        # with headroom). Could be refined by BANDWIDTH from a master
        # playlist but the probe operates on the media playlist.
        bitrate_bps = 2_000_000  # 2 Mbps in bits/s
        bytes_per_segment = (bitrate_bps / 8) * target_duration
        estimated = int(bytes_per_segment * segment_count)
        return estimated if estimated > 0 else None
