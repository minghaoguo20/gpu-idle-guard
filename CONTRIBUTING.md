# 参与开发

## 开发与测试

使用 Python 3.10 或更新版本：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest -v test_gpu_idle_guard.py
```

自动测试模拟 NVML 和进程元数据，不需要 GPU、NVIDIA 驱动或 root 权限。
提交 PR 时说明问题、修改后的行为以及验证结果。

## 实机验证

在有权限访问 NVIDIA 设备和目标进程信息的 Linux 主机运行短时观察：

```bash
gpu-idle-guard --duration-seconds 10
```

本项目当前只模拟回收。不要在测试中终止其他用户的进程。
涉及活动计时的修改应覆盖多卡活动、进程身份、驱动异常及监控中断情况。

## 数据与隐私

`reports/` 是本地输出目录，已被 Git 忽略。不要提交真实进程表、用户名、
GPU UUID、程序路径、凭据、日志或内部服务器地址。
报告问题时使用自行构造或脱敏后的数据，不要直接上传真实监控 JSON。
如果发现凭据泄露，应先撤销或轮换凭据；仅删除文件不能消除 Git 历史中的副本。

提交代码采用仓库中的 MIT 许可证。
