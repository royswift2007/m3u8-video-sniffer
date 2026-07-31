# M3U8D 错误码参考

> 本文档集中列出 M3U8D 所有结构化错误码，供用户排查和开发者参考。
> 每个错误码标注：含义、触发条件、用户处置建议。
> 最后更新：2026-06-27

## 目录

- [组件更新错误码](#组件更新错误码)
- [下载管理错误码](#下载管理错误码)
- [安全错误码](#安全错误码)
- [HLS 探测错误码](#hls-探测错误码)
- [依赖安装错误码](#依赖安装错误码)

---

## 组件更新错误码

### `remote_is_prerelease`
- **含义**：远程最新版本是预发布版（beta/alpha/rc），自动升级被抑制
- **触发条件**：远程 `latest_version` 包含 `-beta` / `-alpha` / `-rc` 等 SemVer 预发布后缀，且组件清单未设置 `allow_prerelease: true`
- **用户处置**：等待上游发布正式版；若确需使用预发布版，在 `deps.json` 对应组件的 `update` 节点设置 `"allow_prerelease": true` 后重新检查更新

### `missing_checksum`
- **含义**：远程发布的组件清单未提供校验和
- **触发条件**：GitHub Release 资产缺少 sha256 字段，且无 sidecar `.sha256` 文件
- **用户处置**：等待开发者更新发布清单；可临时选择"跳过校验"（不推荐）

### `checksum_mismatch`
- **含义**：下载文件的 sha256 与预期不符
- **触发条件**：网络传输损坏或供应链攻击
- **用户处置**：重试更新；多次失败请报告安全事件

### `staging_tampered`
- **含义**：暂存目录中的可执行文件在校验后被修改
- **触发条件**：杀毒软件或恶意进程在安装间隙修改文件
- **用户处置**：将 `Temp/` 目录加入杀软白名单后重试

### `insufficient_disk`
- **含义**：目标磁盘剩余空间不足以完成安装
- **触发条件**：`shutil.disk_usage` 返回的 free 值 < 预期下载大小 × `download_disk_headroom_factor`
- **用户处置**：清理磁盘空间；调整 `tunables.download_disk_headroom_factor` 降低预留比例

### `pin_mismatch`
- **含义**：TOFU pin 文件 HMAC 校验失败
- **触发条件**：`~/.m3u8d/component_pins.json` 被篡改或损坏
- **用户处置**：删除 pin 文件后重新执行更新以重建信任

### `version_mismatch`
- **含义**：安装后探测的版本与预期不符
- **触发条件**：旧进程未完全退出导致覆盖安装失败
- **用户处置**：手动终止所有 M3U8D 相关进程后重试

### `deferred_pending_restart`
- **含义**：文件被占用，安装将在下次启动时完成
- **触发条件**：引擎进程持有文件句柄
- **用户处置**：重启 M3U8D 或手动关闭占用进程

### `tofu_pin_tampered`
- **含义**：TOFU pin 文件的 HMAC 签名被破坏
- **触发条件**：pin 文件被外部修改
- **用户处置**：同 `pin_mismatch`

---

## 下载管理错误码

### `worker_exit_timeout`
- **含义**：下载 worker 未在规定时间内优雅退出
- **触发条件**：worker 池关闭时，线程在 `30s` (`tunables.worker_soft_exit_timeout_s`) 内未 join
- **用户处置**：重启应用；可增大 `tunables.worker_soft_exit_timeout_s`

### `invalid_format_id`
- **含义**：用户指定的格式 ID 无效
- **触发条件**：`--select-video` 的 `resolution` 参数包含非法字符
- **用户处置**：检查 m3u8 内容完整性

### `download_disk_precheck_fail`
- **含义**：开始下载前磁盘空间不足
- **触发条件**：估算的任务大小 × 1.2 > `shutil.disk_usage.free`
- **用户处置**：释放磁盘空间或关闭预检

### `engine_all_failed`
- **含义**：所有可用引擎均下载失败
- **触发条件**：任务遍历引擎列表后无一成功
- **用户处置**：检查网络/代理设置；查看日志确认具体引擎错误

---

## 安全错误码

### `ssrf_blocked`
- **含义**：目标 URL 解析为内网/保留 IP，连接被阻断
- **触发条件**：DNS 解析结果命中 RFC1918 / 回环 / 链路本地地址段
- **用户处置**：若确认目标为可信内网镜像，在 `config.json` 设置 `security.allow_private_networks: true`（注意日志会记录 `ssrf_private_allowed` 告警）

### `cookie_file_outside_trusted_root`
- **含义**：yt-dlp 的 `_cookie_file` 参数指向非受信目录
- **触发条件**：恶意 `m3u8dl://` 载荷试图读取任意本地文件
- **用户处置**：无需处理（安全拦截，已记录日志）

### `bind_timeout`
- **含义**：CatCatch 服务器在超时内未能绑定端口
- **触发条件**：端口 9527–9539 全部被占用
- **用户处置**：关闭占用端口的程序；调整 `tunables.catcatch_port_range_start` / `end`

---

## HLS 探测错误码

### `hls_probe_soft_fail`
- **含义**：TS 分段探测失败但播放列表解析成功
- **触发条件**：部分 CDN 对分段 HEAD 请求返回 403/404，但 m3u8 本身可访问
- **用户处置**：无需处理（引擎仍能正常下载）

### `hls_probe_hard_fail`
- **含义**：m3u8 播放列表本身不可访问
- **触发条件**：主 m3u8 URL 返回 4xx/5xx 或 DNS 失败
- **用户处置**：检查 URL 有效性；确认是否需要 cookie/代理

---

## 依赖安装错误码

### `dependency_download_failed`
- **含义**：首次启动时自动下载依赖工具失败
- **触发条件**：网络不通或 GitHub 不可达
- **用户处置**：手动从 [Releases](https://github.com/) 下载并放入 `bin/` 目录

### `dependency_install_failed`
- **含义**：依赖工具安装/升级后版本探测失败
- **触发条件**：引擎 exe 损坏或不兼容
- **用户处置**：删除 `bin/` 目录下对应 exe，重启以触发重新下载

---

## 通知服务错误码

### `notif_service_no_qapp`
- **含义**：NotificationService 初始化时 QApplication 未运行
- **触发条件**：在 Qt 事件循环启动前调用通知
- **用户处置**：无需处理（自动回退到日志记录）

### `notif_service_tray_unavailable`
- **含义**：系统托盘不可用
- **触发条件**：无桌面环境（如远程桌面会话）
- **用户处置**：无需处理（通知记录在日志面板）
