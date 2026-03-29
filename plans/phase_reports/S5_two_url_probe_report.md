# S5 两样本探测报告

| URL | 预探测 | 阶段 | 变体数 | 备注 |
|---|---|---|---:|---|
| https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8 | FAIL | playlist | 0 | HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/dR66g3Ed/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x000001CA64FB57F0>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。')) |
| https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8 | FAIL | playlist | 0 | HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/neg6lyld/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x000001CA64FBED50>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。')) |

## 详细
### 样本 1
- url: https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8
- probe_ok: False
- probe_stage: playlist
- variant_count: 0
- playlist_url: https://vv.jisuzyv.com/play/hls/dR66g3Ed/index.m3u8
- key_url: 
- segment_url: 
- probe_error: HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/dR66g3Ed/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x000001CA64FB57F0>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。'))

### 样本 2
- url: https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8
- probe_ok: False
- probe_stage: playlist
- variant_count: 0
- playlist_url: https://vv.jisuzyv.com/play/hls/neg6lyld/index.m3u8
- key_url: 
- segment_url: 
- probe_error: HTTPSConnectionPool(host='vv.jisuzyv.com', port=443): Max retries exceeded with url: /play/hls/neg6lyld/index.m3u8 (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x000001CA64FBED50>: Failed to establish a new connection: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。'))
