# 后续增强分步开发方案（抓包稳定性 / 下载成功率 / UI体验）

> 目标：围绕三条主线提供可执行的阶段计划。每个阶段均包含目标、核心改动点、验证要点。

## 0. 准备阶段：基线与开关

- [x] 0.1 增加功能开关与默认值
  - 目标：让新逻辑可控、可回滚。
  - 改动：在 [`config.json`](../config.json) 中新增 `sniffer`/`download`/`ui` 相关开关与阈值；在 [`utils/config_manager.py`](../utils/config_manager.py) 添加默认读取。
  - 验证：启动应用不报错，配置缺省时使用默认值。

- [x] 0.2 日志分级与关键指标
  - 目标：便于定位“抓包丢失/下载失败/用户操作路径”。
  - 改动：在 [`utils/logger.py`](../utils/logger.py) 中补充关键事件日志；对下载失败、重试、引擎切换做结构化日志。
  - 验证：日志包含 `event=sniffer_hit`、`event=download_retry` 等关键字段。

---

## 1. 抓包稳定性提升

### 1.1 采集策略增强
- [x] 1.1.1 请求拦截规则细化
  - 目标：降低误抓与漏抓。
  - 改动：在 [`core/request_interceptor.py`](../core/request_interceptor.py) 和 [`core/m3u8_sniffer.py`](../core/m3u8_sniffer.py) 中：
    - 扩展关键后缀与 MIME 判定；
    - 添加“无效/噪音 URL 过滤规则”与白名单域名策略；
    - 对 `page_url`/`referer` 为空的请求按规则补齐。
  - 验证：抓包条目减少噪音且不丢主流平台资源。

- [x] 1.1.2 去重策略升级
  - 目标：减少重复资源且不误删不同清晰度。
  - 改动：在 [`ui/resource_panel.py`](../ui/resource_panel.py) 中升级 `dedup_key` 生成：
    - 对 `master.m3u8` 与 `variant.m3u8` 设不同键；
    - 对 `page_url` 相同但标题不同，允许新增。
  - 验证：同一视频多清晰度仍可区分，重复采集显著减少。

### 1.2 站点规则精细化
- [x] 1.2.1 站点规则扩展
  - 目标：对难抓站点（Referer/UA/Token）提升成功率。
  - 改动：在 [`config.json`](../config.json) 的 `site_rules` 中增加常见站点模板；
    - 在 [`core/m3u8_sniffer.py`](../core/m3u8_sniffer.py) 应用优先级：显式规则 > 站点模板 > 默认。
  - 验证：指定站点资源抓取成功率提升。

---

## 2. 下载成功率提升

### 2.1 引擎参数优化
- [x] 2.1.1 N_m3u8DL-RE 参数自适应
  - 目标：弱网/限速环境稳定。
  - 改动：在 [`engines/n_m3u8dl_re.py`](../engines/n_m3u8dl_re.py) 中加入 `--adaptive`、`--max-retry` 等可配置项。
  - 验证：失败率下降，错误日志清晰可定位。

- [x] 2.1.2 yt-dlp/streamlink 失败诊断增强
  - 目标：定位“403/geo/签名失效”等。
  - 改动：在 [`engines/ytdlp_engine.py`](../engines/ytdlp_engine.py) 与 [`engines/streamlink_engine.py`](../engines/streamlink_engine.py) 中提取失败日志段并提示。
  - 验证：失败时 UI/日志给出明确建议（cookie/referer/geo）。

### 2.2 失败重试策略升级
- [x] 2.2.1 分级重试与引擎切换
  - 目标：减少“无效重试”。
  - 改动：在 [`core/download_manager.py`](../core/download_manager.py) 中按错误类型进行重试：
    - 401/403 => 先更换 headers/cookie 再重试；
    - 解析失败 => 切换引擎；
    - 超时 => 指数退避。
  - 验证：失败任务更快收敛到可下载的引擎。

- [x] 2.2.2 历史记录重放增强
  - 目标：一键重下时保留上下文。
  - 改动：在 [`ui/history_panel.py`](../ui/history_panel.py) 中保留更多字段（引擎、header、variant）。
  - 验证：历史重下成功率提升。

---

## 3. UI 体验改进

### 3.1 资源列表交互优化
- [x] 3.1.1 搜索/过滤
  - 目标：快速筛选资源。
  - 改动：在 [`ui/resource_panel.py`](../ui/resource_panel.py) 添加搜索栏与过滤下拉（类型/来源/清晰度）。
  - 验证：输入关键词可实时过滤表格。

- [x] 3.1.2 批量操作
  - 目标：多资源一键下载/移除。
  - 改动：在 [`ui/resource_panel.py`](../ui/resource_panel.py) 增加多选与批量下载按钮。
  - 验证：多选后一次性发起任务。

### 3.2 下载队列增强
- [x] 3.2.1 队列分组视图
  - 目标：区分“下载中/失败/完成”。
  - 改动：在 [`ui/download_queue.py`](../ui/download_queue.py) 增加分组展示或状态筛选按钮。
  - 验证：切换过滤可显著减少视觉噪音。

### 3.3 历史与日志联动
- [x] 3.3.1 历史条目关联日志
  - 目标：快速定位失败原因。
  - 改动：在 [`ui/history_panel.py`](../ui/history_panel.py) 右键菜单添加“查看相关日志”。
  - 验证：选中条目可定位日志时间段或关键词。

---

## 4. 里程碑与交付

- [x] 4.1 阶段验收清单
  - 抓包成功率提升（抽样站点成功抓到 M3U8/MPD）
  - 下载成功率提升（失败率下降、可诊断）
  - UI 可用性提升（筛选、批量、状态视图）

- [x] 4.2 文档更新
  - 更新 [`MANUAL.md`](../MANUAL.md) 增加新功能说明
  - 更新 [`README.md`](../README.md) 添加新配置项

---

## 5. 冒烟测试建议（可选）

- [ ] 5.1 抓包：主流站点（YouTube/Bilibili/抖音）
- [ ] 5.2 下载：`master.m3u8` + `variant.m3u8`
- [ ] 5.3 UI：过滤/搜索/批量
