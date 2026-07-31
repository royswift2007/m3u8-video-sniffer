# M3U8D Aria2 无扩展名与无法合并问题函数级修复、验证方案

## 0. 背景结论

本方案针对 Aria2 下载完成后可能出现“下载文件无扩展名”或“只有单片段、无法合并”的问题。

已确认的根因如下：

1. `DownloadTask.filename` 当前语义是标题 stem，不保证带媒体扩展名。默认标题提取逻辑会从 URL 文件名中去掉最后一段扩展名。
2. yt-dlp 与 N_m3u8DL-RE 自身具备输出扩展名、合并、remux 或 mux 能力，因此 stem-only 文件名不会直接暴露为问题。
3. Aria2 路径直接把 `DownloadTask.filename` 传给 aria2 的 `-o` 参数，不补扩展名。
4. Aria2 是直链下载器，不解析 m3u8，不下载完整分片列表，不解密 HLS，不合并分片，不 remux。如果把 m3u8、ts/m4s 分片或带嗅探上下文的 segment 交给 Aria2，它可能只下载单个资源并被下载管理器误判为完成。

相关入口：

- [`M3U8Resource._extract_url_title()`](core/task_model.py:111)：从 URL 文件名提取标题，并去掉后缀。
- [`DownloadTask`](core/task_model.py:228)：`filename` 为下载任务文件名/标题。
- [`Aria2Engine._build_command()`](engines/aria2_engine.py:173)：当前使用 `-o task.filename`。
- [`YtdlpEngine._build_command()`](engines/ytdlp_engine.py:702)：使用 `task.filename.%(ext)s`。
- [`N_m3u8DL_RE_Engine._build_command()`](engines/n_m3u8dl_re.py:760)：负责 HLS/MPD 分片下载、合并、mux。
- [`EngineSelector.select()`](core/engine_selector.py:595)：手动选择引擎时当前会直接尊重用户选择。
- [`DownloadManager._execute_download()`](core/download/manager.py:1588)：引擎返回成功后进入完成态。
- [`MainWindowPostprocessMixin._find_task_output_file()`](ui/main_window_postprocess.py:27)：后处理查找下载产物。

## 1. 修复目标

### 1.1 必须达成

1. Aria2 下载真正直链媒体时，最终产物应带正确扩展名，例如 `movie.mp4`、`movie.webm`、`movie.ts`。
2. HLS、MPD、带上下文的 segment 资源不应被手动 Aria2 路径直接下载为最终产物。
3. Aria2 进程退出成功但产物明显不是最终媒体时，不应直接标记为成功完成，应进入回退或失败提示。
4. 后处理不应优先拿到旧的无扩展名残留文件。
5. 不破坏 yt-dlp、N_m3u8DL-RE、Streamlink 的现有行为。

### 1.2 非目标

1. 不把 Aria2 改造成 HLS 下载/合并引擎。
2. 不全局改变 `DownloadTask.filename` 的 stem 语义。
3. 不在标题生成层强行追加扩展名，避免影响 UI、历史、去重、yt-dlp 输出模板。
4. 不自动删除用户已有的无扩展名历史产物。

## 2. 总体修复策略

采用三层修复：

1. 引擎选择层：拦截明显不适合 Aria2 的 HLS/MPD/segment 任务。
2. Aria2 命令层：仅对确认是直链媒体的 URL，为 Aria2 输出名补扩展名。
3. 下载完成层与后处理层：校验 Aria2 产物，避免把 manifest、单分片、HTML 错误页或无效文件当成完整视频。

推荐分两次提交：

- 第一提交：引擎选择防误用 + Aria2 输出名补扩展名 + 单元测试。
- 第二提交：Aria2 产物校验 + 后处理产物选择优化 + 回归测试。

## 3. 分步骤函数级修复方案

## Step 1：补充 Aria2 输出文件名解析能力

### 3.1 修改文件

- [`engines/aria2_engine.py`](engines/aria2_engine.py:1)

### 3.2 新增/调整常量

在 [`Aria2Engine`](engines/aria2_engine.py:14) 类内新增只用于“输出扩展名推断”的集合，建议不要直接复用所有 can_handle 扩展语义。

建议：

```python
_DIRECT_OUTPUT_EXTENSIONS = {
    ".mp4", ".flv", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".m4v", ".3gp", ".mpg", ".mpeg", ".f4v",
}

_OPTIONAL_TS_OUTPUT_EXTENSIONS = {".ts"}

_HLS_OR_DASH_EXTENSIONS = {".m3u8", ".mpd"}

_MIME_EXTENSION_MAP = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/mp2t": ".ts",
    "video/vnd.dlna.mpeg-tts": ".ts",
    "video/x-flv": ".flv",
    "video/quicktime": ".mov",
}
```

说明：

- `.ts` 不应无条件进入普通直链集合，因为它既可能是完整 MPEG-TS，也可能是 HLS 单分片。
- `.m3u8`、`.mpd` 绝不能作为 Aria2 最终媒体输出扩展。

### 3.3 新增函数：`_path_suffix_from_url(url: str) -> str`

位置建议：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 之前。

职责：

1. 使用 `urllib.parse.urlparse` 获取 path。
2. URL decode path 最后一段。
3. 使用 `pathlib.PurePosixPath` 或字符串方式获取 suffix。
4. 返回小写扩展名；解析失败返回空字符串。
5. 必须忽略 query 和 fragment。

伪代码：

```python
@staticmethod
def _path_suffix_from_url(url: str) -> str:
    try:
        path = urlparse(url or "").path
        suffix = PurePosixPath(unquote(path)).suffix.lower()
        return suffix
    except Exception:
        return ""
```

验证点：

- `https://a.com/video.mp4?token=1` 返回 `.mp4`。
- `https://a.com/video` 返回空字符串。
- `https://a.com/index.m3u8?x=1` 返回 `.m3u8`。

### 3.4 新增函数：`_filename_has_media_suffix(filename: str) -> bool`

位置建议：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 之前。

职责：

1. 判断任务文件名本身是否已经带媒体扩展名。
2. 如果已有后缀，不再追加，防止双扩展名。
3. 应使用 Windows 安全方式，只看 basename，不把目录分隔符纳入判断。

伪代码：

```python
@classmethod
def _filename_has_media_suffix(cls, filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix in cls._DIRECT_OUTPUT_EXTENSIONS or suffix in cls._OPTIONAL_TS_OUTPUT_EXTENSIONS
```

验证点：

- `movie.mp4` 返回 True。
- `movie` 返回 False。
- `movie.final` 返回 False，除非 `.final` 被显式定义为媒体扩展。

### 3.5 新增函数：`_task_looks_like_hls_or_segment(task: DownloadTask) -> bool`

位置建议：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 之前。

职责：判断当前任务是否不应该由 Aria2 作为最终媒体直链处理。

判断条件建议：

1. `task.resource_type` 是 `hls` 或 `dash`：返回 True。
2. `task.resource_type` 是 `segment`，并且存在以下任一上下文：
   - `task.master_url`
   - `task.media_url`
   - `task.page_url`
   - `task.source` 不是 `unknown`
3. `task.mime` 是 HLS/DASH MIME，例如：
   - `application/vnd.apple.mpegurl`
   - `application/x-mpegurl`
   - `audio/mpegurl`
   - `application/dash+xml`
4. URL path 后缀是 `.m3u8` 或 `.mpd`。

伪代码：

```python
@classmethod
def _task_looks_like_hls_or_segment(cls, task: DownloadTask) -> bool:
    resource_type = str(getattr(task, "resource_type", "") or "").lower()
    if resource_type in {"hls", "dash"}:
        return True

    if resource_type == "segment":
        has_context_hint = bool(
            getattr(task, "master_url", None)
            or getattr(task, "media_url", None)
            or getattr(task, "page_url", "")
            or str(getattr(task, "source", "") or "").lower() not in {"", "unknown"}
        )
        if has_context_hint:
            return True

    mime = str(getattr(task, "mime", "") or "").lower()
    if "mpegurl" in mime or "dash+xml" in mime:
        return True

    return cls._path_suffix_from_url(getattr(task, "url", "")) in cls._HLS_OR_DASH_EXTENSIONS
```

注意：裸 `.ts` 直链如果没有 page/master/media/source 上下文，不在这里直接判为 HLS 分片，以兼容完整 TS 文件下载。

### 3.6 新增函数：`_infer_direct_output_extension(task: DownloadTask) -> str`

位置建议：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 之前。

职责：只在确认是直链媒体时推断输出扩展名。

优先级建议：

1. 如果 [`_task_looks_like_hls_or_segment()`](engines/aria2_engine.py:173) 返回 True，直接返回空字符串。
2. 从 URL path suffix 推断：
   - suffix 在 `_DIRECT_OUTPUT_EXTENSIONS` 中，返回 suffix。
   - suffix 是 `.ts`，仅当不是 HLS/segment 上下文时返回 `.ts`。
3. 从 `task.mime` 推断：
   - 按 `_MIME_EXTENSION_MAP` 返回扩展。
   - MIME 含 mpegurl 或 dash+xml 时返回空字符串。
4. 推断不到则返回空字符串。

伪代码：

```python
@classmethod
def _infer_direct_output_extension(cls, task: DownloadTask) -> str:
    if cls._task_looks_like_hls_or_segment(task):
        return ""

    suffix = cls._path_suffix_from_url(getattr(task, "url", ""))
    if suffix in cls._DIRECT_OUTPUT_EXTENSIONS:
        return suffix
    if suffix in cls._OPTIONAL_TS_OUTPUT_EXTENSIONS:
        return suffix

    mime = str(getattr(task, "mime", "") or "").lower().split(";", 1)[0].strip()
    if "mpegurl" in mime or "dash+xml" in mime:
        return ""
    return cls._MIME_EXTENSION_MAP.get(mime, "")
```

### 3.7 新增函数：`_resolve_output_filename(task: DownloadTask) -> str`

位置建议：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 之前。

职责：生成 Aria2 专用落盘文件名。

规则：

1. 读取 `task.filename`，保持原 stem 语义。
2. 如果文件名已经有媒体扩展名，直接返回原名。
3. 调用 [`_infer_direct_output_extension()`](engines/aria2_engine.py:173) 获取扩展名。
4. 如果扩展名为空，返回原名。
5. 否则返回 `filename + ext`。

伪代码：

```python
@classmethod
def _resolve_output_filename(cls, task: DownloadTask) -> str:
    filename = str(getattr(task, "filename", "") or "").strip()
    if not filename:
        filename = "download"
    if cls._filename_has_media_suffix(filename):
        return filename
    ext = cls._infer_direct_output_extension(task)
    if not ext:
        return filename
    return f"{filename}{ext}"
```

### 3.8 修改函数：`Aria2Engine._build_command()`

当前关键位置：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173)。

修改点：

1. 在构造命令前计算 `output_filename = self._resolve_output_filename(task)`。
2. 将 `-o task.filename` 改为 `-o output_filename`。
3. 建议为调试保留日志：原始任务名、实际输出名、推断扩展名来源。
4. 不要修改 `task.filename` 本身。

伪代码：

```python
output_filename = self._resolve_output_filename(task)
cmd = [
    self.binary_path,
    task.url,
    "-d", task.save_dir,
    "-o", output_filename,
    ...
]
```

### 3.9 修改函数：`Aria2Engine.download()`

当前位置：[`Aria2Engine.download()`](engines/aria2_engine.py:45)。

建议：

1. 下载成功日志中同时输出 `task.filename` 与实际 Aria2 输出名。
2. 如果不新增任务字段，可在 [`_build_command()`](engines/aria2_engine.py:173) 内局部日志即可。
3. 如需后续下载管理器读取实际输出名，可在 `task` 上写入非持久属性，例如 `_aria2_output_filename`，但这不是第一阶段必须项。

注意：如果写入动态属性，必须只作为内部运行态信息，不参与历史/序列化，避免扩大改动面。

## Step 2：修复手动选择 Aria2 绕过上下文保护的问题

### 4.1 修改文件

- [`core/engine_selector.py`](core/engine_selector.py:1)

### 4.2 新增函数：`_aria2_is_unsafe_for_context(url: str, context: EngineSelectContext | None) -> bool`

位置建议：[`_decide_from_context()`](core/engine_selector.py:324) 附近或 [`EngineSelector`](core/engine_selector.py:453) 类内私有方法。

职责：判断用户手动选择 Aria2 是否应被覆盖。

规则建议：

1. `context` 为空时，不覆盖手动 Aria2。
2. `context.resource_type` 为 HLS 或 DASH：返回 True。
3. `context.resource_type` 为 segment：
   - 如果 `context.master_url` 或 `context.media_url` 存在，返回 True。
   - 如果 `context.page_url` 存在，返回 True。
   - 如果 `context.source` 不是 `unknown`，返回 True。
4. URL path 后缀为 `.m3u8` 或 `.mpd`：返回 True。
5. MIME 是 mpegurl 或 dash+xml：返回 True。
6. 裸 `.ts` 且无上下文，返回 False，兼容完整 TS 文件。

伪代码：

```python
def _aria2_is_unsafe_for_context(url: str, context: EngineSelectContext | None) -> bool:
    if context is None:
        ext = _path_extension(url)
        return ext in {".m3u8", ".mpd"}

    if context.resource_type in {CTX_HLS, CTX_DASH}:
        return True

    if context.resource_type == CTX_SEGMENT:
        if context.master_url or context.media_url or context.page_url or context.source != CTX_UNKNOWN:
            return True

    mime = (context.mime or "").lower()
    if "mpegurl" in mime or "dash+xml" in mime:
        return True

    return _path_extension(url) in {".m3u8", ".mpd"}
```

### 4.3 新增函数/方法：`_select_safe_replacement_for_aria2(...)`

位置建议：[`EngineSelector`](core/engine_selector.py:453) 类内。

职责：当手动 Aria2 不安全时，选择安全替代引擎。

输入：

- `url`
- `context`

逻辑：

1. 调用 [`EngineSelector.get_candidates()`](core/engine_selector.py:479)，并传入 context。
2. 过滤掉 Aria2。
3. 返回第一个候选。
4. 如果无候选：
   - 优先返回 N_m3u8DL-RE。
   - 再返回 yt-dlp。
   - 最后抛出清晰错误。

伪代码：

```python
def _select_safe_replacement_for_aria2(self, url: str, context: EngineSelectContext | None):
    candidates = [
        (engine, name)
        for engine, name in self.get_candidates(url, context=context, include_generic_engines=False)
        if name != ENGINE_ARIA2
    ]
    if candidates:
        return candidates[0]

    for name in (ENGINE_N_M3U8DL_RE, ENGINE_YTDLP):
        engine = self._engine_map.get(name)
        if engine:
            return engine, name

    raise RuntimeError("当前资源不适合 Aria2，且没有可用的 HLS/通用下载引擎")
```

### 4.4 修改函数：`EngineSelector.select()`

当前位置：[`EngineSelector.select()`](core/engine_selector.py:595)。

修改点：

1. 保持普通手动选择行为不变。
2. 仅当 `user_preference == ENGINE_ARIA2` 且 [`_aria2_is_unsafe_for_context()`](core/engine_selector.py:324) 为 True 时，覆盖手动 Aria2。
3. 覆盖时记录日志，包含：preferred_engine、replacement_engine、resource_type、source、reason。
4. 返回安全替代引擎。

伪代码：

```python
if user_preference and user_preference in self._engine_map:
    if user_preference == ENGINE_ARIA2 and _aria2_is_unsafe_for_context(url, context):
        engine, engine_name = self._select_safe_replacement_for_aria2(url, context)
        logger.warning(...)
        return engine, engine_name

    preferred_engine = self._engine_map[user_preference]
    logger.info(...)
    return preferred_engine, user_preference
```

### 4.5 修改函数：`EngineSelector.predict()`

当前位置：[`EngineSelector.predict()`](core/engine_selector.py:542)。

目的：UI 显示的预测引擎应和真正执行时一致，避免界面显示 Aria2，实际执行切到别的引擎造成困惑。

修改点：

1. 在处理 user_preference 前增加同样的 Aria2 unsafe 判断。
2. 如果手动 Aria2 不安全，返回安全替代引擎。
3. 日志 event 建议与 [`EngineSelector.select()`](core/engine_selector.py:595) 区分，例如 `predict_aria2_overridden_for_context`。

### 4.6 保持函数：`EngineSelector.get_candidates()`

当前位置：[`EngineSelector.get_candidates()`](core/engine_selector.py:479)。

现有逻辑已经对 segment context 跳过 Aria2，应尽量不破坏。

只需确认：

1. `is_segment_context` 时跳过 Aria2 的逻辑保留。
2. HLS/MPD context 时 [`select_engine()`](core/engine_selector.py:354) 优先返回 N_m3u8DL-RE。
3. 新增手动 Aria2 覆盖逻辑不应改变普通自动候选顺序。

## Step 3：下载完成后校验 Aria2 产物

### 5.1 修改文件

- [`core/download/manager.py`](core/download/manager.py:1)

### 5.2 新增函数：`_task_output_candidates(task: DownloadTask) -> list[Path]`

位置建议：[`DownloadManager`](core/download/manager.py:262) 类内，靠近 [`_execute_download()`](core/download/manager.py:1588) 或工具方法区。

职责：根据任务信息查找可能的下载产物。

候选顺序建议：

1. 如果存在内部运行态属性 `_aria2_output_filename`，优先加入 `save_dir / _aria2_output_filename`。
2. 加入 `save_dir / task.filename`。
3. 加入 `save_dir.glob(f"{task.filename}.*")` 匹配项。
4. 去重，只保留文件。
5. 排序时媒体扩展优先。

伪代码：

```python
def _task_output_candidates(self, task: DownloadTask) -> list[Path]:
    save_dir = Path(str(getattr(task, "save_dir", "") or ""))
    filename = str(getattr(task, "filename", "") or "").strip()
    if not save_dir.exists() or not filename:
        return []

    candidates = []
    aria2_name = str(getattr(task, "_aria2_output_filename", "") or "").strip()
    if aria2_name:
        candidates.append(save_dir / aria2_name)

    candidates.append(save_dir / filename)
    candidates.extend(save_dir.glob(f"{filename}.*"))
    return _dedup_existing_files_sorted(candidates)
```

### 5.3 新增函数：`_read_artifact_head(path: Path, size: int = 512) -> bytes`

职责：读取文件头用于轻量判断。

要求：

1. 异常时返回空 bytes。
2. 不读取大文件。
3. 不记录完整路径中的敏感 token，仅记录文件名。

### 5.4 新增函数：`_classify_artifact_head(head: bytes) -> str`

职责：识别明显不应当视为最终媒体的文件。

建议返回值：

- `hls_manifest`
- `dash_manifest`
- `html_error`
- `json_error`
- `mp4`
- `matroska`
- `mpeg_ts`
- `unknown`

判断规则：

1. 去掉 UTF-8 BOM 与首部空白后，以 `#EXTM3U` 开头：`hls_manifest`。
2. 以 `<MPD` 或包含 DASH MPD 头部：`dash_manifest`。
3. 以 `<html`、`<!doctype html` 开头：`html_error`。
4. 以 `{` 或 `[` 开头且像错误响应：可暂判 `json_error`。
5. bytes 4 到 12 附近包含 `ftyp`：`mp4`。
6. bytes 0 到 4 是 Matroska EBML magic：`matroska`。
7. TS sync byte：第 0、188、376 位能看到 `0x47` 时：`mpeg_ts`。
8. 其他：`unknown`。

### 5.5 新增函数：`_validate_aria2_artifact(task: DownloadTask) -> tuple[bool, str]`

位置建议：[`DownloadManager`](core/download/manager.py:262) 类内。

职责：Aria2 返回成功后，判断是否可以接受为最终完成。

判断逻辑：

1. 查找候选产物；无候选返回 `(False, "aria2_no_output_artifact")`。
2. 如果 `task.resource_type` 是 HLS/DASH：返回 False。
3. 如果 `task.resource_type` 是 segment 且有 page/master/media/source 上下文：返回 False。
4. 读取首个候选文件头并分类。
5. 如果分类是 `hls_manifest`、`dash_manifest`、`html_error`、`json_error`：返回 False。
6. 如果分类是 `mp4`、`matroska`、`mpeg_ts`：返回 True。
7. 如果分类是 `unknown`：
   - 文件名有明确媒体扩展时可以暂时 True。
   - 文件无扩展名且无法推断时返回 False 或 warning。推荐第一版返回 False，避免误判。

伪代码：

```python
def _validate_aria2_artifact(self, task: DownloadTask) -> tuple[bool, str]:
    candidates = self._task_output_candidates(task)
    if not candidates:
        return False, "aria2_no_output_artifact"

    if self._task_context_is_hls_or_segment(task):
        return False, "aria2_not_allowed_for_hls_or_segment"

    artifact = candidates[0]
    kind = self._classify_artifact_head(self._read_artifact_head(artifact))
    if kind in {"hls_manifest", "dash_manifest", "html_error", "json_error"}:
        return False, f"aria2_invalid_artifact:{kind}"
    if kind in {"mp4", "matroska", "mpeg_ts"}:
        return True, kind
    if artifact.suffix.lower() in MEDIA_SUFFIXES:
        return True, "extension_trusted"
    return False, "aria2_unknown_suffixless_artifact"
```

### 5.6 修改函数：局部函数 `_try_download()`

当前位置：[`_try_download()`](core/download/manager.py:1958)。

修改点：

1. 当前直接返回 `selected_engine.download(...)`。
2. 改为先接收 `ok`。
3. 如果 `ok` 且 `engine_name == "Aria2"`，调用 [`_validate_aria2_artifact()`](core/download/manager.py:1588)。
4. 校验失败时：
   - 设置 `task.error_message`。
   - 记录 warning 日志。
   - 返回 False，使现有 fallback 逻辑尝试下一个候选引擎。
5. 校验成功时返回 True。

伪代码：

```python
ok = selected_engine.download(task, progress_callback)
if ok and engine_name == "Aria2":
    valid, reason = self._validate_aria2_artifact(task)
    if not valid:
        task._set_fields_locked(error_message=reason)
        logger.warning(...)
        return False
return ok
```

注意：

- 不要在校验失败时删除文件。第一版只阻止误判完成，并允许 fallback。
- 如果 fallback 成功，应保留日志说明是从 Aria2 产物校验失败恢复。
- 如果用户明确只配置了 Aria2，最终失败信息应明确提示当前资源不是直链媒体，应使用 N_m3u8DL-RE 或 yt-dlp。

## Step 4：优化后处理文件查找，避免优先命中无扩展名旧文件

### 6.1 修改文件

- [`ui/main_window_postprocess.py`](ui/main_window_postprocess.py:1)

### 6.2 修改函数：`MainWindowPostprocessMixin._find_task_output_file()`

当前位置：[`MainWindowPostprocessMixin._find_task_output_file()`](ui/main_window_postprocess.py:27)。

现状：

1. 先检查 `save_dir / filename`。
2. 如果存在无扩展名文件，直接返回。
3. 这会导致即使旁边有 `filename.mp4`，也可能优先拿到旧的无扩展名残留。

新规则建议：

1. 如果存在内部运行态实际产物路径，例如 `_aria2_output_filename`，且文件存在，优先返回。
2. 查找 `filename.*` 匹配项。
3. 如果有媒体扩展名匹配项，优先返回媒体扩展名匹配项。
4. 如果没有媒体扩展匹配项，再返回 `save_dir / filename`。
5. 最后返回其他匹配项。

伪代码：

```python
actual_name = str(getattr(task, "_aria2_output_filename", "") or "").strip()
if actual_name:
    actual = save_dir / actual_name
    if actual.is_file():
        return actual

matches = [p for p in save_dir.glob(f"{filename}.*") if p.is_file()]
media_matches = [p for p in matches if p.suffix.lower() in preferred_suffixes]
if media_matches:
    return sorted(media_matches, key=...)[0]

direct = save_dir / filename
if direct.is_file():
    return direct

if matches:
    return sorted(matches, key=...)[0]
return None
```

### 6.3 修改/新增测试

修改 [`test_find_task_output_prefers_existing_direct_filename()`](tests/test_download_queue_panel_logic.py:364)：

- 当前预期是 direct suffixless 优先。
- 新预期应改为：如果同时存在 `movie` 和 `movie.mp4`，返回 `movie.mp4`。

新增测试：

1. 只有 `movie` 时仍返回 `movie`。
2. 同时有 `movie.txt` 和 `movie.mp4` 时返回 `movie.mp4`。
3. 如果 `task._aria2_output_filename = "movie.webm"` 且文件存在，应优先返回 `movie.webm`。

## Step 5：测试计划

### 7.1 Aria2 输出名单元测试

建议新增或扩展 [`tests/test_engine_argv_safety.py`](tests/test_engine_argv_safety.py:1)。

覆盖用例：

1. 直链 mp4：
   - 输入 URL：`https://cdn.example.com/video.mp4`
   - `task.filename = "video"`
   - 期望 [`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 中 `-o` 后的参数是 `video.mp4`。

2. 直链 mp4 带 query：
   - URL：`https://cdn.example.com/video.mp4?token=abc`
   - 期望输出名仍是 `video.mp4`。

3. 文件名已有后缀：
   - `task.filename = "video.mp4"`
   - 期望不变，不生成 `video.mp4.mp4`。

4. m3u8 不补扩展：
   - URL：`https://cdn.example.com/index.m3u8`
   - `resource_type = "hls"`
   - 期望 Aria2 输出名不补 `.m3u8`，并且后续选择层应阻止 Aria2。

5. 带上下文 ts 分片不补 `.ts`：
   - URL：`https://cdn.example.com/seg-1.ts`
   - `resource_type = "segment"`
   - `page_url` 或 `master_url` 存在。
   - 期望不按直链最终媒体处理。

6. 裸完整 ts 允许补 `.ts`：
   - URL：`https://cdn.example.com/archive.ts`
   - 无 page/master/media/source 上下文。
   - 期望输出名是 `archive.ts` 或 `task.filename + ".ts"`。

### 7.2 引擎选择测试

扩展 [`tests/test_engine_selector.py`](tests/test_engine_selector.py:1)。

新增/调整用例：

1. 手动 Aria2 + HLS context：
   - `user_preference = "Aria2"`
   - `context.resource_type = "hls"`
   - 期望 [`EngineSelector.select()`](core/engine_selector.py:595) 返回 N_m3u8DL-RE。

2. 手动 Aria2 + segment + playlist hint：
   - `context.resource_type = "segment"`
   - `master_url` 存在。
   - 期望返回 N_m3u8DL-RE。

3. 手动 Aria2 + segment + page context：
   - `context.resource_type = "segment"`
   - `page_url` 存在。
   - 期望返回 yt-dlp 或现有自动候选第一项。

4. 手动 Aria2 + 裸完整 ts：
   - 无 context 或 context 无 page/master/media/source。
   - 期望仍允许 Aria2。

5. 普通手动选择行为不变：
   - 对非 Aria2 引擎，保留用户选择优先。
   - 避免破坏 [`test_select_prefers_user_engine_even_if_can_handle_returns_false()`](tests/test_engine_selector.py:60) 的意图；如该测试与新逻辑冲突，只针对 Aria2 unsafe 场景调整，不扩大覆盖范围。

### 7.3 下载管理器校验测试

建议扩展 [`tests/test_download_manager_state_machine.py`](tests/test_download_manager_state_machine.py:1) 或新增 `tests/test_download_manager_aria2_artifact.py`。

测试用例：

1. Aria2 返回成功，但产物内容是 `#EXTM3U`：
   - 期望 [`_try_download()`](core/download/manager.py:1958) 视为失败。
   - 若候选中有 N_m3u8DL-RE，期望触发 fallback。

2. Aria2 返回成功，产物是 MP4 文件头：
   - 期望校验通过，任务完成。

3. Aria2 返回成功，产物是 HTML 错误页：
   - 期望校验失败，错误原因包含 `html_error`。

4. Aria2 返回成功，任务上下文是 segment + page_url：
   - 即使文件存在，也不应视为最终完成。

5. 裸完整 TS：
   - 文件头符合 TS sync byte。
   - 期望校验通过。

### 7.4 后处理查找测试

扩展 [`tests/test_download_queue_panel_logic.py`](tests/test_download_queue_panel_logic.py:1)。

测试用例：

1. `movie` 和 `movie.mp4` 同时存在：返回 `movie.mp4`。
2. 只有 `movie` 存在：返回 `movie`。
3. `movie.txt` 和 `movie.webm` 同时存在：返回 `movie.webm`。
4. `_aria2_output_filename` 指向 `movie.mkv`：优先返回 `movie.mkv`。

### 7.5 回归测试命令

在 Windows PowerShell、项目根目录执行：

```powershell
python -m pytest tests/test_engine_selector.py tests/test_engine_argv_safety.py tests/test_download_queue_panel_logic.py tests/test_download_manager_state_machine.py
```

完整回归：

```powershell
python -m pytest
```

## Step 6：手工验证清单

### 8.1 直链 MP4

操作：

1. 使用直链 `https://example.com/video.mp4?token=xxx`。
2. 手动选择 Aria2。
3. 下载完成。

预期：

1. 产物为 `标题.mp4`。
2. 不出现无扩展名文件。
3. 下载队列显示完成。
4. 后处理“转 MP4/remux”能选中 `标题.mp4`。

### 8.2 HLS m3u8 手动 Aria2

操作：

1. 使用 m3u8 URL。
2. 手动选择 Aria2。
3. 点击下载。

预期：

1. 实际执行引擎被切换到 N_m3u8DL-RE 或 yt-dlp。
2. 日志出现手动 Aria2 被覆盖的 warning。
3. 不生成只有 manifest 内容的“完成文件”。

### 8.3 嗅探到的 TS 分片

操作：

1. 使用内置浏览器打开 HLS 页面。
2. 资源列表中出现 ts/m4s segment。
3. 手动选择 Aria2 下载该资源。

预期：

1. 如果有 master/media/page/source 上下文，不直接使用 Aria2。
2. 选择 N_m3u8DL-RE 或 yt-dlp。
3. 不产生单片段完成任务。

### 8.4 裸完整 TS

操作：

1. 使用一个明确完整的 `.ts` 文件直链。
2. 手动选择 Aria2。

预期：

1. Aria2 允许执行。
2. 输出文件带 `.ts`。
3. 校验通过。

### 8.5 历史无扩展名残留

操作：

1. 在下载目录中放置旧文件 `movie`。
2. 新下载生成 `movie.mp4`。
3. 对任务执行 FFmpeg 后处理。

预期：

1. 后处理优先选择 `movie.mp4`。
2. 不再优先选择旧的无扩展名 `movie`。

## Step 7：日志与错误提示建议

### 9.1 Aria2 输出名日志

位置：[`Aria2Engine._build_command()`](engines/aria2_engine.py:173)。

建议 event：`aria2_output_filename_resolved`。

字段：

- `task_filename`
- `output_filename`
- `url_suffix`
- `resource_type`
- `mime`

### 9.2 手动 Aria2 覆盖日志

位置：[`EngineSelector.select()`](core/engine_selector.py:595) 与 [`EngineSelector.predict()`](core/engine_selector.py:542)。

建议 event：

- `manual_aria2_overridden_for_context`
- `predict_aria2_overridden_for_context`

字段：

- `preferred_engine`
- `replacement_engine`
- `resource_type`
- `source`
- `reason`

### 9.3 Aria2 产物校验失败日志

位置：[`_try_download()`](core/download/manager.py:1958) 或新增的 [`_validate_aria2_artifact()`](core/download/manager.py:1588)。

建议 event：`aria2_artifact_validation_failed`。

字段：

- `filename`
- `artifact_name`
- `reason`
- `resource_type`
- `next_action`，例如 `fallback` 或 `fail`

## Step 8：兼容性与风险控制

### 10.1 双扩展名风险

风险：如果直接修改 [`DownloadTask.filename`](core/task_model.py:228)，yt-dlp 可能生成 `movie.mp4.mp4`。

规避：

- 不修改 [`DownloadTask.filename`](core/task_model.py:228)。
- 只在 [`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 内生成 Aria2 专用输出名。

### 10.2 `.ts` 误判风险

风险：`.ts` 既可能是完整媒体，也可能是 HLS 单分片。

规避：

- 有 page/master/media/source 上下文时按 segment 处理，不让 Aria2 接。
- 无上下文裸 `.ts` 保持兼容，允许 Aria2。
- 下载完成后用文件头进行轻量校验。

### 10.3 手动选择语义变化

风险：用户手动选 Aria2，但实际执行被切换。

规避：

- 只在明显 unsafe 的 HLS/MPD/segment 上下文覆盖。
- 日志明确说明原因。
- UI 预测也同步覆盖，减少用户困惑。

### 10.4 旧文件残留风险

风险：同目录已有旧无扩展名文件，后处理可能选错。

规避：

- 修改 [`MainWindowPostprocessMixin._find_task_output_file()`](ui/main_window_postprocess.py:27)，优先媒体扩展文件。
- 可选：记录 Aria2 实际输出名 `_aria2_output_filename`，后处理优先读取。

## Step 9：验收标准

开发完成后，必须满足：

1. 直链 mp4/webm/mkv/flv/mov 等通过 Aria2 下载后带扩展名。
2. 已有扩展名的任务名不会被追加第二个扩展名。
3. m3u8/mpd 不会被手动 Aria2 当最终媒体直接完成。
4. 带 page/master/media/source 上下文的 ts/m4s segment 不会被 Aria2 单片下载后误判完成。
5. 裸完整 `.ts` 直链仍可通过 Aria2 下载并保留 `.ts`。
6. Aria2 下载到 `#EXTM3U`、DASH MPD、HTML 错误页时不会标记为成功完成。
7. 后处理优先选择 `movie.mp4`、`movie.webm`、`movie.mkv` 等带媒体扩展名文件，而不是旧的 `movie` 无扩展名文件。
8. [`tests/test_engine_selector.py`](tests/test_engine_selector.py:1)、[`tests/test_engine_argv_safety.py`](tests/test_engine_argv_safety.py:1)、[`tests/test_download_queue_panel_logic.py`](tests/test_download_queue_panel_logic.py:1)、[`tests/test_download_manager_state_machine.py`](tests/test_download_manager_state_machine.py:1) 相关测试通过。
9. 全量 `pytest` 通过。

## Step 10：建议实施顺序

1. 在 [`engines/aria2_engine.py`](engines/aria2_engine.py:1) 添加输出名推断 helper，并修改 [`Aria2Engine._build_command()`](engines/aria2_engine.py:173)。
2. 为 Aria2 输出名新增单元测试。
3. 在 [`core/engine_selector.py`](core/engine_selector.py:1) 添加手动 Aria2 unsafe 覆盖逻辑，修改 [`EngineSelector.select()`](core/engine_selector.py:595) 和 [`EngineSelector.predict()`](core/engine_selector.py:542)。
4. 为引擎选择新增/调整测试。
5. 在 [`core/download/manager.py`](core/download/manager.py:1) 添加 Aria2 产物校验 helper，并接入 [`_try_download()`](core/download/manager.py:1958)。
6. 为 Aria2 manifest/HTML/MP4/TS 产物校验新增测试。
7. 修改 [`MainWindowPostprocessMixin._find_task_output_file()`](ui/main_window_postprocess.py:27)，更新后处理文件选择测试。
8. 跑局部测试。
9. 跑全量测试。
10. 手工验证 5 个核心场景：直链 MP4、m3u8 手动 Aria2、嗅探 TS 分片、裸完整 TS、历史无扩展名残留。

## 11. 第一版可接受裁剪

如果开发排期紧，可以第一版只做以下内容：

1. [`Aria2Engine._build_command()`](engines/aria2_engine.py:173) 对直链媒体补扩展名。
2. [`EngineSelector.select()`](core/engine_selector.py:595) 阻止手动 Aria2 接 HLS/segment。
3. [`MainWindowPostprocessMixin._find_task_output_file()`](ui/main_window_postprocess.py:27) 优先媒体扩展文件。
4. 补对应单元测试。

第二版再补 [`DownloadManager._execute_download()`](core/download/manager.py:1588) 的产物校验与 fallback。这样可以先快速解决主要用户可见问题，同时降低第一轮改动风险。
