# 使用指南

## 配置项

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `outDir` | string | `dist` | 产物目录 |
| `minify` | bool | `false` | 是否压缩产物 |
| `port` | int | `8080` | 预览端口 |

## 典型流程

先 `init`，再 `build`，最后 `serve`。修改配置后重跑 `build` 即可，无需再次 `init`。
