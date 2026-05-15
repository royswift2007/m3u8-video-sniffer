# M3U8D v0.4.1 Changelog

> Release date: 2026-05-14
> Previous version: v0.3.1

This release is a **security hardening + stability fix** release. It contains no new features; all changes are fixes and improvements.

---

## 🔒 Security Hardening

### Component Update Pipeline

- Component updates now **require sha256 verification**; components without checksum information are refused (`missing_checksum`)
- Three-layer sha256 sources: static pin (`deps.json`), dynamic sidecar (`sha256_url`), Trust-on-First-Use / TOFU (`component_pins.json`)
- All five backend engines (yt-dlp / N_m3u8DL-RE / FFmpeg / aria2 / streamlink) are now covered by automatic verification
- Pre-install sha256 re-verification of staged artifacts to detect post-download tampering (`staging_tampered`)
- Post-install version cross-check (relaxed prefix matching); automatic rollback to `.bak` on mismatch

### CatCatch Local HTTP Service

- Added one-time session token authentication; `POST /download` now requires `X-Session-Token` header
- Origin / Referer allowlist enforcement; non-loopback origins receive 403
- `GET /download` endpoint disabled — now returns **405 Method Not Allowed** to prevent auth bypass
- Request body cap of **64 KiB**; exceeding it returns **413 Request Entity Too Large**
- SSRF filtering (`ensure_public()`): private / loopback / link-local / cloud-metadata addresses return 400
- External `_`-prefixed headers (e.g. `_cookie_file`) are stripped from incoming payloads to prevent path injection

### Protocol Handler (`protocol_handler.pyw`)

- The protocol handler now reads `~/.m3u8d/session.token` and sends `X-Session-Token` + `Origin: http://127.0.0.1` when handing off to a running instance
- Successful handoff reuses the existing instance; a new process is launched only when no instance responds
- Log redaction: tokens and sensitive URL query parameters are replaced with `<redacted>` before writing to disk
- JSON payloads also go through `_`-prefixed header stripping

### Logging and Privacy

- Sensitive headers in command lines and URLs (Cookie / Authorization / X-Session-Token, etc.) are automatically redacted before writing to log files
- `SENSITIVE_QUERY_KEYS` expanded to 28 patterns (covering OAuth / AWS / GCS / CloudFront / Azure token parameters)
- Download history (`history.json`) strips Cookie / Set-Cookie / Authorization / Proxy-Authorization / X-Session-Token and other sensitive fields before writing
- Debug mode (`SECURITY_DEBUG=1`) writes to an isolated `debug.sensitive.log`; disabled by default

### Engine Security

- yt-dlp `format_id` character-set validation: only `[A-Za-z0-9_.+:\-]+` is accepted; shell metacharacters are rejected to prevent command injection
- All engine command lines use parameterized argument arrays; no string concatenation
- Header forwarding allowlist: only Referer / User-Agent / Origin / Cookie / Accept-Language reach the engine

---

## 🛡️ Stability Fixes

### Download Manager

- Idempotent enqueue: duplicate `url + engine + out_dir + title` requests are automatically merged instead of stacking
- Disk-space precheck: free space on the target drive is checked before enqueue (must be ≥ 1.2× estimated size)
- Enqueue result feedback: the UI now clearly shows one of four outcomes — `queued` / `merged` / `needs_confirmation` / `failed`
- Soft exit on concurrency reduction: workers finish their current task before exiting; no in-progress downloads are killed
- Stop response time: all engines exit the read loop within 500 ms after pause/cancel; recursive kill within 1.5 s

### Subprocess and I/O

- Fixed PIPE deadlock: engine output reading switched to non-blocking read loop with timeout protection
- Process tree termination: `_kill_process_tree` recursively terminates child processes to prevent zombie processes
- Cancel during FFmpeg merge: cleans up `.part` / `.tmp` intermediates while preserving already-completed fragments

### Network and Retry

- HLS preflight probe (`hls_probe`): checks key URL + first segment reachability before starting the actual download
- Timeout backoff: timeout-class failures wait with increasing `retry_backoff_seconds` intervals
- Engine fallback chain: on primary engine failure, candidate engines are tried automatically (controlled by `download_engine_fallback`)
- HEAD probe for MIME detection: 2-second timeout; falls back to extension-based detection on failure

### Port and Service

- CatCatch port strategy: if 9527 is occupied, ports 9528–9539 are tried automatically; `port_exhausted` is logged on full failure
- Port startup does not block the UI thread

### Filename and Path

- Windows reserved name filtering (CON / PRN / AUX / NUL / COM1-9 / LPT1-9)
- ASCII control characters, trailing `.` and whitespace are stripped automatically
- Absolute path byte length capped at 240 (leaving room for extensions and temp suffixes)
- Unified `sanitize_title` entry point shared by all path-construction sites

### Log System

- Automatic daily rotation; a new file is created at midnight
- Capacity management: throttled rotation triggers every 1000 entries or 5 seconds when total size exceeds the limit
- High-frequency DEBUG mode does not slow down downloads

---

## 🐛 Bug Fixes

### Protocol Handler Session Handoff

- **Fixed:** After capturing an m3u8 via the CatCatch browser extension, the resource is now correctly forwarded to the already-running instance instead of launching a new process every time

### Resource List Type Filter

- **Fixed:** The format-type filter now works correctly. Container formats (M3U8 / MPD / MP4 / FLV / MKV / WEBM / TS) display their uppercase literal name and are no longer routed through i18n translation, which previously caused filter mismatches

---

## 🔧 Component Manager Enhancements

- All five backend engines (yt-dlp / N_m3u8DL-RE / FFmpeg / aria2 / streamlink) now support automatic update checking and one-click updates
- Large-file download progress: emitted at 2% or 1 MiB granularity to prevent the UI from appearing frozen during FFmpeg's ~130 MB download
- Per-request HTTPS timeout of 10 minutes (`network_timeout=600s`)
- FFmpeg version number is now read from a remote `version_url` instead of being hardcoded
- Post-install version cross-check uses relaxed prefix matching (`8.1.1` is compatible with `8.1.1-essentials_build-www.gyan.dev`)
- Update failures automatically roll back to `.bak`; the `bin` directory is never left in a broken state

---

## 📖 Documentation

- Chinese and English quick manuals updated in sync, covering all user-visible changes above
- New sections: sha256 verification layering, enqueue result feedback, sensitive-header stripping, format_id character set, SSRF filtering

---

## ⚠️ Known Issues

- The "file already exists" check in the Resource List may false-trigger when the filename contains square brackets (e.g. `[1080p]`) due to glob character-class interpretation; will be fixed in the next release
- The built-in browser (Playwright-driven) may fail to play DRM-encrypted videos (Widevine CDM not loaded); use the system browser + CatCatch extension to capture and download instead

---

## Upgrade Notes

- Direct upgrade from v0.3.1; `config.json` is backward-compatible
- On first launch, the Component Manager will automatically create `~/.m3u8d/component_pins.json` (TOFU trust record)
- The protocol handler will automatically create `~/.m3u8d/session.token` (session token); no manual configuration needed
- If CatCatch handoff fails after upgrading, re-run `scripts\register_protocol.bat` to refresh the protocol registration
