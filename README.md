# gpu-idle-guard

用于多人共享 NVIDIA GPU 服务器的常驻监控工具。持续记录各进程最近一次 GPU 活动，
每隔 `check_interval_seconds`（默认 60 秒）检查一次，将超过 `idle_timeout_seconds`
未见活动的进程列为回收候选，并刷新进程表。

**默认 `dry_run=true`：只报告应回收的进程，不会结束任何进程。**
关闭 dry-run 后，对候选先发 SIGTERM，`kill_grace_seconds` 后仍未退出则发 SIGKILL
（发信号前会校验 PID 与启动时间，避免误杀复用 PID）。

## 安装

需要 Python 3.10+、Linux 和正常运行的 NVIDIA 驱动。显卡与驱动需支持 NVML
进程级利用率查询；跨用户读取部分进程信息可能需要 root 权限。

在仓库根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## 使用

```bash
python gpu_idle_guard.py --config config.json   # 常驻运行，Ctrl+C 退出
python gpu_idle_guard.py --no-dry-run --config config.json # 真正终止（需有权限，谨慎）
```

配置为 JSON 文件（见 `config.json`），命令行参数优先于配置文件。
每次检查会在终端打印候选行，并更新：

- `reports/gpu-observation.md`：全部可见 GPU 进程及回收判定。
- `reports/gpu-observation.json`：完整数据、本次 `actions`、采样错误和监控资源开销。

| 配置项 / 参数 | 默认值 | 说明 |
|---|---|---|
| `idle_timeout_seconds` | `900` | 未见活动多久后成为回收候选，单位秒 |
| `check_interval_seconds` | `60` | 判定、动作与报告更新的周期，单位秒 |
| `dry_run` | `true` | `true` 只报告；`--no-dry-run` 才会终止进程 |
| `kill_grace_seconds` | `10` | SIGTERM 后多久升级为 SIGKILL |
| `duration_seconds` | `0` | 运行时长，`0` 表示一直运行 |
| `interval_seconds` | `1` | 活动样本的目标读取周期，单位秒 |
| `process_list_interval_seconds` | `5` | 进程列表和显存的查询周期，单位秒 |
| `activity_threshold_percent` | `1` | 活动判定阈值，单位 % |
| `output` | `reports/gpu-observation.json` | JSON 输出路径 |
| `markdown` | `reports/gpu-observation.md` | Markdown 输出路径 |

活动采样仍按 `interval_seconds` 持续进行（空闲计时依赖连续采样），
每分钟只是判定与动作的频率。

查询较慢时，实际周期会长于目标周期。监控开销取决于 GPU 数量、驱动和采样频率，
可从 JSON 报告查看 CPU、内存和查询耗时。

## 判定规则与结果说明

- 以 PID 和启动时间识别进程，合并同一进程在多张卡上的活动。
- 任意一张卡的计算、显存访问、编码或解码利用率达到阈值，就重新开始空闲计时。
  **占用显存不等于正在使用 GPU。**
- 从监控启动或首次发现进程时开始观察，无法补查完整的历史空闲时长。
  程序重启、监控中断或相关查询异常后，会重新计时。
- 表格中的“≥15分”表示观察期间至少 15 分钟未见活动，之前的历史未知。
  `would_reclaim=true` 表示达到回收阈值；dry-run 下不执行清理。
- 驱动可能不返回空闲进程的样本，极短活动也可能漏报。`samples=0` 或峰值“—”
  表示没有返回样本，**不能理解为精确测得利用率始终为 0**。

报告包含用户名、PID、GPU、显存、启动时间、未见活动时长和候选判定。
JSON 还提供父 PID、可读取的程序路径及各引擎活动统计。

当前按进程判断，未实现任务归组和豁免名单，开启终止前请先用 dry-run 观察候选是否合理。正常的断点调试、数据加载或等待
请求也可能长期不使用 GPU，候选名单应结合服务器使用约定解释。
MIG、MPS 和容器环境需另行验证指标归属及进程可见性。

## 隐私

报告含用户名、GPU UUID 和本地程序路径等信息，不宜直接公开。
`reports/`、环境文件、凭据和缓存已列入 `.gitignore`；自定义输出路径需自行排除。
提交 issue 或 PR 时请使用虚构或脱敏数据。

## 测试与贡献

```bash
python -m unittest -v test_gpu_idle_guard.py
```

测试使用模拟数据，不需要 GPU 或 root 权限。

## 许可证

[MIT License](LICENSE)
