# DEBUG S8 报告（真实样本批次探测）

- 日期：2026-03-14 06:47:59
- 样本文件：`plans\test_samples.md`
- 参与探测：2
- probe_ok：0
- probe_fail：2

## 明细
- 01: ok=False stage=playlist url=https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8
  - error=HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/dR66g3Ed/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x0000012CFFDC17F0>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。'))
- 02: ok=False stage=playlist url=https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8
  - error=HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/neg6lyld/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x0000012CFFDC96D0>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。'))

- 结果文件：`plans\phase_reports\S8_probe_results.json`
