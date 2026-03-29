# S5 Metrics Compare Report

## Baseline Logs
- logs/m3u8sniffer_20260313.log

## Candidate Logs
- logs/m3u8sniffer_20260314.log

## Metrics
| Metric | Baseline | Candidate | Delta |
|---|---:|---:|---:|
| sniffer_hit | 0 | 7 | +7 |
| download_start | 4 | 15 | +11 |
| task_completed | 2 | 26 | +24 |
| task_failed | 0 | 26 | +26 |
| retry | 0 | 29 | +29 |
| hls_probe_ok | 2 | 81 | +79 |
| hls_probe_fail | 0 | 23 | +23 |
| nm_ok | 1 | 0 | -1 |
| download_success_rate | 0.5000 | 1.7333 | 246.67% |
| download_fail_rate | 0.0000 | 1.7333 | n/a |
| probe_pass_rate | 1.0000 | 0.7788 | -22.12% |

## Notes
- Ensure baseline/candidate logs come from the same sample batch and workflow.
- Exclude development dummy tasks, pause/cancel-only runs, and unrelated regression logs.