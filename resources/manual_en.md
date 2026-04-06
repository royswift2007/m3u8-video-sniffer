M3U8 Video Sniffer Detailed User Manual (Compiled According to the Current Actual Program Code)

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

        --headers
            A JSON string, typically in the following format:
            {"referer":"...","user-agent":"...","cookie":"..."}

        --filename
            The default filename or title for the incoming resource.

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

There is also a “Quick Manual” entry in the upper-right corner used to open this manual.

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
        2. Narrow the range via “All Types / M3U8 / MPD / MP4 ...”.
        3. Use the source filter to locate resources from a specific page source.
        4. Use options such as 2160 / 1080 / 720 / Audio to filter resolution.

5.4 Download Selected / Remove Selected / Clear List
    Download Selected:
        Executes the download logic one by one for the currently selected multiple rows.

    Remove Selected:
        Removes the selected resources from the UI list and the internal resource array, then rebuilds the deduplication cache.

    Clear List:
        Clears the resource table, deduplication cache, `page_url` mappings, and filter conditions.

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

6.5 Speed Limit
    Corresponding configuration item:
        speed_limit

    Meaning in the UI:
        The unit is MB/s; 0 means unlimited.

    Actual effect:
        - N_m3u8DL-RE: converted into the `--max-speed` parameter.
        - yt-dlp: uses `--limit-rate`.
        - Aria2: uses `--max-download-limit`.
        - Streamlink currently does not have dedicated rate limiting tied to this parameter.

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
        1. `add_task()` sets the task status to `waiting`.
        2. It selects the preferred engine according to the current settings and user preference.
        3. Worker threads take tasks for execution according to the concurrency limit.

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
        - If not, Firefox Cookies fallback is attempted.

8.3 Streamlink
    Suitable for:
        - Livestream platforms
        - Livestream URLs such as Twitch / Douyu / Huya / Bilibili Live

    Characteristics of the current implementation:
        - Output is usually saved as `.ts`.
        - When exact total progress is unavailable, it displays written size and speed.
        - On failure, it performs simple cause diagnosis such as 401 / 403 / timeout / geo-restriction.

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

    Port strategy:
        - Prefer 9527
        - If occupied, try 9528 ~ 9539

    Endpoints:
        GET /
            View service information and endpoints.

        GET /status
            Returns runtime status.

        GET /download?url=...&name=...
            Adds a task through a simple GET method.

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
        2. First try to POST the task to the running main program.
        3. If the local program is not running, start `main.py` and pass `--url` / `--headers` / `--filename`.
        4. Try delivering to the GUI program again.

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
