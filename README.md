# gpu-idle-guard

用于多人共享 NVIDIA GPU 服务器的进程活动监控工具。记录各进程最近一次 GPU 活动，
将超过指定时间未见活动的进程列为回收候选，并输出进程表。

**当前仅模拟回收，不会结束任何进程。**

## 安装

需要 Python 3.10+、Linux 和正常运行的 NVIDIA 驱动。显卡与驱动需支持 NVML
进程级利用率查询；跨用户读取部分进程信息可能需要 root 权限。

在仓库根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## 使用

观察约 15 分钟，将 900 秒未见 GPU 活动的进程标记为模拟回收候选：

```bash
gpu-idle-guard --duration-seconds 915 --idle-timeout-seconds 900
```

运行期间默认每 30 秒更新以下文件，观察结束后退出：

- `reports/gpu-observation.md`：全部可见 GPU 进程及模拟回收结果。
- `reports/gpu-observation.json`：完整数据、采样错误和监控资源开销。

安装后也可用 `python gpu_idle_guard.py` 运行，参数相同。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--idle-timeout-seconds` | `900` | 未见活动多久后成为回收候选，单位秒 |
| `--duration-seconds` | `910` | 本次观察时长，单位秒 |
| `--interval-seconds` | `1` | 活动样本的目标读取周期，单位秒 |
| `--process-list-interval-seconds` | `5` | 进程列表和显存的查询周期，单位秒 |
| `--snapshot-interval-seconds` | `30` | 报告更新间隔，单位秒 |
| `--activity-threshold-percent` | `1` | 活动判定阈值，单位 % |
| `--output` | `reports/gpu-observation.json` | JSON 输出路径 |
| `--markdown` | `reports/gpu-observation.md` | Markdown 输出路径 |

查询较慢时，实际周期会长于目标周期。监控开销取决于 GPU 数量、驱动和采样频率，
可从 JSON 报告查看 CPU、内存和查询耗时。

## 判定规则与结果说明

- 以 PID 和启动时间识别进程，合并同一进程在多张卡上的活动。
- 任意一张卡的计算、显存访问、编码或解码利用率达到阈值，就重新开始空闲计时。
  **占用显存不等于正在使用 GPU。**
- 从监控启动或首次发现进程时开始观察，无法补查完整的历史空闲时长。
  程序重启、监控中断或相关查询异常后，会重新计时。
- 表格中的“≥15分”表示观察期间至少 15 分钟未见活动，之前的历史未知。
  `would_reclaim=true` 表示达到模拟回收阈值，未执行清理。
- 驱动可能不返回空闲进程的样本，极短活动也可能漏报。`samples=0` 或峰值“—”
  表示没有返回样本，**不能理解为精确测得利用率始终为 0**。

报告包含用户名、PID、GPU、显存、启动时间、未见活动时长和候选判定。
JSON 还提供父 PID、可读取的程序路径及各引擎活动统计。

当前按进程判断，未实现任务归组、豁免和自动终止。正常的断点调试、数据加载或等待
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

测试使用模拟数据，不需要 GPU 或 root 权限。开发说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT License](LICENSE)
