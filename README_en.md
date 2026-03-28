# m3u8 video sniffer v0.2.0

> A Windows desktop tool for streaming-media sniffing, parsing, downloading, protocol handoff, and installer-based distribution. The current release already provides a full workflow from browser capture to queue-based downloading and packaged installation.

## Overview

M3U8D is a Windows desktop application built with Python and PyQt6. Its goal is to unify the following tasks into one workflow:

- open a real web page
- preserve login state and cookies
- capture media requests
- choose a proper download engine
- manage download tasks
- distribute the app through a proper Windows installer

Based on the current 0.2.0 codebase, the project already includes:

- a real Playwright-driven browser workflow instead of relying only on a simplified web view
- capture support for `m3u8`, `mpd`, `mp4`, `webm`, and related media candidates
- automatic / user-preferred engine selection for download dispatch
- download queue management, runtime logs, history, retry handling, and basic metrics
- local HTTP receiving plus `m3u8dl://` protocol integration for external browser / extension handoff
- a complete [`PyInstaller`](build_pyinstaller.py) + [`Inno Setup`](installer/M3U8D.iss) packaging chain

The current desktop packaging entry uses [`mvs.pyw`](mvs.pyw:32), while [`main.py`](main.py:36) remains available as a source entry. The main application window is centered in [`ui/main_window.py`](ui/main_window.py:31).

## Version 0.2.0 Status

The installer version is now defined as `0.2.0` in [`installer/M3U8D.iss`](installer/M3U8D.iss:2).

From the latest code, 0.2.0 specifically includes:

- startup checks for missing required dependencies, with optional guided installation, see [`mvs.pyw`](mvs.pyw:60)
- three main tabs in the main window: browser workspace, resource list, and download center, see [`ui/main_window.py`](ui/main_window.py:146)
- integration of `yt-dlp`, `N_m3u8DL-RE`, `Streamlink`, `Aria2`, and `FFmpeg`, see [`ui/main_window.py`](ui/main_window.py:64)
- a local CatCatch server that starts at port `9527` and falls back to `9528-9539` if needed, see [`core/catcatch_server.py`](core/catcatch_server.py:181)
- a protocol handler that accepts JSON payloads, command-style payloads, and plain URL payloads, see [`protocol_handler.pyw`](protocol_handler.pyw:99)
- installer-side options to download required dependencies, recommended dependencies, and register the `m3u8dl://` protocol after installation, see [`installer/M3U8D.iss`](installer/M3U8D.iss:68)

## Core Features

### 1. Browser Workspace and Resource Sniffing

The current browser workflow is mainly built around [`PlaywrightDriver`](core/playwright_driver.py) and [`BrowserView`](ui/browser_view.py:27).

It currently supports:

- launching a real browser session with persistent login state and cookies
- navigating directly from the address bar, see [`ui/main_window.py`](ui/main_window.py:195)
- caching a pending URL until the browser becomes ready, see [`ui/browser_view.py`](ui/browser_view.py:164)
- forwarding captured resources into the sniffer and resource list, see [`ui/browser_view.py`](ui/browser_view.py:185)
- adding `--disable-blink-features=AutomationControlled` at startup to reduce automation detection, see [`main.py`](main.py:28) and [`mvs.pyw`](mvs.pyw:37)

One important limitation should be documented clearly: the New Tab action can trigger navigation, but [`BrowserView.back()`](ui/browser_view.py:249), [`BrowserView.forward()`](ui/browser_view.py:250), and [`BrowserView.reload()`](ui/browser_view.py:251) are still placeholders rather than fully implemented browser controls.

### 2. Resource List and Filtering

The resource list is managed by [`ResourcePanel`](ui/resource_panel.py:14). In its current form, it supports:

- automatic deduplication and candidate collection, see [`ui/resource_panel.py`](ui/resource_panel.py:249)
- search, type filter, source filter, and quality filter, see [`ui/resource_panel.py`](ui/resource_panel.py:111)
- batch download, batch removal, and clear-list operations, see [`ui/resource_panel.py`](ui/resource_panel.py:87)
- a table showing filename, type, quality, source, suggested engine, detection time, and action button, see [`ui/resource_panel.py`](ui/resource_panel.py:141)

This makes the current version suitable for real-world cases where multiple candidate streams appear only after playback begins.

### 3. Multi-Engine Download Workflow

The main window loads external engines from configuration at startup, see [`ui/main_window.py`](ui/main_window.py:64).

The current engine set includes:

- `N_m3u8DL-RE`
- `yt-dlp`
- `Streamlink`
- `Aria2`
- `FFmpeg` for post-processing

Dependency declarations are stored in [`deps.json`](deps.json:1):

- required: `yt-dlp`, `N_m3u8DL-RE`, `FFmpeg`
- recommended: `aria2c`, `Streamlink`
- optional: `Deno`

When a task enters the queue, the manager chooses an engine based on the URL and optional user preference, see [`DownloadManager.add_task()`](core/download_manager.py:48) and [`core/engine_selector.py`](core/engine_selector.py).

### 4. Download Queue, Logs, and History

The download center is mainly powered by [`DownloadQueuePanel`](ui/download_queue.py:20) and [`DownloadManager`](core/download_manager.py:24).

Current capabilities include:

- queue states such as waiting, downloading, paused, failed, and completed
- task operations such as pause, resume, stop, delete, retry, and open folder, see [`ui/download_queue.py`](ui/download_queue.py:90)
- concurrent worker-based task execution, see [`DownloadManager._start_workers()`](core/download_manager.py:171)
- failure classification, rough failure-stage detection, retries, and status updates, see [`core/download_manager.py`](core/download_manager.py:201)
- integrated runtime logs, download history, and queue views through the main UI, see [`ui/main_window.py`](ui/main_window.py:244)

### 5. External Integration: CatCatch + Protocol Handler

The project currently supports two external entry paths:

1. local HTTP API delivery
2. `m3u8dl://` protocol delivery

Relevant components:

- local HTTP server: [`CatCatchServer`](core/catcatch_server.py:162)
- API endpoints such as `/download` and `/status`: [`core/catcatch_server.py`](core/catcatch_server.py:48)
- protocol handler implementation: [`protocol_handler.pyw`](protocol_handler.pyw)
- protocol registration script: [`scripts/register_protocol.bat`](scripts/register_protocol.bat)
- protocol unregistration script: [`scripts/uninstall_protocol.bat`](scripts/uninstall_protocol.bat)

This allows browser extensions, external scripts, or other tools to push already-discovered links into M3U8D.

### 6. Installer-Based Distribution

Version 0.2.0 keeps the existing packaging approach:

- PyInstaller build entry: [`build_pyinstaller.py`](build_pyinstaller.py)
- batch wrapper: [`build_pyinstaller.bat`](build_pyinstaller.bat)
- installer script: [`installer/M3U8D.iss`](installer/M3U8D.iss)
- final installer output: [`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)

This is not a tiny bare executable. It is a proper installer package with an installation UI and post-install actions.

The current installer can:

- install the main one-dir application bundle
- install the separate protocol-handler one-dir bundle
- optionally download required and recommended dependencies after installation
- optionally register the `m3u8dl://` protocol after installation

## Typical Workflows

### Workflow A: Open a page inside the app and capture resources

1. Start the application.
2. Open the browser workspace.
3. Start the browser.
4. Enter the target page URL.
5. Play the video, switch quality, or log in.
6. Filter captured candidates in the resource list.
7. Select a resource and send it to the download queue.
8. Monitor progress and logs in the download center.

Best suited for:

- cases where the real media URL appears only after page execution
- sites that require cookies or login state
- cases where you must choose among multiple stream variants manually

### Workflow B: Push links from CatCatch or external scripts

1. An external tool sends `url`, `headers`, and `filename` to the local HTTP service.
2. Or it launches M3U8D through the `m3u8dl://` protocol.
3. The app inserts the resource into the resource list.
4. The user confirms and downloads it.

Related handling can be found in [`mvs.pyw`](mvs.pyw:97), [`main.py`](main.py:63), and [`protocol_handler.pyw`](protocol_handler.pyw:175).

### Workflow C: Paste a known media URL directly

If you already have a direct `m3u8`, `mpd`, or `mp4` URL, you can send it into the app and let the engine-selection logic handle the rest.

## Runtime Environment

Recommended environment:

| Item | Requirement |
| --- | --- |
| OS | Windows 10/11 64-bit |
| Python | 3.9+ |
| GUI stack | `PyQt6` + `PyQt6-WebEngine` |
| Browser | Google Chrome installed on the system is recommended |
| Network | Access to GitHub and common media sites is recommended |
| Disk space | At least 500MB, 2GB+ recommended |

Important notes:

- the built-in browser workflow strongly depends on a system-installed Chrome; see [`resources/manual_zh.md`](resources/manual_zh.md) and [`resources/manual_en.md`](resources/manual_en.md)
- installing `playwright install chromium` alone is not equivalent to having the full Chrome-based workflow available
- if key external engines are missing, the app may still start, but download capability will be reduced significantly

## Dependencies

### Python Dependencies

Core Python packages are listed in [`requirements.txt`](requirements.txt):

- `PyQt6>=6.6.0`
- `PyQt6-WebEngine>=6.6.0`
- `plyer>=2.1.0`
- `requests>=2.31.0`
- `playwright>=1.40.0`

### External Binary Dependencies

The dependency manifest is defined in [`deps.json`](deps.json:1), and the download flow is implemented by [`scripts/download_dependencies.py`](scripts/download_dependencies.py) and [`scripts/download_tools.bat`](scripts/download_tools.bat:1).

Default categories:

- required: `yt-dlp`, `N_m3u8DL-RE`, `FFmpeg`
- recommended: `aria2c`, `Streamlink`
- optional: `Deno`

In the installer flow, post-install dependency download is triggered through [`download_tools.bat`](scripts/download_tools.bat:1), as configured in [`installer/M3U8D.iss`](installer/M3U8D.iss:68).

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare browser and external tools

- make sure Google Chrome is installed on the system
- make sure required files exist under [`bin/`](bin), or run [`scripts/download_tools.bat`](scripts/download_tools.bat)

### 3. Start the application

The recommended source entry aligned with the current packaging flow is:

```bash
python mvs.pyw
```

You can also use the lighter source entry:

```bash
python main.py
```

Notes:

- [`mvs.pyw`](mvs.pyw:32) includes runtime-directory initialization plus required-dependency checking
- [`main.py`](main.py:36) remains useful as a direct source entry for development or debugging

## Command-Line Parameters

Current entry points support:

- `--url`
- `--headers`
- `--filename`

Argument parsing is implemented in [`mvs.pyw`](mvs.pyw:23) and [`main.py`](main.py:19).

Typical use cases:

- link handoff from the protocol handler
- resource injection from external scripts
- debugging by directly pushing a target resource into the GUI

## Packaging and Installation

### Source Execution

- primary entry: [`mvs.pyw`](mvs.pyw)
- alternate entry: [`main.py`](main.py)

### PyInstaller Packaging

- build script: [`build_pyinstaller.py`](build_pyinstaller.py)
- batch wrapper: [`build_pyinstaller.bat`](build_pyinstaller.bat)
- spec output directory: [`build/pyinstaller/spec/`](build/pyinstaller/spec)

### Inno Setup Installer

- installer script: [`installer/M3U8D.iss`](installer/M3U8D.iss)
- installer output directory: [`installer/output/`](installer/output)
- current installer: [`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)

## Project Structure

```text
M3U8D/
├── mvs.pyw                    # current packaging entry / recommended source entry
├── main.py                    # alternate source entry
├── protocol_handler.pyw       # m3u8dl:// protocol handler
├── config.json                # application configuration
├── deps.json                  # external dependency manifest
├── core/                      # sniffing, dependency checks, download management, task model
├── engines/                   # download-engine adapters
├── ui/                        # PyQt6 GUI layer
├── utils/                     # logging, i18n, config, notifications
├── resources/                 # icons, built-in manuals, UI assets
├── scripts/                   # protocol registration, dependency download, test scripts
├── tests/                     # automated tests
├── installer/                 # Inno Setup installer and output directory
├── build/                     # PyInstaller intermediate artifacts
├── logs/                      # runtime logs
├── cookies/                   # cookie storage
└── plans/                     # plans, reports, and design notes
```

Good starting points for reading the code:

- main window: [`MainWindow`](ui/main_window.py:31)
- browser control: [`BrowserView`](ui/browser_view.py:27)
- download management: [`DownloadManager`](core/download_manager.py:24)
- protocol integration: [`protocol_handler.pyw`](protocol_handler.pyw)
- local HTTP integration: [`CatCatchServer`](core/catcatch_server.py:162)

## FAQ

### 1. The app starts, but the browser workflow is unstable

Check the following first:

- whether Chrome is installed and can launch normally
- whether the target site exposes the real media URL only after login or playback
- whether playback or quality switching actually triggered network requests
- whether required external engines are installed correctly

### 2. Why does the installer still ask to download dependencies?

Because the installer currently ships the application bundle and runtime structure, while external download engines are completed through the post-install dependency-download step. See [`installer/M3U8D.iss`](installer/M3U8D.iss:68) and [`scripts/download_tools.bat`](scripts/download_tools.bat:10).

### 3. Why is [`mvs.pyw`](mvs.pyw) recommended over only using [`main.py`](main.py)?

Because the latest packaging path and protocol handoff flow are closer to [`mvs.pyw`](mvs.pyw:32), and it also performs runtime-directory initialization plus required-dependency checks.

### 4. What should I check if protocol integration fails?

Check:

- whether [`scripts/register_protocol.bat`](scripts/register_protocol.bat) was executed
- whether the packaged `protocol_handler` bundle was deployed correctly
- whether the local CatCatch service is running
- whether ports `9527-9539` are blocked by firewall or occupied by other processes

### 5. Does the project now support full installer-based distribution?

Yes. The repository already contains the full [`PyInstaller`](build_pyinstaller.py) + [`Inno Setup`](installer/M3U8D.iss) chain and the installer output [`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe).

## Compliance and Open-Source Notice

Please read [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md).

In particular:

- this project is intended for technical research, learning, development validation, and lawful personal use
- open-sourcing the project does not imply any authorization over target-site content
- users are responsible for checking site terms, copyright limits, and local laws
- third-party download engines have their own licenses and redistribution rules
- the project must not be used for infringement, abusive bulk access, bypassing restrictions, or any illegal activity

## Related Documents

- [`INSTALL.md`](INSTALL.md): installation, environment preparation, troubleshooting
- [`MANUAL.md`](MANUAL.md): Chinese user manual
- [`resources/manual_zh.md`](resources/manual_zh.md): detailed Chinese built-in manual source
- [`resources/manual_en.md`](resources/manual_en.md): detailed English built-in manual source
- [`UNINSTALL.md`](UNINSTALL.md): uninstall guide
- [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md): compliance boundary and open-source notice

---

## License

The repository currently includes [`LICENSE`](LICENSE) and [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md). Before redistributing binaries, installers, or releases, also verify the licenses and redistribution requirements of all bundled third-party tools.
