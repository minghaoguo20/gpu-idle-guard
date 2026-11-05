#!/usr/bin/env python3
"""GPU idle guard: long-running monitor that flags (dry-run) or terminates idle GPU processes."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

import psutil
import pynvml as nv


DEFAULTS = dict(
    idle_timeout_seconds=900,
    duration_seconds=0,                # 0 = run forever
    interval_seconds=1,                # activity sampling period
    process_list_interval_seconds=5,
    check_interval_seconds=60,         # how often idle processes are evaluated/reported
    activity_threshold_percent=1,
    dry_run=True,                      # True: only report, never terminate
    kill_grace_seconds=10,             # SIGTERM -> SIGKILL delay
    output='reports/gpu-observation.json',
    markdown='reports/gpu-observation.md',
)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise ValueError('unknown config keys: ' + ', '.join(sorted(unknown)))
    return config


def iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(timespec='seconds')


class Activity:
    def __init__(self, now):
        self.first_seen = now
        self.quiet_since = now
        self.last_active = None
        self.samples = 0
        self.active_samples = 0
        self.peak = {engine: 0 for engine in ('sm', 'mem', 'enc', 'dec')}
        self.resets = 0

    def reset(self, now):
        self.quiet_since = now
        self.resets += 1

    def observe(self, sample, now, wall_now, threshold):
        stamp = sample.timeStamp / 1_000_000
        if stamp > wall_now + 2:
            self.reset(now)
            return False
        observed_at = min(now, now - (wall_now - stamp))
        if observed_at < self.first_seen:
            return True
        self.samples += 1
        values = {engine: int(getattr(sample, engine + 'Util', 0)) for engine in self.peak}
        if any(value < 0 or value > 100 for value in values.values()):
            self.reset(now)
            return False
        for engine, value in values.items():
            self.peak[engine] = max(self.peak[engine], value)
        # Memory/codec work also counts as activity, to avoid false idle reports.
        if max(values.values()) >= threshold:
            self.active_samples += 1
            if self.last_active is None or observed_at > self.last_active:
                self.last_active = observed_at
                self.quiet_since = max(self.quiet_since, observed_at)
        return True

    def summary(self, now, timeout):
        quiet = max(0, now - self.quiet_since)
        return dict(
            observed_seconds=round(now-self.first_seen, 1),
            seconds_without_observed_activity=round(quiet, 1),
            no_activity_seen_in_observation=self.last_active is None,
            seconds_since_last_observed_activity=(None if self.last_active is None
                                                 else round(now-self.last_active, 1)),
            samples=self.samples, active_samples=self.active_samples,
            peak_utilization_percent=dict(self.peak), health_resets=self.resets,
            would_reclaim=quiet >= timeout,
            sample_evidence='no_samples' if not self.samples else 'samples_received',
        )


def process_info(pid):
    process = psutil.Process(pid)
    with process.oneshot():
        created = process.create_time()
        info = dict(pid=pid, started_at=iso(created), started_timestamp=created,
                    username=process.username(), name=process.name(),
                    status=process.status(), ppid=process.ppid())
        try:
            info['executable'] = process.exe()
        except psutil.AccessDenied:
            info['executable'] = None
    return (pid, created), info


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)


def duration(seconds):
    return f'{int(seconds)//60}分{int(seconds)%60:02d}秒'


def markdown(report):
    mode = 'dry-run，未终止任何进程' if report['dry_run'] else '已启用终止（SIGTERM，超时后 SIGKILL）'
    lines = ['# GPU 进程观察结果', '',
             f"观察：{report['started_at']} 至 {report['finished_at']}（UTC）；"
             f"阈值 {report['config']['idle_timeout_seconds']:g} 秒；{mode}。", '',
             '无样本表示未观察到活动，并非证明 GPU 完全没有执行。标记“≥”表示从监控开始就未见活动，历史未知；峰值“—”表示未返回样本。', '',
             '| GPU | PID | 用户 | 程序 | 显存 GiB | 启动时间 UTC | 距最近活动/未见活动时长 | 样本数 | 峰值 SM% | 判定 |',
             '|---|---:|---|---|---:|---|---|---:|---:|---|']
    for row in sorted(report['processes'], key=lambda row: (
            max(row['gpus'], key=lambda gpu: gpu['memory_mib'] or 0)['index'], row['pid'])):
        gpu = ','.join(str(g['index']) for g in row['gpus'])
        memory = [g['memory_mib'] for g in row['gpus']]
        mem = f'{sum(memory)/1024:.2f}' if all(v is not None for v in memory) else '未知'
        quiet = duration(row['seconds_without_observed_activity'])
        if row['no_activity_seen_in_observation']:
            quiet = '≥' + quiet
        verdict = ('应回收（dry-run）' if report['dry_run'] else '应回收') if row['would_reclaim'] else '未到阈值'
        if row['health_resets']:
            verdict += '；计时曾重置'
        values = [gpu, row['pid'], row['username'], row['name'], mem,
                  row['started_at'], quiet, row['samples'],
                  row['peak_utilization_percent']['sm'] if row['samples'] else '—', verdict]
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
    lines += ['', '完整路径、各 GPU 显存、其他引擎活动、采样错误和资源开销见配套 JSON。', '']
    return '\n'.join(lines)


def act(rows, args, terminated, now):
    """Report idle candidates; terminate them unless dry_run. Never blocks the sampling loop."""
    actions = []
    live = {(row['pid'], row['started_timestamp']) for row in rows}
    for key in [k for k in terminated if k not in live]:
        del terminated[key]
    for row in rows:
        if not row['would_reclaim']:
            continue
        key = (row['pid'], row['started_timestamp'])
        item = dict(pid=row['pid'], username=row['username'], name=row['name'],
                    idle_seconds=row['seconds_without_observed_activity'])
        if args.dry_run:
            item['action'] = 'would_terminate'
        else:
            try:
                process = psutil.Process(row['pid'])
                # Guard against PID reuse: only signal the exact process we observed.
                if abs(process.create_time() - row['started_timestamp']) > 1e-3:
                    raise psutil.NoSuchProcess(row['pid'])
                if key not in terminated:
                    process.terminate()
                    terminated[key] = now
                    item['action'] = 'sigterm'
                elif now - terminated[key] >= args.kill_grace_seconds:
                    process.kill()
                    item['action'] = 'sigkill'
                else:
                    item['action'] = 'waiting_after_sigterm'
            except psutil.NoSuchProcess:
                item['action'] = 'already_gone'
            except (psutil.Error, OSError) as exc:
                item.update(action='failed', error=str(exc))
        actions.append(item)
        print(f"[{iso(time.time())}] {'dry-run ' if args.dry_run else ''}{item['action']} "
              f"pid={item['pid']} user={item['username']} name={item['name']} "
              f"idle={duration(item['idle_seconds'])}", flush=True)
    return actions


def run(args):
    nv.nvmlInit()
    try:
        handles = [nv.nvmlDeviceGetHandleByIndex(i) for i in range(nv.nvmlDeviceGetCount())]
        devices = [dict(index=i, uuid=nv.nvmlDeviceGetUUID(h), name=nv.nvmlDeviceGetName(h))
                   for i, h in enumerate(handles)]
        started, wall_start = time.monotonic(), time.time()
        cpu_start = time.process_time()
        cursors = [int(wall_start * 1_000_000)] * len(handles)
        process_cache = [[] for _ in handles]
        list_updated = [float('-inf')] * len(handles)
        states, errors = {}, []
        cycles, max_cycle, gap_resets, error_count = 0, 0, 0, 0
        previous_cycle = started
        clock_offset = wall_start-started
        last_write = started - args.check_interval_seconds
        terminated = {}
        own_process = psutil.Process()

        def error(gpu, operation, exc):
            nonlocal error_count
            error_count += 1
            errors.append(dict(at=iso(time.time()), gpu=gpu, operation=operation, error=str(exc)))
            del errors[:-100]

        def publish():
            now = time.monotonic()
            rows = []
            for state in states.values():
                rows.append(dict(**state['info'], gpus=list(state['gpus'].values()),
                                 **state['activity'].summary(now, args.idle_timeout_seconds)))
            elapsed = now-started
            actions = act(rows, args, terminated, now)
            print(f"[{iso(time.time())}] check: {len(rows)} GPU processes, "
                  f"{len(actions)} idle candidates (timeout {args.idle_timeout_seconds:g}s)", flush=True)
            report = dict(
                dry_run=args.dry_run, actions=actions, started_at=iso(wall_start), finished_at=iso(time.time()),
                config=vars(args), devices=devices, elapsed_seconds=round(elapsed, 2),
                cycles=cycles, max_cycle_seconds=round(max_cycle, 3),
                monitor_cpu_seconds=round(time.process_time()-cpu_start, 3),
                monitor_average_cpu_percent=round(100*(time.process_time()-cpu_start)/max(elapsed, .001), 2),
                monitor_rss_mib=round(own_process.memory_info().rss/1024**2, 2),
                gap_resets=gap_resets, error_count=error_count, recent_errors=errors,
                note='Only observed activity is known. No samples is not proof of zero work. '
                     'With dry_run a candidate is only a policy simulation.',
                processes=sorted(rows, key=lambda r: (min(g['index'] for g in r['gpus']), r['pid'])),
            )
            if args.output:
                atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            if args.markdown:
                atomic_write(args.markdown, markdown(report))
            return report

        while True:
            cycle_start = time.monotonic()
            new_offset = time.time()-cycle_start
            if abs(new_offset-clock_offset) > 2:
                error(None, 'clock_change', 'wall clock changed; starting a fresh observation window')
                cursors = [int(time.time()*1_000_000)] * len(handles)
                for state in states.values():
                    state['activity'].reset(cycle_start)
            clock_offset = new_offset
            # A stalled collector must not count an unmonitored gap as idle.
            if cycle_start-previous_cycle > max(5, args.interval_seconds*3):
                gap_resets += 1
                for state in states.values():
                    state['activity'].reset(cycle_start)
            previous_cycle = cycle_start
            present, failed_gpus = set(), set()
            current_gpus = {}
            for index, handle in enumerate(handles):
                list_ok = True
                if time.monotonic()-list_updated[index] >= args.process_list_interval_seconds:
                    fresh_processes = []
                    for query in (nv.nvmlDeviceGetComputeRunningProcesses, nv.nvmlDeviceGetGraphicsRunningProcesses):
                        try:
                            fresh_processes.extend(query(handle))
                        except nv.NVMLError as exc:
                            error(index, query.__name__, exc)
                            failed_gpus.add(index)
                            list_ok = False
                    if list_ok:
                        process_cache[index] = fresh_processes
                        list_updated[index] = time.monotonic()
                processes = process_cache[index]
                by_pid = {}
                for process in processes:
                    memory = process.usedGpuMemory
                    memory = None if memory is None or not 0 <= memory < 2**64-1 else memory
                    if process.pid not in by_pid or (memory is not None and memory > (by_pid[process.pid] or 0)):
                        by_pid[process.pid] = memory
                pid_keys = {}
                for pid, memory in by_pid.items():
                    try:
                        key, info = process_info(pid)
                    except psutil.NoSuchProcess:
                        continue
                    except (psutil.Error, OSError) as exc:
                        error(index, f'process_metadata:{pid}', exc)
                        failed_gpus.add(index)
                        continue
                    if key not in states:
                        states[key] = dict(info=info, activity=Activity(time.monotonic()), gpus={})
                    states[key]['info'] = info
                    present.add(key)
                    pid_keys[pid] = key
                    current_gpus.setdefault(key, {})[index] = dict(
                        **devices[index], memory_mib=None if memory is None else round(memory/1024**2, 1),
                        memory_observed_at=iso(time.time()-(time.monotonic()-list_updated[index])))
                try:
                    samples = nv.nvmlDeviceGetProcessUtilization(handle, cursors[index])
                except nv.NVMLError_NotFound:
                    samples = []
                except nv.NVMLError as exc:
                    error(index, 'utilization', exc)
                    failed_gpus.add(index)
                    samples = []
                for sample in samples:
                    key = pid_keys.get(sample.pid)
                    if key is not None and sample.timeStamp > cursors[index] and sample.timeStamp >= key[1]*1e6:
                        if not states[key]['activity'].observe(sample, time.monotonic(), time.time(), args.activity_threshold_percent):
                            error(index, 'sample_invalid', 'future timestamp or invalid utilization')
                            failed_gpus.add(index)
                if samples:
                    valid_stamps = [s.timeStamp for s in samples if s.timeStamp <= (time.time()+2)*1e6]
                    if valid_stamps:
                        cursors[index] = max(cursors[index], max(valid_stamps))
                if not list_ok:
                    for key, state in states.items():
                        if index in state['gpus']:
                            present.add(key)
                            current_gpus.setdefault(key, {})[index] = state['gpus'][index]
            for key, state in list(states.items()):
                if key not in present:
                    del states[key]
                    continue
                state['gpus'] = current_gpus[key]
                if failed_gpus.intersection(state['gpus']):
                    state['activity'].reset(time.monotonic())
            cycles += 1
            cycle_elapsed = time.monotonic()-cycle_start
            max_cycle = max(max_cycle, cycle_elapsed)
            if cycle_elapsed > max(5, args.interval_seconds*3):
                for state in states.values():
                    state['activity'].reset(time.monotonic())
            done = args.duration_seconds > 0 and time.monotonic()-started >= args.duration_seconds
            if done or time.monotonic()-last_write >= args.check_interval_seconds:
                report = publish()
                last_write = time.monotonic()
            if done:
                print(json.dumps({k: v for k, v in report.items() if k not in ('processes', 'recent_errors')}, ensure_ascii=False))
                break
            wait = args.interval_seconds-(time.monotonic()-cycle_start)
            if args.duration_seconds > 0:
                wait = min(wait, args.duration_seconds-(time.monotonic()-started))
            time.sleep(max(0, wait))
    finally:
        nv.nvmlShutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='JSON config file; command-line options override it')
    for key, default in DEFAULTS.items():
        flag = '--' + key.replace('_', '-')
        if isinstance(default, bool):
            parser.add_argument(flag, action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
        else:
            parser.add_argument(flag, type=type(default), default=argparse.SUPPRESS)
    cli = vars(parser.parse_args())
    config_path = cli.pop('config')
    values = dict(DEFAULTS)
    try:
        if config_path is not None:
            values.update(load_config(config_path))
    except (OSError, ValueError) as exc:
        parser.error(f'bad config: {exc}')
    values.update(cli)
    args = argparse.Namespace(**values)
    if not isinstance(args.dry_run, bool):
        parser.error('dry_run must be true or false')
    if min(args.idle_timeout_seconds, args.interval_seconds, args.process_list_interval_seconds,
           args.check_interval_seconds) <= 0 or args.duration_seconds < 0 or args.kill_grace_seconds < 0:
        parser.error('timeouts and intervals must be positive (duration 0 = run forever)')
    if not 1 <= args.activity_threshold_percent <= 100:
        parser.error('activity threshold must be between 1 and 100')
    try:
        run(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == '__main__':
    main()
