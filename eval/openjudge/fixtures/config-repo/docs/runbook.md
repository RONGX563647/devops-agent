# Checkout 发布手册

## 发布

1. 修改 `deploy.yaml` 的镜像 tag
2. 执行 `scripts/deploy.sh`
3. 观察 `kubectl rollout status`

## 回滚

```bash
kubectl rollout undo deployment/checkout
```

回滚后确认副本数为 2，低于 2 需要立即升级。
