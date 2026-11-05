import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gpu_idle_guard as guard


def sample(timestamp, sm=0, mem=0, enc=0, dec=0):
    return SimpleNamespace(timeStamp=int(timestamp*1_000_000), smUtil=sm,
                           memUtil=mem, encUtil=enc, decUtil=dec)


class SimulatedClock:
    """Keep collector tests independent of machine load and scheduling delays."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000 + self.now

    def process_time(self):
        return 0.0

    def sleep(self, seconds):
        self.now += seconds


class ActivityTests(unittest.TestCase):
    def test_no_history_and_timeout_boundary(self):
        activity = guard.Activity(100)
        self.assertFalse(activity.summary(999.99, 900)['would_reclaim'])
        result = activity.summary(1000, 900)
        self.assertTrue(result['would_reclaim'])
        self.assertTrue(result['no_activity_seen_in_observation'])
        self.assertIsNone(result['seconds_since_last_observed_activity'])
        self.assertEqual(result['sample_evidence'], 'no_samples')

    def test_delayed_sample_uses_sample_time_not_query_time(self):
        activity = guard.Activity(100)
        activity.observe(sample(1015, sm=1), now=120, wall_now=1020, threshold=1)
        self.assertEqual(activity.summary(130, 900)['seconds_since_last_observed_activity'], 15)

    def test_out_of_order_multi_gpu_samples_do_not_rewind_activity(self):
        activity = guard.Activity(100)
        activity.observe(sample(1019, sm=20), 120, 1020, 1)
        activity.observe(sample(1010, sm=30), 120, 1020, 1)
        self.assertEqual(activity.summary(130, 900)['seconds_without_observed_activity'], 11)

    def test_pre_monitoring_samples_do_not_create_fake_history(self):
        activity = guard.Activity(100)
        activity.observe(sample(995, sm=50), 110, 1010, 1)
        result = activity.summary(120, 900)
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['seconds_without_observed_activity'], 20)

    def test_zero_samples_do_not_reset_but_codec_and_memory_do(self):
        for engine in ['sm', 'mem', 'enc', 'dec']:
            with self.subTest(engine=engine):
                activity = guard.Activity(100)
                activity.observe(sample(1009), 110, 1010, 1)
                self.assertEqual(activity.quiet_since, 100)
                activity.observe(sample(1010, **{engine: 1}), 110, 1010, 1)
                self.assertEqual(activity.summary(120, 900)['seconds_without_observed_activity'], 10)

    def test_monitor_error_restarts_full_observation_window(self):
        activity = guard.Activity(100)
        activity.reset(900)
        self.assertFalse(activity.summary(1000, 900)['would_reclaim'])
        self.assertTrue(activity.summary(1800, 900)['would_reclaim'])

    def test_future_sample_rejected(self):
        activity = guard.Activity(100)
        self.assertFalse(activity.observe(sample(1100, sm=100), 110, 1010, 1))
        self.assertEqual(activity.resets, 1)
        self.assertIsNone(activity.last_active)

    def test_invalid_utilization_is_not_counted_as_activity(self):
        activity = guard.Activity(100)
        self.assertFalse(activity.observe(sample(1010, sm=2**32-1), 110, 1010, 1))
        self.assertIsNone(activity.last_active)
        self.assertEqual(activity.resets, 1)


class CollectorTests(unittest.TestCase):
    def collect(self, utilization_error=None, list_interval=.005):
        # Both /proc and NVIDIA queries are simulated (sandbox PID namespaces differ).
        process = SimpleNamespace(pid=os.getpid(), usedGpuMemory=2*1024**3)
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(idle_timeout_seconds=.001, duration_seconds=.02,
                                   interval_seconds=.005, snapshot_interval_seconds=.005,
                                   process_list_interval_seconds=list_interval,
                                   activity_threshold_percent=1,
                                   output=str(Path(directory)/'report.json'),
                                   markdown=str(Path(directory)/'report.md'))
            with contextlib.ExitStack() as stack:
                queries = {}
                stack.enter_context(patch.object(guard, 'time', SimulatedClock()))
                stack.enter_context(patch.object(guard, 'process_info', return_value=(
                    (process.pid, 1), dict(pid=process.pid, started_at='1970-01-01T00:00:01+00:00',
                                           started_timestamp=1, username='test', name='fake',
                                           status='sleeping', ppid=1, executable='/test/python'))))
                stack.enter_context(patch.object(guard.psutil, 'Process', return_value=SimpleNamespace(
                    memory_info=lambda: SimpleNamespace(rss=1024**2))))
                for name, value in [('nvmlInit', None), ('nvmlShutdown', None),
                                    ('nvmlDeviceGetCount', 2), ('nvmlDeviceGetHandleByIndex', 0),
                                    ('nvmlDeviceGetUUID', 'test'), ('nvmlDeviceGetName', 'fake'),
                                    ('nvmlDeviceGetComputeRunningProcesses', [process, process]),
                                    ('nvmlDeviceGetGraphicsRunningProcesses', [process])]:
                    queries[name] = stack.enter_context(patch.object(guard.nv, name, return_value=value))
                stack.enter_context(patch.object(guard.nv, 'nvmlDeviceGetProcessUtilization',
                                                side_effect=utilization_error or guard.nv.NVMLError_NotFound()))
                with contextlib.redirect_stdout(io.StringIO()):
                    guard.run(args)
                self.process_list_calls = queries['nvmlDeviceGetComputeRunningProcesses'].call_count
            report = json.loads(Path(args.output).read_text())
            self.assertIn('未终止任何进程', Path(args.markdown).read_text())
            return report

    def test_duplicate_contexts_and_multi_gpu_are_one_process(self):
        report = self.collect()
        self.assertEqual(len(report['processes']), 1)
        self.assertEqual(len(report['processes'][0]['gpus']), 2)
        self.assertEqual(sum(g['memory_mib'] for g in report['processes'][0]['gpus']), 4096)
        self.assertTrue(report['processes'][0]['would_reclaim'])
        self.assertEqual(report['error_count'], 0)

    def test_unsupported_utilization_cannot_become_idle_candidate(self):
        report = self.collect(guard.nv.NVMLError_NotSupported())
        self.assertGreater(report['error_count'], 0)
        self.assertGreater(report['processes'][0]['health_resets'], 0)
        self.assertFalse(report['processes'][0]['would_reclaim'])

    def test_slow_process_discovery_does_not_slow_activity_polling(self):
        report = self.collect(list_interval=1)
        self.assertEqual(self.process_list_calls, 2)  # One list per GPU, not per cycle.
        self.assertGreater(report['cycles'], 2)

    def test_report_contains_identity_and_memory(self):
        row = self.collect()['processes'][0]
        self.assertEqual(row['username'], 'test')
        self.assertEqual(row['started_timestamp'], 1)
        self.assertEqual(row['executable'], '/test/python')
        self.assertTrue(all(g['memory_observed_at'] for g in row['gpus']))


if __name__ == '__main__':
    unittest.main()
