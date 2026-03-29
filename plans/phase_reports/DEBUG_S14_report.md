# DEBUG S14 报告（全链路门禁）

- 命令总数：5
- 失败数量：0

## 命令结果
- `python -m compileall main.py protocol_handler.pyw core ui engines utils tests` -> code=0
- `python -m pytest tests -q -p no:cacheprovider` -> code=0
- `python scripts/s4_fault_injection.py` -> code=0
- `python scripts/s5_smoke.py` -> code=0
- `cmd /c scripts\run_smoke_tests.bat` -> code=0
