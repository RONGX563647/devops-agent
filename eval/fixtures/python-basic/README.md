# python-basic

一个用于评测的小型 Python 服务模板，故意保持极简：只有一个业务模块、
一个测试文件与一条 GitHub Actions 流水线。

## 目录

- `app.py`：业务函数 `add`、`divide`
- `tests/test_app.py`：pytest 用例
- `.github/workflows/ci.yml`：CI 流水线

## 安装

```bash
python -m pip install -r requirements.txt
```

## 运行测试

```bash
python -m pytest
```

## 约定

- 分支保护要求所有改动走 PR；
- CI 在 `push` 与 `pull_request` 时触发，执行 ruff 与 pytest。
