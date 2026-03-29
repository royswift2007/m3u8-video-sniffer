# 内置浏览器 m3u8 成功率提升开发方案（分阶段+强制测试门禁）

## 1. 目标与范围
- 目标：提升“程序内置浏览器抓取到的 m3u8 链接”的下载成功率，缩小与外部猫爪插件链路的差距。
- 范围：仅针对 m3u8 相关抓取、解析、入队、下载链路。
- 不在本方案范围：非 m3u8 链路行为调整、UI 大改、引擎大版本升级。

## 2. 强制门禁规则（必须执行）
- 规则 1：每个阶段完成后必须通过该阶段全部测试，才允许进入下一阶段开发。
- 规则 2：任何阶段测试未通过，禁止继续后续阶段，必须先修复并重测。
- 规则 3：每阶段结束必须产出“测试记录”并归档到 `plans/phase_reports/`。
- 规则 4：每阶段上线前必须完成一次回归，覆盖前序所有已完成阶段的关键用例。

## 3. 已校对并修正的计划风险
- 风险 A：下载前探测逻辑不应耦合到 UI 线程类 `M3U8FetchThread`。
  结论：新增独立服务模块 `core/services/hls_probe.py`。
- 风险 B：`PlaywrightDriver` 中 `export_cookies_to_file` 存在重复定义。
  结论：先统一为一个实现，避免后续调用混乱。
- 风险 C：`cookie_exporter` 在主窗口已赋值，但 `YtdlpEngine` 未实际使用。
  结论：若进入 yt-dlp 相关阶段，需补“实际调用路径”。
- 风险 D：当前仓库缺少 `tests/` 自动化目录。
  结论：阶段测试先采用“脚本化回归 + 日志校验”，并在后续补齐自动化测试骨架。

## 4. 基线与统一测试标准（所有阶段通用）
- 基线样本：准备至少 20 条 m3u8 样本（成功样本、鉴权样本、失效样本、跨域样本）。
- 基线指标：
1. 抓取命中率（内置浏览器检测到可下载 m3u8 的比例）
2. 下载成功率（任务最终完成比例）
3. 首次成功耗时（从点击下载到开始稳定下载）
4. 失败阶段分布（playlist/key/segment/merge）
- 通用通过线（每阶段最低要求）：
1. 本阶段目标指标提升，且不低于前阶段
2. 非 m3u8 链路无新增回归
3. 关键日志字段完整（task_id/url/stage/reason）

## 5. 阶段实施计划（按收益排序）

### 阶段 S0：基础修复与观测打底（前置阶段）
- 目标：修正已知结构问题，建立可追踪日志。
- 代码改动点：
1. [core/playwright_driver.py](C:/Users/qinghua/Documents/M3U8D/core/playwright_driver.py)：合并重复 `export_cookies_to_file`
2. [utils/logger.py](C:/Users/qinghua/Documents/M3U8D/utils/logger.py)：补充结构化日志字段规范
3. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：增加阶段日志标记（stage/reason）
- 测试项：
1. Cookie 导出功能可用且仅存在一个有效实现
2. 下载失败日志能区分阶段和原因
3. 原有下载流程不受影响
- 通过门禁：
1. 三项测试全通过
2. 产出 `plans/phase_reports/S0_report.md`

### 阶段 S1：同 URL 资源上下文合并（替代首条即丢）
- 目标：避免“先抓到的不完整 headers”覆盖真实可用上下文。
- 代码改动点：
1. [core/m3u8_sniffer.py](C:/Users/qinghua/Documents/M3U8D/core/m3u8_sniffer.py)：`add_resource` 去重改为合并策略
2. [ui/resource_panel.py](C:/Users/qinghua/Documents/M3U8D/ui/resource_panel.py)：去重展示逻辑与 sniffer 行为一致
- 测试项：
1. 同 URL 多次捕获后，最终 headers 为最完整版本
2. 入队任务使用合并后的 headers
3. 资源列表不出现异常重复
- 通过门禁：
1. 抓取命中率或下载成功率有可观测提升
2. `S1_report.md` 完成并评审通过

### 阶段 S2：下载前 HLS 探测（playlist/key/segment）
- 目标：将“不可下载链接”在入队前筛出，并定位失败阶段。
- 代码改动点：
1. 新增 [core/services/hls_probe.py](C:/Users/qinghua/Documents/M3U8D/core/services/hls_probe.py)
2. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：下载前探测接入
- 测试项：
1. 可输出三段探测结果（playlist/key/segment）
2. 401/403 可在探测阶段被识别
3. 无效任务入队比例下降
- 通过门禁：
1. 探测误杀率可接受（需在报告中量化）
2. `S2_report.md` 通过

### 阶段 S3：重试策略重排（鉴权优先，同引擎优先）
- 目标：减少“本可修复却直接换引擎”导致的失败。
- 代码改动点：
1. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：`_classify_failure` 和 `_execute_download`
2. [engines/n_m3u8dl_re.py](C:/Users/qinghua/Documents/M3U8D/engines/n_m3u8dl_re.py)：配合鉴权重试参数
- 测试项：
1. 401/403 先走补头重试，再回退引擎
2. 暂停/取消不触发错误回退
3. 与 S2 联动稳定
- 通过门禁：
1. 鉴权类失败下降
2. `S3_report.md` 通过

### 阶段 S4：内置浏览器“播放后抓取窗口”
- 目标：捕获真正可下载的最终 m3u8，而非过渡链接。
- 代码改动点：
1. [core/playwright_driver.py](C:/Users/qinghua/Documents/M3U8D/core/playwright_driver.py)：播放后 10-15 秒持续捕获窗口
2. [core/sniffer_script.py](C:/Users/qinghua/Documents/M3U8D/core/sniffer_script.py)：动态资源监听补强
- 测试项：
1. 播放后候选链接数量和质量提升
2. 过渡链接占比下降
3. 性能与稳定性可接受
- 通过门禁：
1. 抓取有效率提升
2. `S4_report.md` 通过

### 阶段 S5：master/media 双链路保留与回退
- 目标：单链路失败时可自动切换到另一链路。
- 代码改动点：
1. [core/task_model.py](C:/Users/qinghua/Documents/M3U8D/core/task_model.py)：补字段（master_url/media_url）
2. [ui/main_window.py](C:/Users/qinghua/Documents/M3U8D/ui/main_window.py)：入队时保留双链路
3. [engines/n_m3u8dl_re.py](C:/Users/qinghua/Documents/M3U8D/engines/n_m3u8dl_re.py)：尝试顺序支持
- 测试项：
1. master 失效时可自动改用 media
2. media 失效时可回退 master
3. 历史任务兼容
- 通过门禁：
1. 双链路回退成功
2. `S5_report.md` 通过

### 阶段 S6：候选链接打分与优选
- 目标：将“首条命中”升级为“最佳候选命中”。
- 代码改动点：
1. [core/m3u8_sniffer.py](C:/Users/qinghua/Documents/M3U8D/core/m3u8_sniffer.py)：候选池和评分字段
2. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：下载前优选逻辑
- 测试项：
1. 候选打分可解释（日志可见）
2. 自动选择成功率高于首条策略
3. 无明显性能退化
- 通过门禁：
1. 优选策略效果达标
2. `S6_report.md` 通过

### 阶段 S7：站点规则自动学习（可回滚）
- 目标：让程序从成功任务中沉淀规则，长期提升成功率。
- 代码改动点：
1. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：成功后学习入口
2. [utils/config_manager.py](C:/Users/qinghua/Documents/M3U8D/utils/config_manager.py)：`site_rules_auto` 读写
- 测试项：
1. 自动学习不覆盖人工规则
2. 学习规则可禁用、可清理
3. 回放历史样本有效
- 通过门禁：
1. 自动学习可控且有效
2. `S7_report.md` 通过

### 阶段 S8：统计与回归体系固化
- 目标：形成稳定迭代闭环，确保后续变更可量化。
- 代码改动点：
1. [utils/logger.py](C:/Users/qinghua/Documents/M3U8D/utils/logger.py)：指标日志格式固化
2. [core/download_manager.py](C:/Users/qinghua/Documents/M3U8D/core/download_manager.py)：阶段统计输出
3. 新增 `plans/phase_reports/metrics_template.md`
- 测试项：
1. 可按域名/阶段/引擎统计成功率
2. 回归模板可复用
3. 文档齐备
- 通过门禁：
1. 阶段统计可复盘
2. `S8_report.md` 通过

## 6. 阶段切换检查单（每阶段结束必须填写）
- 阶段编号：
- 代码提交范围：
- 测试用例总数 / 通过数 / 失败数：
- 关键指标对比（本阶段 vs 上阶段）：
- 已知问题与风险：
- 是否允许进入下一阶段（是/否）：
- 审核人：
- 审核日期：

## 7. 交付要求
- 文档交付：
1. 本方案文档（本文件）
2. 每阶段测试报告：`plans/phase_reports/Sx_report.md`
- 代码交付：
1. 每阶段单独提交，禁止跨阶段混改
2. 阶段未通过前禁止进入后续阶段开发

## 8. 立即执行建议
- 先执行 S0、S1、S2、S3，完成第一波成功率提升。
- 任何阶段未通过门禁，立即停在当前阶段修复，不得跳阶段继续开发。
