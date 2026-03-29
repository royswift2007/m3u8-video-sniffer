# 真实样本清单（S5 回归）

> 用于“改造前/改造后”同批次对比。建议至少 20 条，推荐 30+。

## 填写规则
- `分类`：`media` / `master` / `aes-128` / `referer-required` / `cookie-required` / `dash` / `mp4-direct` / `historical-fail`
- `期望抓取`：`Y` 或 `N`
- `期望下载`：`Y` 或 `N`
- `状态`：`todo` / `done`
- URL、Referer、Cookie 只用于本地回归，不要提交敏感凭据到公共仓库。

## 样本表
| ID | 分类 | URL | Referer | Cookie(可选) | 期望抓取 | 期望下载 | 备注 | 状态 |
|---|---|---|---|---|---|---|---|---|
| 01 | media | https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8 | https://ddys.io/ |  | Y | Y | 用户提供样本1 | done |
| 02 | media | https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8 | https://ddys.io/ |  | Y | Y | 用户提供样本2 | done |
| 03 | master |  |  |  | Y | Y |  | todo |
| 04 | master |  |  |  | Y | Y |  | todo |
| 05 | aes-128 |  |  |  | Y | Y |  | todo |
| 06 | aes-128 |  |  |  | Y | Y |  | todo |
| 07 | referer-required |  |  |  | Y | Y | 需要 Referer | todo |
| 08 | referer-required |  |  |  | Y | Y | 需要 Referer | todo |
| 09 | cookie-required |  |  |  | Y | Y | 需要 Cookie | todo |
| 10 | cookie-required |  |  |  | Y | Y | 需要 Cookie | todo |
| 11 | dash |  |  |  | Y | Y |  | todo |
| 12 | dash |  |  |  | Y | Y |  | todo |
| 13 | mp4-direct |  |  |  | Y | Y |  | todo |
| 14 | mp4-direct |  |  |  | Y | Y |  | todo |
| 15 | historical-fail |  |  |  | Y | N | 历史失败样本 | todo |
| 16 | historical-fail |  |  |  | Y | N | 历史失败样本 | todo |
| 17 | historical-fail |  |  |  | Y | N | 历史失败样本 | todo |
| 18 | master |  |  |  | Y | Y | 多层嵌套优先 | todo |
| 19 | media |  |  |  | Y | Y |  | todo |
| 20 | cookie-required |  |  |  | Y | Y |  | todo |

## 执行记录
- 基线批次日志：
- 候选批次日志：
- 对比报告路径：`plans/phase_reports/S5_compare_report.md`

