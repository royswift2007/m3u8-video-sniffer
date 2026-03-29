# DEBUG S5 报告（内置浏览器 m3u8 抓取链路成功率增强）

## 本轮状态（2026-03-14）
- 状态：通过（S5 实施 + 量化工具链 + 真实样本流程模板已落地）

## 本轮完成
1. 防重复事件绑定
- 文件：`core/playwright_driver.py`
- 改动：新增 `_remember_page_configured()` 与 `_configured_page_ids`，避免同一页面重复注册事件造成重复抓取和噪音日志。

2. 嵌套 m3u8 解析增强
- 文件：`core/m3u8_parser.py`
- 改动：
  - 新增嵌套深度限制（`features.m3u8_nested_depth`，默认 3，范围 1~5）
  - 新增循环引用检测（visited set）
  - 嵌套失败使用结构化日志字段

3. S5 烟测脚本
- 文件：`scripts/s5_smoke.py`
- 覆盖：
  - Playwright 页面配置去重守卫
  - M3U8 嵌套循环检测与有界性
  - N_m3u8DL-RE primary/media 候选回退链路存在性

4. 指标脚本与对比脚本
- 文件：
  - `scripts/s5_metrics_from_logs.py`
  - `scripts/s5_compare_metrics.py`
- 功能：
  - 单批次日志指标聚合
  - 基线/候选两批日志自动对比并输出 Markdown 报告

5. 真实样本回归执行模板与校验
- 文件：
  - `plans/real_sample_regression_checklist.md`
  - `plans/test_samples.md`
  - `scripts/s5_sample_sheet_check.py`
- 功能：
  - 标准化 20 条样本模板（分类覆盖）
  - 自动校验样本表结构与最小数量门禁

## 验证记录
1. 语法检查
- `python -m compileall core\playwright_driver.py core\m3u8_parser.py scripts\s5_smoke.py scripts\s5_metrics_from_logs.py scripts\s5_compare_metrics.py scripts\s5_sample_sheet_check.py`
- `python -m py_compile core\playwright_driver.py core\m3u8_parser.py scripts\s5_smoke.py scripts\s5_metrics_from_logs.py scripts\s5_compare_metrics.py scripts\s5_sample_sheet_check.py`

2. 烟测
- `python scripts/s5_smoke.py` -> PASS

3. 指标与对比演示
- 单批次：
  - `python scripts/s5_metrics_from_logs.py logs/m3u8sniffer_20260314.log logs/m3u8sniffer_20260313.log`
- 前后对比：
  - `python scripts/s5_compare_metrics.py --baseline logs/m3u8sniffer_20260313.log --candidate logs/m3u8sniffer_20260314.log --out plans/phase_reports/S5_compare_report.md`
- 报告：`plans/phase_reports/S5_compare_report.md`

4. 样本模板校验演示
- `python scripts/s5_sample_sheet_check.py --file plans/test_samples.md --min 20`
- 结果：结构 PASS，当前 `missing_url_rows=20`（待填真实样本 URL）

## 结果解读
- 演示对比仅用于验证工具链有效，不代表真实业务结论（日志包含开发期任务）。
- 已具备“样本填表 -> 运行批次 -> 指标对比 -> 报告输出”的完整流程。

## 结论
- S5 当前开发目标达成：
  - 抓取链路稳定性增强（防重复绑定）
  - 解析链路鲁棒性增强（深度受控、防环）
  - 回退链路可用（primary/media）
  - 指标统计与前后对比闭环已建立
  - 真实样本批次执行模板可直接启用
## 追加（两样本实测）
- 样本：
  - `https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8`
  - `https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8`
- 报告：`plans/phase_reports/S5_two_url_probe_report.md`
- 结果：两条都在 `playlist` 阶段失败，错误为 `WinError 10013`（当前执行环境网络访问被限制），属于环境限制而非业务逻辑失败。
- 说明：请在你的本机程序运行环境再执行同脚本复测，以获得真实网络结论。
