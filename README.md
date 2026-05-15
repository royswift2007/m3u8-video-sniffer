# M3U8D v0.4.1

<p align="leftr">
  <b>English</b> | <a href="README_ZH.md">简体中文</a>
</p>

> A Windows desktop tool for streaming-media sniffing, parsing, and downloading. Captures media resources from real browser sessions, dispatches them to multiple download engines, and manages the full lifecycle from discovery to completion.

<p align="center">
  <img src="images/download%20center.jpg" width="800" alt="Main Interface">
</p>

<p align="center">
  <img src="images/brower%20workbench.jpg" width="400" alt="Browser Workbench" style="display: inline-block; margin-right: 10px;">
  <img src="images/resource%20list.jpg" width="400" alt="Resource List" style="display: inline-block;">
</p>

## Overview

M3U8D is a Windows desktop application built with Python and PyQt6. It unifies the following tasks into one workflow:

- Open real web pages in a persistent browser session with login state and cookies
- Automatically discover m3u8 / mpd / mp4 / webm / magnet and other media candidates during playback
- Filter, select quality, and assign download engines from the resource list
- Execute downloads with multiple engines and manage queue, retries, and history
- Receive resources from external browser extensions or scripts via local HTTP API and `m3u8dl://` protocol

## Key Features

### Browser Workbench and Resource Sniffing

- Launches a real persistent Chrome via Playwright (not an embedded web control)
- Preserves login state, cookies, and extensions across sessions
- Four discovery paths: page-URL pattern matching, request interception, response Content-Type detection, and injected frontend script callbacks
- Capture window mechanism for delayed / dynamically-injected media links
- Reduces automation detection with `--disable-blink-features=AutomationControlled`

### Resource List and Filtering

- Multi-layer deduplication (URL, video ID, itag, title, variant)
- Search by title / URL / source text
- Filter by type (M3U8 / MPD / MP4 / FLV / MKV / WEBM / TS), source domain, and resolution
- Automatic M3U8 master playlist parsing and variant expansion
- Batch download, batch removal, and clear operations

### Multi-Engine Download

Five download engines work together:

| Engine | Best For |
|--------|----------|
| N_m3u8DL-RE | m3u8 / mpd / HLS / DASH with quality selection |
| yt-dlp | YouTube / Bilibili / TikTok / Instagram / Twitter / Vimeo and page-based sites |
| Streamlink | Live streams (Twitch / Douyu / Huya / Bilibili Live) |
| Aria2 | Direct-link files and magnet links |
| FFmpeg | Post-processing (remux, merge, subtitle extraction) |

Engine selection priority: user preference → extension-based detection → MIME probe → live-platform list → yt-dlp fallback. Rules are externalized in `resources/engine_rules.json`.

### Download Management

- Idempotent enqueue: duplicate requests are merged, not stacked
- Disk-space precheck before enqueue (1.2× estimated size)
- Concurrent worker pool with dynamic adjustment (soft exit on shrink)
- HLS preflight probe (key URL + first segment reachability)
- Retry with backoff, engine fallback chain, and auth-retry-first strategy
- Stop response within 2 seconds (500ms read-loop exit + 1.5s recursive kill)
- Clear feedback: `queued` / `merged` / `needs_confirmation` / `failed`

### Component Manager

- Manages all five engines: check local version, check remote updates, one-click update
- Three-layer sha256 verification: static pin, dynamic sidecar (`sha256_url`), Trust-on-First-Use (TOFU via `~/.m3u8d/component_pins.json`)
- Staging directory isolation → pre-install sha256 re-verify → atomic replace with `.bak` rollback
- Post-install version cross-check with relaxed prefix matching
- Per-2% progress updates for large downloads (FFmpeg ~130 MB)
- No silent background installs; read-only check on startup, user-confirmed updates only

### External Integration

**CatCatch Local HTTP Service:**
- Binds strictly to `127.0.0.1:9527` (fallback 9528–9539)
- Session token authentication (`X-Session-Token` + Origin allowlist)
- SSRF filtering: rejects private / loopback / link-local / cloud-metadata URLs
- Request body limit: 64 KiB (413 on exceed)
- `GET /download` disabled (405); all downloads via authenticated `POST /download`
- Internal `_`-prefixed headers stripped from external payloads

**Protocol Handler (`m3u8dl://`):**
- Reads `~/.m3u8d/session.token` and hands off to running instance via authenticated POST
- Falls back to launching a new instance only when no running instance responds
- Log redaction: tokens and sensitive query params never written in plaintext

### Security and Privacy

- All engine command lines use parameterized arrays (no string concatenation)
- Headers forwarded to engines are allowlisted: Referer / User-Agent / Origin / Cookie / Accept-Language only
- yt-dlp `format_id` validated against `[A-Za-z0-9_.+:\-]+` (shell metacharacters rejected)
- Download history (`history.json`) strips Cookie / Authorization / X-Session-Token before writing
- Log redaction: 28 sensitive query-key patterns (OAuth / AWS / GCS / CloudFront / Azure tokens)
- Debug-sensitive log (`SECURITY_DEBUG=1`) isolated and disabled by default

## Runtime Environment

| Item | Requirement |
|------|-------------|
| OS | Windows 10/11 64-bit |
| Python | 3.9+ |
| GUI | PyQt6 + PyQt6-WebEngine |
| Browser | Google Chrome installed on the system |
| Network | Access to GitHub and common media sites |
| Disk | At least 500 MB, 2 GB+ recommended |

**Important:** The built-in browser depends on system-installed Chrome. `playwright install chromium` alone is not a substitute.

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare browser and external tools

- Ensure Google Chrome is installed and can launch normally
- Ensure required engines exist under `bin/`, or run `scripts/download_tools.bat`

### 3. Start the application

```bash
python mvs.pyw
```

Or the lighter development entry:

```bash
python main.py
```

### 4. (Optional) Register the protocol handler

```bash
scripts\register_protocol.bat
```

This enables `m3u8dl://` links from browser extensions (e.g., CatCatch) to be received by M3U8D.

## Command-Line Parameters

| Parameter | Description |
|-----------|-------------|
| `--url` | Video or page URL (http/https only, max 4096 chars, SSRF-filtered) |
| `--headers` | JSON string with request headers (allowlisted fields only) |
| `--filename` | Default filename (Windows-safe sanitized, max 240 bytes path) |

## Dependencies

### Python

Listed in `requirements.txt`:
- PyQt6 ≥ 6.6.0
- PyQt6-WebEngine ≥ 6.6.0
- plyer ≥ 2.1.0
- requests ≥ 2.31.0
- playwright ≥ 1.40.0

### External Engines

Declared in `deps.json`:
- **Required:** yt-dlp, N_m3u8DL-RE, FFmpeg
- **Recommended:** aria2c, Streamlink
- **Optional:** Deno

## Packaging and Installation

| Step | Tool | Entry |
|------|------|-------|
| Build | PyInstaller | `build_pyinstaller.py` / `build_pyinstaller.bat` |
| Installer | Inno Setup | `installer/M3U8D.iss` |
| Output | | `installer/output/M3U8D-Setup v0.4.1.exe` |

The installer can:
- Install the main application bundle and protocol handler
- Optionally download required / recommended engines post-install
- Optionally register the `m3u8dl://` protocol

## Project Structure

```text
M3U8D/
├── mvs.pyw                    # recommended entry (packaging-aligned)
├── main.py                    # development entry
├── protocol_handler.pyw       # m3u8dl:// protocol handler
├── config.json                # application configuration
├── deps.json                  # external dependency manifest
├── core/                      # sniffing, download management, component updates
│   └── download/              # modular download manager (queue, workers, classifier)
├── engines/                   # download-engine adapters
├── ui/                        # PyQt6 GUI layer
├── utils/                     # logging, i18n, config, redaction, path sanitization
├── resources/                 # icons, manuals, engine_rules.json
├── scripts/                   # protocol registration, dependency download
├── tests/                     # automated tests
├── installer/                 # Inno Setup installer
├── bin/                       # external engine binaries
├── logs/                      # runtime logs (auto-rotated)
└── cookies/                   # cookie storage per domain
```

## FAQ

### The browser starts but some videos won't play

The Playwright-driven Chrome runs with different flags than your daily browser. Common causes:
- Widevine DRM CDM not loaded (check `chrome://components` in the built-in browser)
- GPU/hardware decode restricted in automation mode
- Some anti-bot systems detect the automation environment

**Workaround:** Use your system browser + CatCatch extension to capture the resource URL, then let M3U8D download it via the protocol handler or HTTP API.

### Protocol handler launches a new instance instead of reusing the running one

- Ensure `scripts\register_protocol.bat` was executed after the latest install
- Check that `~/.m3u8d/session.token` exists and is readable
- Verify ports 9527–9539 are not blocked by firewall

### Component update seems stuck

Large components (FFmpeg ~130 MB) take several minutes. The UI shows per-2% progress updates. The total timeout is 10 minutes per HTTPS request. If truly stuck (zero speed for extended time), click Retry in Component Manager.

## Compliance and Open-Source Notice

See [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md).

- This project is for technical research, learning, and lawful personal use
- Open-sourcing does not grant any rights over target-site content
- Users must verify site terms, copyright, and local laws
- Third-party engines have their own licenses
- Must not be used for infringement, bulk abuse, or illegal activity

## Related Documents

- [INSTALL.md](INSTALL.md) — installation and environment setup
- [CHANGELOG_v0.4.1.md](CHANGELOG_v0.4.1.md) — detailed changelog from v0.3.1
- [resources/manual_zh.md](resources/manual_zh.md) — detailed Chinese user manual
- [resources/manual_en.md](resources/manual_en.md) — detailed English user manual

## License

See [LICENSE](LICENSE). Before redistributing binaries or installers, verify the licenses of all bundled third-party tools.
