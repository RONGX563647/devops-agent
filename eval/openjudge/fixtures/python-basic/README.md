# python-basic

小型演示仓库：`calc.py` 提供加法与乘法，测试在 `tests/test_calc.py`。

## 运行测试

```bash
pytest -q
```

当前 `test_add` 是失败的，用来演示「读日志 → 定位 → 修复」的评测流程。

## 配置

`config/app.yaml` 里是服务名、重试次数与超时。
