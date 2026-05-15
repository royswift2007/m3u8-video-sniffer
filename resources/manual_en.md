M3U8 Video Sniffer Detailed User Manual (Compiled According to the Current Actual Program Code)
> Applies to program version: v0.4.1 (M3U8D)

# Important Before First Run: Runtime Environment and Chrome Requirements

## ✅ Runtime Environment Overview (Please Confirm First)

> [!IMPORTANT]
> The built-in browser in the current program actually depends on a system-installed Google Chrome.
> Installing only `playwright install chromium` is not a substitute.
> If Chrome is not installed, the Browser Workbench, in-page web sniffing, Cookie / login-state reuse, and download capability for some sites will all be significantly affected.

### Environment and Dependency Requirements

| Category | Name | Requirement / Version | Required? | Description |
| :--- | :--- | :--- | :--- | :--- |
| Operating System | Windows | Windows 10 / 11 64-bit | Required | The current project is designed around the Windows desktop environment. |
| Python Runtime | Python | 3.9 or higher | Required | Used to run `main.py` / `mvs.pyw`. |
| Python Package Manager | pip | Must work normally | Required | Required to run `pip install -r requirements.txt` to install dependencies. |
| Python Dependencies | requirements.txt | Must be fully installed | Required | Includes PyQt6 / PyQt6-WebEngine / plyer / requests / playwright. |
| Browser Environment | Google Chrome | Must be installed on the system and launch normally | Mandatory (for built-in browser scenarios) | The current program depends on system Chrome, not `playwright install chromium`. |
| Download Engine | `bin/yt-dlp.exe` | File must exist and be executable | Required | Required for page-based sites and a large number of general sites. |
| Download Engine | `bin/N_m3u8DL-RE.exe` | File must exist and be executable | Required | Core download engine for m3u8 / mpd / HLS / DASH. |
| Download Engine | `bin/ffmpeg.exe` | File must exist and be executable | Required | Required for audio/video merging, remuxing, and some post-processing. |
| Download Engine | `bin/aria2c.exe` | File must exist and be executable | Recommended | More important for direct-link resources, multi-connection downloads, and magnet-link scenarios. |
| Download Engine | `bin/streamlink.exe` | File must exist and be executable | Recommended | More important for live streams / stream replay tasks. |
| Auxiliary Tool | `bin/deno.exe` | File must exist and be executable | Optional | The current main workflow does not hard-depend on it, but it is recommended to keep it. |
| Network Environment | GitHub / common resource sites must be reachable | Stable internet recommended | Recommended | First-time dependency installation, tool downloads, and site parsing rely on network connectivity. |
| Disk Space | Local available space | At least 500MB, 2GB+ recommended | Required | The tools themselves, caches, temporary files, and intermediate merge files all consume disk space. |
| Browser Extension | CatCatch | Install as needed | Optional | Only needed if you want to send resources from Chrome / Edge into the program with one click. |

### If Chrome Is Not Installed

- The Browser Workbench cannot start properly.
- Automatic in-page sniffing will fail.
- Browser Cookie / login-state functionality will fail.
- Real media URLs from some sites cannot be captured.
- Overall download capability will be significantly degraded.

### Conclusion

Google Chrome is not an optional add-on. It is a key prerequisite that affects the built-in browser, sniffing success rate, login-state reuse, and the overall user experience.

### Items That Must Be Satisfied First

Windows 10 / 11 64-bit, Python 3.9+, pip, all dependencies in requirements.txt, Google Chrome, `bin/yt-dlp.exe`, `bin/N_m3u8DL-RE.exe`, `bin/ffmpeg.exe`

### Recommended / Optional Items

- Recommended: `bin/aria2c.exe`, `bin/streamlink.exe`
- Optional: `bin/deno.exe`

================================================================================
1. Program Positioning and Overall Structure
================================================================================
This program is a PyQt6-based desktop video sniffing and downloading tool. Its core goals are:
    1. Open real web pages in a standalone browser while preserving login state, extensions, and normal web interaction.
    2. Automatically discover candidate resources such as m3u8 / mpd / mp4 / webm / magnet links during playback.
    3. Put resources into the resource list so the user can filter them, choose quality, assign an engine, and enqueue them for download.
    4. Execute tasks with multiple download engines while recording logs, history, and retry / failure information.

The program entry points and main execution chain are as follows:
    - main.py
      Responsible for starting `QApplication`, creating the main window, and parsing the `--url` / `--headers` / `--filename` parameters.

    - ui/main_window.py
      Responsible for assembling the browser tab, resource tab, and download-management tab into the main UI and connecting all signals.

    - core/playwright_driver.py
      Responsible for launching persistent Chrome, listening to page navigation, network requests, responses, and results returned by injected frontend scripts.

    - core/m3u8_sniffer.py
      Responsible for receiving discovered resources in a unified way, normalizing headers, applying site rules, deduplicating, and merging context.

    - ui/resource_panel.py
      Responsible for displaying the resource table, filtering, batch operations, and background M3U8 parsing plus variant expansion.

    - core/download_manager.py
      Responsible for the task queue, concurrency, retries, fallback, HLS preflight probing, state transitions, and download metrics.

    - engines/*.py
      Responsible for dispatching tasks to N_m3u8DL-RE, yt-dlp, Streamlink, and Aria2 respectively.

    - ui/download_queue.py / ui/log_panel.py / ui/history_panel.py
      Respectively responsible for task-queue display, key runtime-log display, and download history with re-download support.

    - core/catcatch_server.py + protocol_handler.pyw
      Responsible for integrating with the browser extension and system protocol handler, sending external requests into the program.


================================================================================
2. Startup Methods and External Parameters
================================================================================
2.1 Normal Startup
    The most common startup method:
        python main.py

    After startup, the program will:
        1. Create the main window.
        2. Initialize the configured download engines.
        3. Initialize the download manager and resource sniffer.
        4. Start the local CatCatch HTTP service.
        5. Display the three main tabs: Browser / Resource List / Download Center.

2.2 Startup with Command-Line Parameters
    The current entry point actually supports the following parameters:
        --url
            A video or page URL passed in externally.
            Validation: only `http` / `https` schemes are accepted, and the length must be at most 4096 characters.
            The value is filtered for private-network and cloud-metadata targets (addresses such as
            127/10/172.16-31/192.168/169.254/::1/fc00::/fe80:: and cloud-metadata endpoints like 169.254.169.254 are rejected).
            When validation fails, the program exits with code 2 and does not start the UI.

        --headers
            A JSON string, typically in the following format:
            {"referer":"...","user-agent":"...","cookie":"..."}
            Validation: the headers go through the same sanitation path as the CatCatch HTTP service —
            header names may only contain `[A-Za-z0-9-]` and must be at most 64 characters;
            values must not contain `\r \n \0` and must be at most 4096 characters;
            only the allowlisted fields Referer / User-Agent / Origin / Cookie / Accept-Language are kept,
            and any other fields are silently dropped.

        --filename
            The default filename or title for the incoming resource.
            Validation: Windows reserved names (CON / PRN / AUX / NUL / COM1-9 / LPT1-9),
            ASCII control characters, and trailing `.` / whitespace are stripped automatically;
            the final absolute-path byte length is capped at 240;
            when the value ends up fully empty, it falls back to `media_<timestamp>`.

    Actual execution logic:
        1. Start the main UI normally first.
        2. If `--url` is provided, construct an `M3U8Resource` 500ms later.
        3. The program automatically chooses an engine based on the URL.
        4. The resource is added directly into the “Resource List” tab.
        5. The UI automatically switches to the Resource List tab and waits for the user to continue the download.

    Applicable scenarios:
        - When the protocol handler starts the program and passes a link back.
        - When an external script hands a parsed link to the program.
        - When manually injecting a resource into the GUI during debugging.

2.3 Browser Parameters Automatically Set at Startup
    The program appends the following parameter to the Qt WebEngine / browser environment:
        --disable-blink-features=AutomationControlled

    Purpose:
        Reduce the probability that websites detect an automation environment, thereby improving the success rate of login, playback, and stream capture.


================================================================================
3. Main Interface Overview
================================================================================
The main interface is currently divided into 3 primary tabs:
    1. Browser Workbench
    2. Resource List
    3. Download Center

There are also two entries in the upper-right corner: “Component Manager” and “Quick Manual”:
    - Component Manager: opens the status and update-management window for external download components.
    - Quick Manual: opens this manual.

3.0 Component Manager and Auto-Update Notes
    Entry location:
        The main window now provides a “Component Manager” entry in the upper-right corner, in the same area as “Quick Manual”.

    Information shown:
        The Component Manager lists yt-dlp, N_m3u8DL-RE, FFmpeg, aria2c, and Streamlink, and shows the following information for each component:
            - Local version
            - Latest version
            - Current status (for example installed, missing, update available, check failed, etc.)
            - Local path

    Common actions:
        - Refresh Local Status: rescans the local bin directory and executable files, then updates local version, path, and missing-state information.
        - Check Updates: reads remote release information online. It only checks the latest version and update availability; it does not automatically download or install anything.
        - Update All Updatable: updates all components currently detected as updatable, with confirmation before execution.
        - Per-component Install / Update / Retry: installs, updates, or retries a failed operation for one specific component. This is useful when you only need to fill in a missing component or update a selected component.

    Behavior after startup:
        After startup, the program only performs read-only checks and shows entry/status hints in the upper-right area. It will not automatically download, install, or replace any component in the background.

    Installer and missing components:
        When using the installer, required components (yt-dlp, N_m3u8DL-RE, and FFmpeg) are downloaded by default. Recommended components such as aria2c and Streamlink need to be selected in the installation wizard.
        If a component is later found missing, outdated, or has an abnormal path, open “Component Manager” to install / update it.

    Safety strategy:
        - A second confirmation is required before updating to avoid accidental operations.
        - Downloaded files are first saved into a separate staging directory instead of being written directly into the `bin` directory.
        - Before installation, the update goes through a full verification chain:
            · The manifest must provide either a `sha256` digest or a signature; when both are missing the task fails with `missing_checksum`.
            · After the download finishes, the file is compared byte-by-byte against the manifest's `sha256`; on mismatch the task fails with `checksum_mismatch` and the staged file is removed.
            · When the manifest includes a signature, Authenticode / a pre-shipped public key is used to verify it on Windows.
            · A disk-space precheck runs before replacement (requiring 1.2× the expected size, i.e. a 20% headroom); if there is not enough room the task fails with `insufficient_disk` and the `bin` directory is left untouched.
            · The `sha256` of the staged artifact is re-verified immediately before replacement; on mismatch the task fails with `staging_tampered`.
        - Before replacement, the old file is saved as a `.bak` copy and kept until the next startup; if replacement fails, a rollback from `.bak` is attempted. No failure path modifies the `bin` directory.
        - If a target engine file is being held open by another process, the task is marked `deferred_pending_restart` and will be retried automatically on the next startup.
        - It is recommended to pause or finish active download tasks before updating components, because yt-dlp.exe, N_m3u8DL-RE.exe, ffmpeg.exe, aria2c.exe, streamlink.exe, or similar files may be occupied and fail to be replaced.

    Layered sha256 sources (all five backends can be verified automatically):
        Each backend publishes its releases differently, so the program tries the following sources in order to obtain a comparable sha256. The bottom line "no sha256, no install" is never bypassed:
        - Static pin: a fixed expected digest is written into `deps.json` under `checksum.sha256`. aria2 1.37.0 uses this path (pure local byte-by-byte comparison, fastest and strictest).
        - Dynamic sidecar (`checksum.sha256_url`): the official digest is read in real time from a sidecar file in the same Release.
            · yt-dlp: the official `SHA2-256SUMS` manifest is fetched and the row matching the exact exe filename is picked.
            · FFmpeg (gyan.dev build): the corresponding `.zip.sha256` file is fetched as the expected digest for the archive; after extraction into `bin` the sha256 of the extracted `ffmpeg.exe` is also recomputed.
        - Trust-on-first-use (TOFU): the official releases of N_m3u8DL-RE and streamlink do not ship a standard sha256 sidecar today. The program downloads them only over HTTPS from a strict domain allowlist (`github.com` / `objects.githubusercontent.com` / the matching PyPI CDN), records the measured digest into `~/.m3u8d/component_pins.json` after the first successful install, and on every subsequent update of the same version fails with `pin_mismatch` and rolls back if the freshly downloaded file's sha256 does not match the recorded value. There is no silent overwrite.
        - If none of the three sources above yields a comparable sha256 for a given backend, the task fails immediately with `missing_checksum` and installation is refused (a diagnostic mode can relax this with audit logging, but it is disabled by default).

    Download progress and "does it look stuck?":
        Component archives vary widely in size (yt-dlp ~12 MB, N_m3u8DL-RE ~30 MB, FFmpeg essentials build ~130 MB), so a single download can take anywhere from seconds to several minutes. To keep the UI from looking "frozen" on large archives:
        - During the download phase, progress is emitted at coarser steps of "whichever is larger, 2% advance or 1 MiB accumulated"; the "Downloading" status in the UI keeps ticking instead of sitting on a fixed string.
        - Each individual HTTPS request has a total timeout of 10 minutes (`network_timeout=600s`). A timeout failure is only declared after that threshold is crossed. A short period without new progress below that threshold does not mean failure; wait and observe the network first.
        - If progress stays completely frozen for a long time while the network speed is zero, you can click "Retry" or "Cancel" in the Component Manager. The failure path never corrupts the `bin` directory.

    Post-install version cross-check (relaxed compatibility match):
        - After the file is copied or replaced into `bin`, the program calls the target engine's own `--version` (or an equivalent command) to read back the real version string and compares it against the target version declared by the manifest / remote source. The update is considered "finished" only when the comparison passes.
        - The comparison uses a "compatibility match" rule: leading `v` / `V` and surrounding whitespace are stripped from both sides, and the two strings are considered compatible when one is a prefix of the other AND the next character after the prefix is one of `.` `-` `_` `+` space, or end-of-string.
            · Examples: target `8.1.1` is compatible with engine output `8.1.1-essentials_build-www.gyan.dev`, `ffmpeg version 8.1.1 Copyright ...`, `v8.1.1`, and so on; no false failure.
            · But `8.1.1` is NOT compatible with `8.1.10` (the next character is the digit `0`, which is not in the separator set), and the task fails with `version_mismatch`.
        - As soon as the cross-check reports `version_mismatch`, the previously-kept `.bak` is rolled back immediately so the old version can continue to be used.

    Display tips:
        If a local path, version number, or remote version string is too long, the table may show truncated text. Hover over the corresponding cell to view the full content.

3.1 Composition of the Browser Workbench Tab
    The top toolbar contains:
        - Back button
        - Forward button
        - Refresh button
        - New Tab button
        - Address bar
        - Start Detection button
        - Download Strategy dropdown

    The lower area is divided into left and right sections:
        The left side is the browser-control card, which provides:
            - Start Browser
            - Stop Browser
            - Current explanatory text

        The right side is the driver log area, used to display the browser driver runtime status.

3.2 Composition of the Resource List Tab
    The page mainly includes:
        - Page title and introduction
        - Download Selected / Remove Selected / Clear List
        - Search box
        - Type filter
        - Source filter
        - Resolution filter
        - Resource table

    The current resource-table columns are:
        1. Filename
        2. Type
        3. Resolution
        4. Source domain (the current implementation actually displays the source URL / `page_url` text)
        5. Engine
        6. Detection Time
        7. Action (Download button)

3.3 Composition of the Download Center Tab
    The page contains two major areas:
        A. Download Preferences
            - Save Location
            - Change Location
            - Open Folder
            - Threads
            - Retries
            - Concurrent Tasks
            - Speed Limit

        B. Draggable split area below
            - Upper half: Download Queue
            - Lower half: Runtime Logs / Download History (switch via tabs)


================================================================================
4. Browser Workbench: Features, Implementation, and Usage
================================================================================
4.1 Browser Startup Mechanism
    Implementation:
        The browser is not a web control embedded inside the Qt window. Instead, Playwright launches a real persistent Chrome.
        Its core logic is in `core/playwright_driver.py`.

    Main characteristics:
        - Uses a persistent user-data directory to preserve login state and Cookies.
        - Tries to clean up leftover lock files such as `SingletonLock` at startup.
        - Listens to events such as `page`, `request`, `response`, `download`, and `console`.
        - Supports multiple tabs. When the user opens a new page in Chrome, the sniffing logic is configured automatically.

    Usage:
        1. Open “Browser Workbench”.
        2. Click “Start Browser”.
        3. Wait until the driver log shows “Browser ready”.
        4. Then perform page visits, login, playback, and other actions.

4.2 Address Bar and Start Detection
    Implementation:
        Pressing Enter in the address bar and clicking the “Start Detection” button both trigger the same logic:
            - If the input is a normal URL, it is passed to Playwright for navigation.
            - If the browser is not started, the program starts it first and then navigates automatically.
            - If the input lacks `http` / `https`, the program automatically prepends `https://`.

    Usage:
        1. Enter the site page URL in the address bar.
        2. Press Enter or click “Start Detection”.
        3. The page will open in the external browser.
        4. Perform real actions on the page, such as clicking Play, switching resolution, or logging in.

4.3 Back / Forward / Refresh / New Tab
    Current code status:
        - The “New Tab” button currently calls `add_new_tab()`, whose implementation makes the browser load a new address.
        - The compatibility methods `back()` / `forward()` / `reload()` in `BrowserView` are still placeholder no-op implementations.

    This means:
        - “New Tab” works, but in essence it initiates a navigation request.
        - The Back, Forward, and Refresh buttons already exist visually, but are not yet in a fully implemented state.

4.4 Download Strategy Dropdown
    The actual dropdown items are:
        - Auto Select
        - N_m3u8DL-RE
        - yt-dlp
        - Streamlink
        - Aria2

    Its purpose is not to download immediately. Instead:
        When you click Download on a resource, it tells the program which engine should be preferred.

    Specific rules:
        - If the selected engine can handle the URL, the user-selected engine is used preferentially.
        - If the selected engine cannot handle it, the program falls back to automatic selection.
        - See Section 8 for automatic-selection priority.

4.5 How Browser Sniffing Is Implemented
    The current code actually uses the Playwright sniffing pipeline, not primarily the QWebEngine interceptor.

    There are 4 main discovery paths inside `PlaywrightDriver`:
        1. Page navigation matches a video-page pattern
           For example, page URL rules for YouTube / Bilibili / TikTok / Instagram / Twitch, etc.
           After a match, the program adds the page URL to the list as a resource that yt-dlp can handle.

        2. `request` event
           Intercepts network requests sent by the browser. As long as the URL looks like a video stream, it is recorded.

        3. `response` event
           Even if the URL itself does not look like a video, it is still recorded when the response header `Content-Type` indicates HLS / MP4 / WebM, etc.

        4. Frontend injected script + `console` callback
           The page injects `sniffer_script`, which actively outputs frontend-discovered media addresses to the console during playback,
           and the Python side then receives and stores them.

    In addition, there is also a “capture window” mechanism:
        - When navigation occurs, playback is detected, or a media link is hit, a period of continued probing is started.
        - During this period, the code periodically scans the `video` tag, `source` tag, and the `performance` resource list.
        - This is suitable for capturing media links that appear later, are dynamically injected, or only appear after switching quality.

4.6 Practical Usage Recommendations for the Browser Tab
    Recommended sequence:
        1. Start the browser.
        2. Log in to the target site in the external browser.
        3. Play the video and switch resolution if necessary.
        4. Return to the program and see whether candidate resources appear in the “Resource List”.
        5. If the site is a page-based site such as YouTube / Bilibili / TikTok, a page resource usually appears directly.
        6. If the site is an HLS streaming site, an m3u8 resource or its variant resources usually appear.

4.7 Special Handling for Magnet Links
    Implementation:
        When a link starting with `magnet:?` is entered in the address bar, the browser is not instructed to navigate. Instead, the program directly constructs a resource entry
        and forcibly sets Aria2 as the suggested engine.

    Usage:
        1. Paste the magnet link directly into the address bar.
        2. Press Enter.
        3. The resource enters the Resource List.
        4. Then click Download.


================================================================================
5. Resource List: Features, Implementation, and Usage
================================================================================
5.1 How Resources Enter the List
    After a resource enters the list, it goes through the following processing:
        1. It is received uniformly by `M3U8Sniffer.add_resource()`.
        2. m3u8 request headers are normalized:
            - Header names are converted to lowercase uniformly.
            - `referer` is auto-filled.
            - `user-agent` is auto-filled.
            - `origin` is inferred from `referer` where possible.
        3. If site rules are enabled, `site_rules` is used to auto-fill headers.
        4. Deduplication is performed based on URL, title, and platform features.
        5. A candidate score `candidate_score` is calculated for later download-side link prioritization.
        6. The resource is passed to the main window and the resource table for display via the `on_resource_found` callback.

5.2 Resource Deduplication Logic
    In the current code, deduplication is not a one-size-fits-all URL-only rule, but a multi-layer strategy:
        - Resources with the same URL merge context rather than always being inserted repeatedly.
        - Platforms such as YouTube apply additional deduplication by video ID, `itag`, and title.
        - M3U8 master playlists and media playlists generate separate keys.
        - M3U8 variant resources are distinguished by `height` / `bandwidth` / `variant_url`.

    Purpose:
        Avoid flooding the list with the same video due to multiple CDNs, repeated requests, or page refreshes.

5.3 Search and Filtering
    Currently available filters:
        - Search title, URL, and source text
        - Type filter
        - Source filter
        - Resolution filter

    Usage:
        1. Enter keywords in the search box to search the title, full URL in the tooltip, and source text.
        2. Narrow the range via “All Types / M3U8 / MPD / MP4 ...”. The Type column follows a simple display rule: container formats such as m3u8 / mpd / mp4 / flv / mkv / webm / ts are shown with their uppercase literal name (`M3U8` / `MPD` / `MP4` / `FLV` / `MKV` / `WEBM` / `TS`), while only the three semantic labels `Unknown` / `Video Stream` / `Playlist` go through UI localization, so the dropdown matches the visible text exactly.
        3. Use the source filter to locate resources from a specific page source.
        4. Use options such as 2160 / 1080 / 720 / Audio to filter resolution.

5.4 Download Selected / Remove Selected / Clear List / Clear Temp Files
    Download Selected:
        Executes the download logic one by one for the currently selected multiple rows.

    Remove Selected:
        Removes the selected resources from the UI list and the internal resource array, then rebuilds the deduplication cache.

    Clear List:
        Clears the resource table, deduplication cache, `page_url` mappings, and filter conditions.

    Clear Temp Files:
        A dedicated entry (button or menu item) that only cleans intermediate download artifacts (`.part` / `.tmp` / leftover segments) under `temp_dir`.
        It is no longer performed implicitly by the add-task path, so it will not delete segments of a task you are currently downloading.
        Run it manually when disk space is tight.

5.5 Automatic M3U8 Parsing and Variant Expansion
    When a master m3u8 resource is added to the list, `ResourcePanel` automatically starts the background thread `M3U8FetchThread`:
        - Download the m3u8 content.
        - Determine whether it is a master playlist.
        - Parse `#EXT-X-STREAM-INF`.
        - Recursively parse nested master playlists (limited by `m3u8_nested_depth`).
        - Generate variants for each resolution.

    After parsing completes, two things happen:
        1. The “Resolution” column of the original row is updated, for example: `1080p/720p/480p`.
        2. Each variant is automatically added to the table as a new resource row, and the title gets a suffix such as `[1080p]`.

    This means:
        The user can either download from the original master entry or click a specific resolution variant directly.

5.6 Routing Logic After Clicking Download
    After the user clicks “Download” in the Resource List, the main window performs the following checks according to the actual code:
        A. If it is a platform page supported by yt-dlp
            - Prefer `page_url` instead of a CDN fragment URL.
            - Open the format-selection dialog.
            - The user can select a specific `format_id` or directly choose “Best Quality”.

        B. If it is `.m3u8`
            - Open the M3U8 resolution-selection dialog.
            - Prefer reusing the `variants` already cached in the Resource List to avoid duplicate requests.
            - If the current engine is N_m3u8DL-RE, the program will try to pass `master_url + selected_variant`.
            - If another engine is used, it downloads directly from the selected variant URL.

        C. Other resources
            - Directly create a download task and enqueue it.

    Enqueue result feedback (four possible outcomes after clicking Download):
        Regardless of which branch (A / B / C) is taken, enqueue ultimately goes through the download manager's
        `add_resource`, and the UI surfaces one of the following four explicit outcomes so the user is never left
        wondering whether the click did anything:
            - `queued`: the new task was enqueued normally; a matching row appears in the Download Queue, and you can continue tracking it in the Download Center.
            - `merged`: an identical task already exists (the idempotency key `sha1(url|engine|out_dir|title)` hit a live entry), so this click is merged into the existing task and no second duplicate row is created in the queue. The status bar shows "Merged into existing task".
            - `needs_confirmation`: the pre-enqueue disk precheck failed (by default the target drive must have at least 1.2× the estimated size of free space). The program pops a dialog letting you choose "Continue / Cancel"; choosing Continue is recorded with `disk_precheck=bypassed` for audit. If you make no choice, the task is NOT enqueued so you do not run out of space mid-download.
            - `failed`: the URL / headers were rejected after going through the same sanitation path as the protocol handler (private / cloud-metadata addresses, non-http(s) schemes, over-length URL, over-length or malformed headers, and so on). The status bar shows the reason, and no row appears in the queue.

5.7 How to Use the yt-dlp Format-Selection Dialog
    Implementation:
        Calls `yt-dlp -J` to obtain the format list, then shows it in a popup through `ui/format_dialog.py`.

    Current dialog features:
        - Mainly displays formats of 720p and above.
        - Display columns: ID / Resolution / Format / Codec / Size.
        - Double-click a row to confirm directly.
        - You can click “Best Quality”.

    Usage:
        1. Click “Download” on a supported-site resource.
        2. Wait for the program to obtain formats.
        3. Select a specific resolution row and click “Confirm Download”, or directly click “Best Quality”.

5.8 How to Use the M3U8 Resolution-Selection Dialog
    Usage:
        1. Click “Download” on an m3u8 resource.
        2. If variants are already cached, the dialog opens immediately; otherwise, the playlist is analyzed in the background first.
        3. After selecting a resolution, the program begins creating the download task.


================================================================================
6. Download Center: Features, Implementation, and Usage
================================================================================
6.1 Save Location
    Corresponding configuration item:
        download_dir

    Implementation:
        - The current download directory is displayed in the UI.
        - “Change Location” opens a directory-selection dialog.
        - “Open Folder” opens the directory directly in the system file explorer.
        - After modification, the value is written back to `config.json` immediately.

    Usage:
        1. Enter the Download Center.
        2. Click “Change Location”.
        3. Choose a directory.
        4. Subsequent new tasks will be saved there by default.

6.2 Thread Count
    Corresponding configuration item:
        engines.n_m3u8dl_re.thread_count

    Function:
        Mainly affects the single-task download thread count of N_m3u8DL-RE.

    Notes:
        - This is the thread count of a single task, not the number of tasks downloading simultaneously.
        - It does not apply equivalently and directly to yt-dlp / Streamlink.

    Recommendation:
        - You can increase it appropriately when the network is stable.
        - On some sites, an excessively high thread count may trigger 403 errors, rate limiting, or instability.

6.3 Retry Count
    Corresponds to two levels:
        A. Retry count in the UI
            engines.n_m3u8dl_re.retry_count
            This value is written directly to N_m3u8DL-RE as the engine-internal retry parameter.

        B. Overall retry count in the download manager
            max_retry_attempts
            This value controls how many complete task-level rounds `DownloadManager` can attempt at most.

    Difference:
        - Engine-internal retry: one engine command retries internally by itself.
        - Manager-level retry: after an engine fails, the task can still go through another round and may even switch engines.

6.4 Concurrent Tasks
    Corresponding configuration item:
        max_concurrent_downloads

    Implementation:
        `DownloadManager` starts multiple background worker threads and uses this value to control the number of simultaneously running tasks.

    Usage:
        - Increase it: multiple tasks can download at the same time, but this consumes more bandwidth and disk resources.
        - Decrease it: more stable, suitable for sites prone to 403 errors or unstable networks.

    Dynamic adjustment:
        When concurrency is lowered from N to M (M<N), the most recently started N-M workers receive a soft-exit signal
        and finish naturally once their current task completes. If any of them does not exit within 30 seconds,
        a `worker_exit_timeout` warning is recorded, but tasks that are already downloading are never force-killed.
        Raising concurrency spawns new workers immediately; the `active_workers` indicator in the UI always reflects the real live count.

6.5 Speed Limit
    Corresponding configuration item:
        speed_limit

    Meaning in the UI:
        The unit is always MB/s; 0 means unlimited.

    Actual effect:
        - N_m3u8DL-RE: converted into `--max-download-speed {N}MB`.
        - Aria2: converted into `--max-overall-download-limit={N}M`.
        - yt-dlp: converted into `--limit-rate` (expanded on a MB/s basis).
        - Streamlink currently does not apply a dedicated download-rate limit from this parameter.

    Usage recommendations:
        - When you encounter network fluctuation, disconnects, or certificate / gateway issues, moderate rate limiting may help.
        - When downloading multiple tasks, appropriate rate limiting can improve overall stability.

6.6 Download Queue Display Content
    The current columns are:
        - Filename
        - Status
        - Progress
        - Speed
        - Engine

    Status may include:
        waiting / downloading / paused / failed / completed

    Description:
        - Progress may be unknown for livestream recording tasks; in that case, it may show downloaded size or “Recording...”.
        - The status text and color change according to task state.

6.7 Download Queue Context Menu and Bottom Buttons
    Currently supported:
        - Pause
        - Resume
        - Stop
        - Delete
        - Retry
        - Open Location
        - Pause All
        - Clear Completed
        - Sort by Status
        - Batch Import

    Additional operations supported in the context menu:
        - Copy Link
        - Completed tasks can play the file

    Actual behavior when deleting a task:
        - Notify `DownloadManager` to remove the task.
        - If the process is still running, terminate the download process.
        - Try to clean up temporary files 3 seconds later.
        - Finished output files are not proactively deleted.

6.8 Batch Import
    Supported inputs:
        - `http://`
        - `https://`
        - `magnet:`

    Usage:
        1. Click “Batch Import”.
        2. Enter one link per line.
        3. After confirmation, the program filters invalid items.
        4. Valid items are added to the Resource List in batch instead of being downloaded immediately.


================================================================================
7. Core Download-Management Mechanisms (According to the Actual Code)
================================================================================
7.1 What Is Actually Stored in a Task Object
    Each `DownloadTask` mainly contains:
        - url
        - save_dir
        - filename
        - headers
        - status
        - progress
        - speed
        - engine
        - error_message
        - downloaded_size
        - selected_variant
        - master_url
        - media_url
        - candidate_scores
        - retry_count / max_retries
        - stop_requested / stop_reason
        - created_at / started_at / completed_at

    This means:
        A task stores not only the final download URL, but also context such as source resolution, master-playlist address, and failure state.

7.2 Engine-Selection Priority
    Combined decision order (highest priority first):
        1. The engine the user manually selected in the UI (highest priority).
        2. File-extension inference (the URL query string is stripped before matching).
        3. MIME probing via HEAD request (2 s timeout, protected by the private-network / cloud-metadata filter;
           on probe failure it falls back to extension inference and records `engine_select=fallback`).
        4. Live-platform list match (`LIVE_PLATFORMS`).
        5. yt-dlp as the final fallback.

    The decision tables (file extensions and live-platform list) are externalized in `resources/engine_rules.json`
    and can be edited directly without changing any code.

    The current automatic-selection order is:
        N_m3u8DL-RE -> Streamlink -> Aria2 -> yt-dlp

    Processing tendency:
        - N_m3u8DL-RE: more inclined toward m3u8 / mpd / HLS / DASH.
        - Streamlink: more inclined toward livestream platform URLs.
        - Aria2: more inclined toward direct-link files and magnet links.
        - yt-dlp: serves as the general fallback and page-based video-platform downloader.

7.3 Task Enqueueing and Concurrency
    Implementation:
        `DownloadManager` uses `Queue` + multiple worker threads.

    Flow:
        1. Idempotency precheck: the key `sha1(url|engine|out_dir|title)` is looked up first. If a task with the same key
           already exists, the new request is merged into it (returning `merged`) so repeated "re-download" clicks
           do not stack duplicate entries in the queue.
        2. Disk-space precheck: an estimated size is read from the manifest (falling back to 500 MB when it cannot be read)
           and compared with the free space on the disk that hosts the target directory. When space is insufficient,
           you are prompted with `needs_confirmation=insufficient_disk` and can choose to continue or cancel;
           choosing to bypass the precheck records `disk_precheck=bypassed`.
        3. `add_task()` sets the task status to `waiting`.
        4. It selects the preferred engine according to the current settings and user preference.
        5. Worker threads take tasks for execution according to the concurrency limit.

7.4 HLS Preflight Probe
    Corresponding feature toggles:
        features.hls_probe_enabled
        features.hls_probe_hard_fail

    Implementation:
        For m3u8 tasks, `HLSProbe` is called first:
            - Fetch the playlist.
            - If it is a master playlist, fetch the first variant first.
            - Check the key URL.
            - Check whether the first segment can be accessed.

    Function:
        Detect in advance the case where “the playlist is accessible but the key / ts segments are not”.

    Meaning of the hard-fail toggle:
        - `True`: if preflight probing fails, the task is marked failed directly.
        - `False`: if preflight probing fails, only a log is written and the download flow continues.

7.5 Candidate-Link Prioritization
    Corresponding feature toggle:
        features.download_candidate_ranking_enabled

    Function:
        Scores the `url` / `media_url` / `master_url` of an m3u8 task and selects a better address as the primary download URL.

    Scoring tendencies:
        - `https` gets bonus points.
        - Links carrying `referer` / `origin` / `cookie` / `authorization` get bonus points.
        - URLs that look like advertising or tracker endpoints get penalized.

7.6 Retry and Fallback
    Corresponding feature toggles:
        features.download_retry_enabled
        features.download_engine_fallback
        features.download_auth_retry_first
        features.download_auth_retry_per_engine

    Actual flow:
        1. First try the current candidate engine.
        2. If it fails, the program roughly classifies the error text into `auth` / `parse` / `timeout` / `unknown`.
        3. If it is an authentication-related failure, it first tries to supplement headers through `site_rules`, then retries several times within the same engine.
        4. If fallback is allowed, it continues trying other available engines.
        5. If task-level retry is allowed, another full round can still run after a full-round failure.
        6. Timeout-type failures use incremental waiting according to `backoff_seconds`.

7.7 Pause, Resume, Cancel, and Delete Tasks
    Stop response time:
        After you click "Pause" or "Cancel", every engine (N_m3u8DL-RE / yt-dlp / Streamlink / Aria2) breaks out of its
        read loop and calls `terminate()` within 500 ms; if the process has not exited after 1.5 s, it is killed recursively.
        The typical end-to-end response time is at most 2 seconds.
        Cancelling during an FFmpeg merge of a large file cleans up intermediate `.part` / `.tmp` files while keeping
        the artifacts from the previous completed step (for example, if the segments have finished downloading but
        the merge has not completed, the segments are kept so you can resume later).

    Pause:
        - Mark `stop_requested=True`
        - `stop_reason=paused`
        - Terminate the external process if one exists
        - Status enters `paused`

    Resume:
        - Remove from the `paused` list
        - Re-enqueue via `add_task()`

    Cancel:
        - `stop_reason=cancelled`
        - Terminate the process
        - Final status is handled as `failed`

    Delete:
        - `stop_reason=removed`
        - Remove from the manager state and queue
        - The UI side will clean temporary files later

7.8 Download Metrics and Automatic Learning of Site Rules
    Download metrics:
        Inside `DownloadManager`, counters are accumulated for `success_total` / `failed_total` / `by_engine` / `by_stage`.

    Automatic learning of site rules:
        Corresponding configuration items:
            site_rules_auto.enabled
            site_rules_auto.max_rules
            site_rules_auto.allow_cookie

        When this feature is enabled, `referer` / `user-agent` / `origin` / `cookie` from successful tasks can be extracted as automatic rules,
        so headers can be auto-filled in future visits to the same site.


================================================================================
8. Description and Usage Recommendations for Each Download Engine
================================================================================
8.1 N_m3u8DL-RE
    Suitable for:
        - m3u8
        - mpd
        - HLS / DASH
        - master-playlist + variant-resolution scenarios

    Characteristics of the current implementation:
        - Before startup, it reads `--help` once to detect which parameters the current binary supports.
        - It constructs and tries multiple candidate addresses in order: `primary` / `master` / `media`.
        - Supports safe-mode fallback to handle incompatible parameters.
        - Supports passing `--select-video` according to `selected_variant`.
        - Supports speed limit, thread count, retry count, output format, etc.

    Recommended usage:
        - Prioritize it for m3u8 sites.
        - Prioritize it when you want precise quality selection.

8.2 yt-dlp
    Suitable for:
        - Page-based sites such as YouTube / Bilibili / TikTok / Instagram / Twitter / Vimeo
        - Sites that require page parsing, format enumeration, and audio/video merging

    Characteristics of the current implementation:
        - Can obtain the format list first, then let the user select `format_id`.
        - Supports reading manually exported Cookies files.
        - If format retrieval or download fails, it will try to fall back to Firefox Cookies.
        - If a certificate problem is encountered, it automatically retries once with `--no-check-certificates`.
        - The speed limit can inherit the global `speed_limit`.

    How Cookies are actually used:
        - The program infers the Cookies filename based on the URL; for example, YouTube maps to `cookies/www.youtube.com_cookies.txt`.
        - If that file exists, it is used preferentially.
        - When the exact match misses, the program falls back along `.` boundaries in the hostname:
          `music.youtube.com` first tries `music.youtube.com_cookies.txt`, then `youtube.com_cookies.txt`,
          then `com_cookies.txt`, and so on until a file is found or the suffix list is exhausted.
        - If not, Firefox Cookies fallback is attempted.

    Console encoding:
        On Windows, yt-dlp stdout is decoded through a three-tier fallback: utf-8 (`errors='replace'`) → mbcs
        (the system ANSI code page, typically CP936) → latin-1.
        When the decoder actually falls back to mbcs / latin-1, the log line is tagged with `decode=mbcs` / `decode=lossy`
        so characters are never dropped silently.

    format_id character set (non-numeric format IDs supported):
        - Before the user- / dialog-selected `format_id` is handed to yt-dlp, it is validated against a strict allowlist: only characters matching `[A-Za-z0-9_.+:\-]+` are accepted.
        - The following real-world format IDs from YouTube / Bilibili and similar sites are all accepted and do not need to be downgraded to "best quality":
            · Pure numeric: `137`, `140`, `399`.
            · Combined audio+video with `+`: `137+140`, `bestvideo+bestaudio`, `bestvideo[height<=1080]+bestaudio/best`.
            · HLS / DASH prefixes: `hls-720`, `dash-480`, `http-720p`, `avc1_4d401f`.
            · Colons, underscores, and dots: `ec-3_audio`, `video:1080p`, `audio.original`.
        - Any string containing spaces, newlines, semicolons, pipes, backticks, `$()`, `&`, or `<>` is rejected with `invalid_format_id` and is never spliced into the command line. Even if the dialog is tampered with by a third party, it cannot become a command-injection entry point.

8.3 Streamlink
    Suitable for:
        - Livestream platforms
        - Livestream URLs such as Twitch / Douyu / Huya / Bilibili Live

    Characteristics of the current implementation:
        - Output is usually saved as `.ts`.
        - When exact total progress is unavailable, it displays written size and speed.
        - On failure, it performs simple cause diagnosis such as 401 / 403 / timeout / geo-restriction.
        - Cookies are split automatically on `;`: `a=1; b=2` becomes two separate `--http-cookie "a=1"` and `--http-cookie "b=2"` arguments,
          values are URL-escaped, and entries with an empty name or without `=` are dropped.

8.4 Aria2
    Suitable for:
        - Direct-link files such as mp4 / flv / webm / ts
        - `magnet` links

    Characteristics of the current implementation:
        - Supports multi-connection parallel downloading.
        - Inherits the global speed limit.
        - Can attach request headers such as `referer` / `user-agent` / `cookie`.

8.5 FFmpeg
    Its actual role in the current code:
        `FFmpegProcessor` is already loaded, but the main UI currently does not expose standalone buttons for “transcode / merge / extract subtitles / compress”.

    Implemented methods include:
        - Remux to MP4
        - Merge audio and video
        - Extract subtitles
        - Compress video

    Description:
        It is currently more like an integrated post-processing capability than a high-frequency entry point in the main UI.

    Cancel during merge:
        Large-file merges run through a cancellable read loop. When the operation is cancelled, intermediate `.part` / `.tmp`
        files are cleaned up while artifacts from the previous completed step are kept (for example, if the segments
        have finished downloading but the merge has not completed, the segments are preserved so you can resume later).


================================================================================
9. Logs, History, and Notifications
================================================================================
9.1 Runtime Logs
    The Runtime Logs panel only shows key logs or `WARNING` / `ERROR` / `CRITICAL`.

    Displayed content includes:
        - Task added to queue
        - Download started
        - Download completed
        - Download failed
        - Engine fallback
        - CatCatch request received
        - Configuration changes
        - Application startup / shutdown

    Usage:
        - If you feel that “nothing happened after clicking”, check here first.
        - If a download fails, read the key error here first, then go to the log folder for the full log.

    Default level and debug switches:
        File-level logging defaults to INFO. To capture DEBUG output, set the environment variable `M3U8D_LOG_DEBUG=1`
        and restart the program.
        Sensitive headers in command lines and URLs (Cookie / Set-Cookie / Authorization / Proxy-Authorization /
        X-Session-Token / User-Agent / Referer / Origin) as well as URL query parameters named `token` / `sign` /
        `signature` / `auth` are replaced with `<redacted>` before log lines are written to disk, so they never land
        on disk in plaintext.
        When you need plaintext for troubleshooting, set `SECURITY_DEBUG=1` to enable a separate `debug.sensitive.log`
        (disabled by default; be sure to disable it again once troubleshooting is done).
        Logs rotate automatically by date and a new file is created when the day changes at midnight; when the total size
        exceeds the limit, a throttled rotation kicks in (checked every 1000 entries or every 5 seconds), so enabling
        high-frequency DEBUG will not slow down downloads.

9.1.1 How to Understand Common Log Levels
    INFO: Indicates that the flow is progressing normally, or that one step has completed successfully.
        Common messages:
            - “Task added to queue”
            - “Download started”
            - “Download completed”
            - “Download task added”
        You can understand this as: the program is working, or this small step has completed successfully.

    WARNING: Indicates a condition that needs attention, but the program is usually still continuing, or has already automatically performed fallback / retry.
        Common messages:
            - Performance reminders such as “Download speed is too slow”
            - “The user-specified engine failed; fell back to a subsequent engine”
            - “Cookies may have expired; please export them again”
            - “Port is occupied; trying a fallback port”
        You can understand this as: this is not the final failure, but success rate, speed, or compatibility may be affected; if failure occurs later, follow the hint to supplement Cookies, switch engine, or retry.

    ERROR: Indicates that the current step has failed and usually needs focused attention and handling.
        Common messages:
            - “Download failed”
            - “Task failed”
            - “Failed to obtain formats”
            - “Failed to start script”
        You can understand this as: the current operation did not succeed. First check whether the link is valid, whether login is required, whether Referer / Cookie is complete, or try switching download engines.

    DEBUG: Mainly used for development and detailed troubleshooting. It is more common in full logs, and ordinary use may not display it frequently in the panel.
        Common messages:
            - “Command: ...”
            - “Tail output (20 lines)”
            - “Resource details”
        You can understand this as: this is auxiliary information for deep troubleshooting; ordinary users only need to look at INFO / WARNING / ERROR first.

9.2 Download History
    Storage path:
        Under the user directory: `.m3u8sniffer/history.json`

    Recorded content includes:
        - Filename
        - URL
        - Status
        - Size
        - headers
        - engine
        - save_dir
        - selected_variant
        - master_url
        - media_url
        - completed_at
        - cookie_file (if it existed at that time)

    Sensitive-header stripping before writing to history:
        Before `history.json` is written to disk, the `headers` field is filtered through a denylist so that short-lived tokens / credentials are not persisted to the user directory:
            - The following keys (case-insensitive) are dropped entirely and will NOT appear in `history.json`:
              `Cookie` / `Set-Cookie` / `Authorization` / `Proxy-Authorization` / `X-Session-Token` /
              `X-Auth-Token` / `Token` / `Api-Key` / `X-Api-Key`.
            - Fields that the downloader needs for reuse, such as `Referer` / `User-Agent` / `Origin` / `Accept-Language`, are kept verbatim so "right-click → Re-download" can still start with the correct context.
            - The stripping only happens at the point of writing to `history.json`; running tasks still hold the full headers. Because of that, "Re-download" from history gets the already-stripped headers back. If that causes auth failures, add the missing `Cookie` / `Authorization` value back through site rules or external headers passed at launch.

    Supported actions in the context menu:
        - Re-download
        - Open file location
        - View related logs
        - Delete from history
        - Copy filename / Copy URL / Copy whole row

9.3 System Notifications
    Corresponding configuration item:
        notification_enabled

    Actual behavior in the current code:
        At present, the notification function mainly writes logs and does not actually display system notification toasts.
        The `plyer`-based approach remains in comments, but is not enabled by default.


================================================================================
10. External Integration: CatCatch HTTP and the `m3u8dl://` Protocol
================================================================================
10.1 CatCatch HTTP Service
    At startup, the main window creates `CatCatchServer` and automatically starts the local HTTP service.

    Bind address:
        The service binds strictly to `127.0.0.1` and never to `0.0.0.0` / `::`.

    Port strategy:
        - Prefer 9527
        - If occupied, try 9528 ~ 9539
        - When every candidate port fails, `bind_timeout` or `port_exhausted` is logged and the UI is never left hanging.

    Authentication and cross-site protection:
        - At startup a one-time session token is generated at random and written to `~/.m3u8d/session.token`
          (POSIX permission 0600 / owner-only DACL on Windows) for trusted clients to read.
        - Every request's `Origin` / `Referer` must be on the allowlist; the default allowlist contains the four
          loopback variants `http://127.0.0.1`, `http://localhost`, `https://127.0.0.1`, and `https://localhost`.
        - `POST /download` additionally requires the `X-Session-Token` header to match the current session token:
          when it is missing or wrong the service returns 401; when the `Origin` is not on the allowlist it returns 403
          and does not echo back any `Access-Control-Allow-*` header.
        - `Access-Control-Allow-Origin` only echoes back the specific allowlisted origin and is never `*`.

    URL protection (SSRF filtering):
        - The `url` received by `POST /download` first goes through a public-address check (`ensure_public()`); the following targets are rejected with **400 Bad Request** and are never added to the queue:
            · Loopback: `127.0.0.0/8`, `::1`.
            · Private ranges: `10/8`, `172.16/12`, `192.168/16`, `fc00::/7`.
            · Link-local: `169.254/16`, `fe80::/10`.
            · Cloud metadata: `169.254.169.254` (IMDSv1/v2 endpoint) and other common cloud-internal endpoints.
            · Non-`http` / `https` schemes, over-length URLs (>4096), or hostnames that cannot be resolved to an IP (resolution failure is treated as untrusted).
        - This filter shares its code path with `main.py --url` and the protocol handler, so URLs coming in through the browser extension, the `m3u8dl://` protocol, or the command line are all validated by the same rule set.

    Internal marker protection (external `_`-prefixed headers are rejected):
        - Internally the program may attach one-shot hints (such as "which cookies file should this task use") to the headers dict as underscore-prefixed keys like `_cookie_file`. These are strictly for engine-side consumption.
        - Before `POST /download`'s `headers` field is forwarded, every key starting with `_` is dropped. This prevents external callers from disguising a key as "it looks internal" to bypass the allowlist or to inject an arbitrary local path into an engine command line. The `m3u8dl://` protocol handler applies the same stripping to its JSON payload.
        - As a result, neither the browser extension, an external script, nor the protocol handler can make the program read an arbitrary file by writing `_cookie_file: C:\...` into the JSON body. The real cookie-file path is only ever derived internally from the URL.

    Headers forwarded to engines are sanitized:
        - Header names may only contain `[A-Za-z0-9-]` and must be at most 64 characters; values must not contain
          `\r \n \0` and must be at most 4096 characters.
        - Only the allowlisted fields Referer / User-Agent / Origin / Cookie / Accept-Language are forwarded;
          other fields are silently dropped.
        - When forwarded to an engine command line, headers are always passed through a parameterized argument array;
          string concatenation is never used.

    Request size limit:
        The `POST /download` request body is capped at **64 KiB**. When that limit is exceeded the service returns
        **413 Request Entity Too Large** and writes a `catcatch_body_too_large` entry in the log. Normal browser-extension
        payloads (URL + headers + filename, typically a few KiB) sit comfortably below this ceiling and are never
        affected by it.

    Endpoints:
        GET /
            View service information and endpoints.

        GET /status
            Returns runtime status.

        GET /download?url=...&name=...
            This GET endpoint now returns **405 Method Not Allowed** and no longer triggers any download action. The reason is that a read-only GET cannot be protected by a session token, so all download requests are funneled through the authenticated `POST /download` + `X-Session-Token` path. External callers should switch to POST; the read-only `GET /` and `GET /status` endpoints remain available and still do not require authentication.

        POST /download
            Accepts JSON or form.
            Supported fields: `url` / `headers` / `name` / `filename`

    Actual behavior after receiving a request:
        1. Construct a resource object first.
        2. Add it to the Resource List.
        3. Switch to the Resource List tab.
        4. Show a status-bar prompt that a download request has been received.

10.2 `m3u8dl://` Protocol Handler
    The protocol handling script is `protocol_handler.pyw`.

    It supports parsing three kinds of input:
        1. `m3u8dl:"URL" --save-dir ... --save-name ... -H "Header: Value"`
        2. `m3u8dl://http://example.com/xxx.m3u8`
        3. `m3u8dl://{"url":"...","headers":{},"name":"..."}`

    Actual execution flow:
        1. Parse the incoming protocol content.
        2. Read `~/.m3u8d/session.token` for the `X-Session-Token` header, attach `Origin: http://127.0.0.1`,
           and POST `/download` to each candidate port from 9527 to 9539 in turn. The first port that returns
           a 2xx response counts as a successful handshake and no new main instance is launched.
        3. Only when every candidate port fails to hand off (the main program really is not running,
           the token file is missing, or the token is no longer valid) does the handler start `main.py`
           with `--url` / `--headers` / `--filename`.
        4. After the new process comes up, `send_to_app` is polled for up to 12 seconds to deliver the link
           into the new instance's resource list.

    Log redaction:
        `logs/protocol_handler.log` never records the token in plaintext and only keeps redacted metadata such as
        `token_loaded=<bool>` / `token_len=<int>` / `status_code=<int>` / `auth_ok=<bool>`.

    Emergency rollback:
        If some environment cannot use the handshake for the moment (for example, the token file is quarantined
        by security software while the main program is still running), you can set the environment variable
        `M3U8D_HANDOFF_LEGACY=1` to temporarily revert to the legacy behavior (no token read, no Origin header,
        only a 2xx check). Every such call writes an extra legacy marker line so you can spot it in the log.
        It is disabled by default; please unset the variable once diagnosis is done.

    Usage scenarios:
        - A browser extension sends resources back to the desktop program with one click.
        - An external script or tool calls the system protocol.

    Recommended CatCatch URL Protocol settings:
        - Enable m3u8dl:// Download m3u8 or mpd: N_m3u8DL-RE
        - Confirm Parameters: Enabled
        - Parameter Setting:
          "${url}" --save-dir "%USERPROFILE%\Downloads\m3u8dl" --save-name "${title}_${now}" ${referer|exists:'-H "Referer:*"'} ${cookie|exists:'-H "Cookie:*"'} --no-log

    Setting notes:
        - `${url}`: the real resource URL captured by CatCatch.
        - `--save-dir`: passes the target output directory through the protocol payload.
        - `--save-name`: combines the CatCatch title and timestamp into the default file name.
        - `${referer|exists:...}`: if CatCatch has a Referer, it appends `-H "Referer:*"` automatically.
        - `${cookie|exists:...}`: if CatCatch has a Cookie, it appends `-H "Cookie:*"` automatically.
        - `--no-log`: kept as a compatibility passthrough flag; the current protocol parser ignores it safely.

    Prerequisites:
        1. Run scripts\register_protocol.bat first to register the `m3u8dl://` protocol.
        2. Then paste the settings above into CatCatch's URL Protocol m3u8dl configuration.
        3. After that, sending a resource from CatCatch will automatically hand it to the app and insert it into the resource list.
 
 
================================================================================
11. All Major `config.json` Settings and How to Use Them
================================================================================
11.1 Base Directories and Global Task Settings
    download_dir
        Meaning: default download directory.
        Usage:
            - Can be changed directly in the UI under “Save Location”.
            - Can also be edited manually in `config.json`.

    temp_dir
        Meaning: temporary directory. Intermediate files such as those used by N_m3u8DL-RE will use it.
        Usage:
            - Recommended to place it on a local SSD path.
            - If disk space is insufficient, it can be moved to another drive.

    max_concurrent_downloads
        Meaning: number of simultaneously running tasks.
        Usage:
            - “Concurrent Tasks” in the UI directly changes this value.

    speed_limit
        Meaning: global speed limit in MB/s; 0 means unlimited.
        Usage:
            - “Speed Limit” in the UI directly changes this value.

    max_retry_attempts
        Meaning: maximum number of task-level retry rounds.
        Recommendation:
            - For stable sites, 1~3 is usually enough.
            - For sites prone to disconnects, it can be increased appropriately.

    retry_backoff_seconds
        Meaning: number of seconds to wait between task-level retries.
        Description:
            - Timeout-type failures use incremental backoff based on this value.

11.2 `site_rules` and Automatically Learned Rules
    site_rules
        Meaning: automatically supplements headers such as Referer, UA, Cookie, Authorization, etc. based on domain / keyword.

        Common fields in a single rule:
            name
            domains
            url_keywords
            referer
            user_agent
            headers

        Typical uses:
            - Some sites require `referer` for m3u8.
            - Some sites require a fixed UA.
            - Some sites require `authorization` or `cookie`.

    site_rules_auto.enabled
        Meaning: whether the program is allowed to automatically learn rules from successful tasks.

    site_rules_auto.max_rules
        Meaning: the maximum number of automatically learned rules to retain.

    site_rules_auto.allow_cookie
        Meaning: whether Cookies may also be written into rules during automatic learning.
        Reminder:
            - Enabling this is more convenient, but also makes it easier to solidify short-lived Cookies into the configuration.

11.3 `features`: Per-Item Explanation of Feature Switches
    sniffer_rules_enabled
        Meaning: whether `site_rules` should be applied at the sniffing stage to supplement headers.
        Recommendation: generally keep it `true`.

    sniffer_dedup_enabled
        Meaning: whether resource deduplication is enabled.
        Recommendation: generally keep it `true`; otherwise the list may explode in volume.

    sniffer_filter_noise
        Meaning: a reserved noise-filter switch for the network interceptor.
        Description: the current main stream-capture pipeline is Playwright-based, and the QWebEngine interceptor is not the primary path.

    download_retry_enabled
        Meaning: whether task-level retries are allowed.

    download_engine_fallback
        Meaning: whether to automatically switch to candidate engines when the primary engine fails.

    download_auth_retry_first
        Meaning: when authentication fails, whether to first retry in the same engine after supplementing headers.

    download_auth_retry_per_engine
        Meaning: when each engine encounters an auth-type failure, how many same-engine authentication retries are allowed at most.

    download_candidate_ranking_enabled
        Meaning: whether to score and rank m3u8 candidate links.

    hls_probe_enabled
        Meaning: whether to perform HLS preflight probing before formal downloading.

    hls_probe_hard_fail
        Meaning: whether an HLS preflight-probe failure directly marks the task as failed.

    browser_capture_window_enabled
        Meaning: whether to enable the continued probing window after playback begins.

    browser_capture_window_seconds
        Meaning: the base duration in seconds of one capture window.

    browser_capture_extend_on_hit_seconds
        Meaning: extra seconds to extend after a media hit occurs.

    browser_capture_probe_interval_ms
        Meaning: the interval in milliseconds for active page scanning during the capture window.

    ui_batch_actions
        Meaning: whether to display “Download Selected / Remove Selected” in the Resource List.

    ui_filter_search
        Meaning: whether to display the search and filtering bar in the Resource List.

    m3u8_nested_depth
        Meaning: the maximum depth for parsing nested master playlists.
        Description: although it may not be explicitly written in the default configuration file, the code supports this feature.

11.4 `engines.n_m3u8dl_re`
    path
        Meaning: executable-file path of N_m3u8DL-RE.

    thread_count
        Meaning: default thread count.

    thread_min / thread_max
        Meaning: adaptive thread-count range.

    retry_count
        Meaning: engine-internal retry count.

    max_retry
        Meaning: compatibility parameter written into N_m3u8DL-RE as `--max-retry`.

    adaptive
        Meaning: whether to append `--adaptive` (takes effect only if the current binary supports it).

    output_format
        Meaning: output format, for example `mp4`.

    force_http1
        Meaning: if enabled, try passing `--force-http1` to N_m3u8DL-RE.

    no_date_info
        Meaning: if enabled, try passing `--no-date-info`.

11.5 `engines.ytdlp` / `streamlink` / `aria2` / `ffmpeg`
    engines.ytdlp.path
        yt-dlp path.

    engines.streamlink.path
        Streamlink path.

    engines.aria2.path
        Aria2 path.

    engines.aria2.max_connection_per_server
        Maximum connections per server.

    engines.aria2.split
        Number of segments.

    engines.ffmpeg.path
        FFmpeg path.

11.6 Other Configuration Items
    notification_enabled
        Meaning: whether notification events are logged.
        Description: currently mainly affects whether the notification function executes its logging-notification logic.

    auto_delete_temp
        Meaning: this item exists in the configuration.
        Description: the current main download flow does not directly use it as a unified switch; it is more of a reserved / extensibility item.

    proxy.enabled / proxy.http / proxy.https
        Meaning: proxy configuration items already exist in the configuration structure.
        Description: the current main download and browser pipelines do not yet uniformly propagate proxy parameters directly from these values, so these are more reserved configuration items.

    catcatch.port
        Meaning: if provided in the configuration, it can specify the preferred port of `CatCatchServer`; the default is 9527.


11.7 Environment Variables
    The following environment variables affect runtime behavior. They are all disabled by default, and changes take effect after restarting the program.

    M3U8D_LOG_DEBUG
        Meaning: when set to `1`, raises the file-level log level from the default INFO to DEBUG.
        Note: keep it disabled during normal use, and only enable it when you need to capture detailed runtime traces.

    SECURITY_DEBUG
        Meaning: when set to `1`, enables a separate `debug.sensitive.log` that records the raw content of command lines
        and sensitive headers for troubleshooting.
        Note: this file lives alongside the main logs but in a dedicated file. Once troubleshooting is done, disable the
        variable or remove the file so sensitive content does not linger on disk.

    M3U8D_HANDOFF_LEGACY
        Meaning: when set to `1`, the protocol handler falls back to the legacy behavior (no token read, no Origin header,
        only a 2xx check), acting as an emergency rollback channel when `m3u8dl://` delivery misbehaves.
        Keep it disabled during day-to-day use.

    M3U8D_SECURITY_DIAGNOSTIC
        Meaning: when set to `1` together with `security.allow_weak_manifest_verification=true` in the configuration,
        component updates are allowed to relax manifest verification in diagnostic mode.
        Note: only use this for offline diagnosis; in production both switches must remain disabled.


================================================================================
12. Common Operation Examples
================================================================================
12.1 Capture and Download a Web Video
    1. Open Browser Workbench.
    2. Click Start Browser.
    3. Enter the video-site page URL in the address bar.
    4. Play the video in the external Chrome.
    5. Switch to the Resource List.
    6. Select a resource and click Download.
    7. Check progress and logs in Download Center.

12.2 Download a Page-Based Video from YouTube / Bilibili, etc.
    1. Open the corresponding video page.
    2. Wait for the page-based resource to enter the Resource List.
    3. Click Download.
    4. Select a resolution in the format-selection dialog or click “Best Quality”.
    5. If the site requires login, preparing the corresponding Cookies file is more reliable.

12.3 Download an m3u8 Video with Multiple Resolutions
    1. Let the program capture the m3u8 resource.
    2. Wait for the Resolution column in the Resource List to update.
    3. Select the original master entry or a specific variant.
    4. Click Download.
    5. Confirm the target resolution in the popup dialog.

12.4 Download with a Magnet Link
    1. Paste the `magnet` link into the address bar.
    2. Press Enter.
    3. The resource enters the Resource List.
    4. Click Download; the program will usually use Aria2.

12.5 Re-download from History
    1. Open Download Center.
    2. Switch to “Download History”.
    3. Right-click the target record.
    4. Select “Re-download”.

12.6 Return via CatCatch or Protocol
    1. Keep the program running.
    2. The browser extension sends the URL to the local HTTP API, or starts the program through `m3u8dl://`.
    3. The resource enters the Resource List.
    4. You then confirm the download.


================================================================================
13. Common Issues and Troubleshooting Suggestions (Combined with the Current Code Behavior)
================================================================================
13.1 Nothing Happens After Clicking Download
    Check first:
        - Whether the task actually entered the download queue.
        - Whether a format-selection dialog popped up and you did not confirm it.
        - Whether a file with the same name already exists locally and the program is waiting for an overwrite decision.
        - Whether HLS preflight probing failed and was directly blocked by `hls_probe_hard_fail`.

13.2 The Page Is Captured but No Resource Is Detected
    Suggestions:
        - Confirm that you have clicked “Start Browser”.
        - Actually play the video in the external browser, rather than merely staying on the details page.
        - Wait a few seconds and let the capture window continue scanning.
        - If necessary, switch resolution or refresh and try again.

13.3 YouTube / Bilibili Resources Are Incomplete or Cannot Be Downloaded
    Suggestions:
        - Try to make a page-based resource appear in the list and then download it with yt-dlp.
        - Prepare the corresponding site Cookies file.
        - If format retrieval fails, the program will try Firefox Cookies fallback.

13.4 m3u8 Download Fails
    Common causes:
        - `referer` / `cookie` is incomplete
        - The key or ts segments are not authorized
        - The site is sensitive to overly high thread counts

    Suggestions:
        - Configure `site_rules`.
        - Reduce thread count, concurrency, and speed limit.
        - Prioritize N_m3u8DL-RE.
        - Check the Runtime Logs and the full logs under the `logs` directory.

13.5 Why Does the Program Switch Engines After a Download Failure?
    Because the current default enables:
        features.download_engine_fallback = true

    Function:
        After one engine fails, the program tries other candidate engines to improve the success rate.

13.6 Why Can Old Tasks Still Be Re-downloaded from History?
    Because the history stores not only the URL, but also tries to preserve context such as `headers`, `engine`, `save_dir`, `selected_variant`, `master_url`, `media_url`, etc.

13.7 Private or LAN Addresses Cannot Be Downloaded
    To reduce the risk of using this program to reach internal networks or cloud-metadata endpoints
    (`127.0.0.1`, `10.x`, `172.16/12`, `192.168.x`, `169.254.x`, `fc00::/7`, and similar ranges),
    m3u8 parsing and fetching reject non-public addresses by default. The log will show markers such as
    `SSRFBlocked` or `ssrf_blocked`.

    If you genuinely need to download through a public-facing domain that a corporate reverse proxy
    forwards into an internal network, set `allow_private_networks` to `true` inside `features` in
    `config.json` and restart the program. The log will print a prominent warning to indicate that this
    is a reduced-privilege path. Regular users should keep the default of `false`.


================================================================================
14. Notes on the Current Code State (To Avoid Misunderstandings)
================================================================================
The following items are capabilities that “exist in code but are not necessarily used frequently as complete outward-facing features in the main UI”:
    - `FFmpegProcessor` is loaded, but the main UI has no standalone post-processing buttons.
    - The QWebEngine `NetworkInterceptor` class exists, but the main stream-capture path is currently `PlaywrightDriver`.
    - Configuration items such as `auto_delete_temp` / `proxy` already exist, but the current main flow does not yet uniformly and fully take over them.
    - The browser page’s Back / Forward / Refresh compatibility interfaces are still placeholder implementations and do not constitute complete navigation control.

Purpose of the above explanation:
    To help you read this manual based on the current actual visible behavior of the program and the code already integrated, rather than treating all reserved classes as fully completed outward-facing features.


================================================================================
15. Recommended Daily Usage Sequence
================================================================================
    1. First confirm that each downloader exists in the `bin` directory.
    2. Start the program.
    3. If browser-based stream capture is needed, click “Start Browser” first.
    4. Open the video page and start playback.
    5. Filter out the target resource in the Resource List.
    6. Choose the proper engine according to the site type:
        - `m3u8` / `mpd`: prioritize N_m3u8DL-RE
        - Page-based platforms: prioritize yt-dlp
        - Livestreams: prioritize Streamlink
        - Direct links / magnet links: prioritize Aria2
    7. Observe task status, logs, and history in Download Center.
    8. If it fails, then use the logs to decide whether to supplement Cookies, supplement Referer, adjust thread count, disable rate limiting, or switch engines.
