# 真实样本回归执行清单（S5）

## 1. 目标
- 用同一批真实样本，对比“改造前/改造后”的内置浏览器 m3u8 抓取与下载效果。
- 输出可复盘的日志与指标报告。

## 2. 样本要求
- 数量：至少 20 条（建议 30+）。
- 构成：
  - media playlist
  - master playlist（含多码率）
  - AES-128
  - 需要 Referer
  - 需要 Cookie
  - 至少 3 个历史失败样本
- 约束：同站点、同内容、同网络环境下做前后对比。

## 3. 执行前准备
1. 清理干扰
- 关闭旧实例，重启程序。
- 新建日志文件（按日期自动生成）。

2. 固定配置
- 下载目录、并发数、限速、引擎顺序保持一致。
- 若对比前后版本，确保 `config.json` 差异仅限本次改动相关项。

3. 标记批次
- 在报告中记录：
  - 版本号/提交号
  - 执行日期
  - 网络环境
  - 样本列表文件

## 4. 执行步骤
1. 基线批次（改造前）
- 用样本列表跑完整批次。
- 保存日志：`logs/m3u8sniffer_YYYYMMDD.log`。

2. 候选批次（改造后）
- 在同样本、同配置、同网络条件下再跑一轮。
- 保存日志到另一份文件。

3. 生成单批次指标
```powershell
python scripts/s5_metrics_from_logs.py <log1> <log2>
```

4. 生成前后对比报告
```powershell
python scripts/s5_compare_metrics.py --baseline <baseline_log...> --candidate <candidate_log...> --out plans/phase_reports/S5_compare_report.md
```

## 5. 通过门禁（S5）
- `download_success_rate` 不低于基线。
- `probe_pass_rate` 不低于基线。
- 不出现新的“重复抓取风暴”或明显 UI 卡顿。
- 关键失败日志均可定位到 `event/stage/error_type`。

## 6. 失败处理
- 如门禁不通过：
  - 先按失败阶段归类（playlist/key/segment/auth/parse）。
  - 回滚高风险改动点，保留日志增强。
  - 重新跑同批次样本验证。
