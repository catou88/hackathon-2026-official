"""Real subprocess regressions for the public CLI's hard case/run boundary.

The worker is synthetic and never imports a model client or opens a network
connection. Sleeps model uninterruptible data/SDK work, not a mocked clock.
"""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mini_rca.supervision import supervise


WORKER = r'''
import csv, json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mini_rca.cli import atomic_text
from mini_rca.supervision import WorkerChannel
out = Path(sys.argv[2]); behavior = sys.argv[3]
channel = WorkerChannel.from_environment(out)
done = set()
if (out/'predictions.csv').exists():
    with (out/'predictions.csv').open(newline='', encoding='utf-8') as f:
        done = {int(row['row_id']) for row in csv.DictReader(f)}
def result(rid):
    return {'row_id': rid, 'prediction': 'synthetic-answer-'+str(rid),
            'mode': 'offline-fallback', 'wall_s': 0, 'cost_dollars': 0,
            'evidence': '# Answer\nSynthetic fixture\n\n# Confidence\nLow\n\n# Evidence\nCompleted synthetic tool\n\n# Ruled out\nNone\n',
            'models': {}, 'model_events': [], 'ledger': [{'complete': True}], 'warnings': []}
for rid in ([0, 1] if behavior in ('hang_then_next', 'fast_then_stuck_exit') else [0]):
    if rid in done: continue
    channel.start_case(rid, time.monotonic())
    if rid == 0 and behavior != 'fast_then_stuck_exit':
        if behavior != 'stale_generation':
            channel.checkpoint(result(rid))
        if behavior == 'hang_then_next':
            atomic_text(out/'model_responses/0/01.json', json.dumps({
                'row_id': 0, 'attempt': 1,
                'event': {'model': 'zai-org/GLM-4.7-Flash', 'attempt': 1,
                          'status': 'pending', 'reserved_dollars': 0.031}}))
            child = subprocess.Popen([sys.executable, '-B', '-c',
                'import time; time.sleep(90)'], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            atomic_text(out/'grandchild.pid', str(child.pid))
        while True: time.sleep(0.03)
    channel.result(result(rid))
if behavior == 'fast_then_stuck_exit':
    # Even after the final accepted result, a stuck close must be bounded.
    while True: time.sleep(0.03)
'''


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path('/proc')/str(pid)/'stat'
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] in {'Z', 'X'}:
            return False  # Container PID 1 may delay reaping an already dead child.
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False


class HardSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root/'data'; self.data.mkdir()
        self.out = self.root/'out'; self.out.mkdir()
        self.queries = self.root/'query.csv'
        self.worker = self.root/'synthetic_worker.py'
        self.worker.write_text(WORKER, encoding='utf-8')

    def tearDown(self):
        # A failing tree-cleanup regression must not leak the synthetic child.
        path = self.out/'grandchild.pid'
        if path.exists():
            pid = int(path.read_text())
            if process_alive(pid):
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=3)
                else:
                    import signal
                    os.kill(pid, signal.SIGKILL)
        self.temp.cleanup()

    def run_worker(self, behavior, count=1, case_seconds=4, run_seconds=12):
        with self.queries.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['row_id', 'task_index', 'instruction'])
            for rid in range(count):
                writer.writerow([rid, 'task_7', 'synthetic instruction'])
        args = ['--dataset', str(self.data), '--queries', str(self.queries),
                '--out', str(self.out), '--mode', 'offline',
                '--case-seconds', str(case_seconds), '--run-seconds', str(run_seconds)]
        began = time.monotonic()
        summary = supervise(args, command=[sys.executable, '-B', str(self.worker),
                                         str(ROOT), str(self.out), behavior])
        return summary, time.monotonic()-began

    def predictions(self):
        with (self.out/'predictions.csv').open(newline='', encoding='utf-8') as f:
            return {int(r['row_id']): r for r in csv.DictReader(f)}

    def test_hanging_tool_recovers_snapshot_cost_and_next_case(self):
        summary, wall = self.run_worker('hang_then_next', count=2)
        rows = self.predictions()
        self.assertEqual(set(rows), {0, 1})
        self.assertEqual(rows[0]['prediction'], 'synthetic-answer-0')
        self.assertEqual(rows[1]['prediction'], 'synthetic-answer-1')
        self.assertLess(float(rows[0]['wall_s']), 4.8)
        self.assertLess(wall, 9)
        self.assertEqual(summary['completed'], 2)
        self.assertAlmostEqual(summary['run_cost_dollars'], 0.031)
        trace = json.loads((self.out/'traces/0.json').read_text(encoding='utf-8'))
        self.assertTrue(trace['hard_timeout'])
        self.assertEqual(trace['termination_reason'], 'hard_case_deadline')
        self.assertEqual(trace['ledger'], [{'complete': True}])
        self.assertEqual(trace['model_events'][0]['failure_kind'], 'hard_case_timeout')
        self.assertTrue(trace['model_events'][0]['usage_estimated'])
        audit = json.loads((self.out/'model_responses/0/01.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(audit['event']['cost_dollars'], 0.031)
        usage = [json.loads(line) for line in (self.out/'usage.jsonl').read_text().splitlines()]
        self.assertEqual(sorted(u['row_id'] for u in usage), [0, 1])
        child_pid = int((self.out/'grandchild.pid').read_text())
        self.assertFalse(process_alive(child_pid), 'hard timeout left a live grandchild')

    def test_run_deadline_wins_over_longer_case_deadline(self):
        summary, wall = self.run_worker('run_hang', case_seconds=8, run_seconds=3)
        self.assertLess(wall, 3.8)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['deadline_events'][0]['reason'], 'hard_run_deadline')
        self.assertEqual(self.predictions()[0]['prediction'], 'synthetic-answer-0')

    def test_previous_generation_snapshot_is_never_adopted(self):
        stale = self.out/'supervision/checkpoints/0.json'
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({'generation': 'old-worker', 'at': time.monotonic(),
            'result': {'row_id': 0, 'prediction': 'must-not-be-adopted'}}), encoding='utf-8')
        summary, wall = self.run_worker('stale_generation')
        self.assertLess(wall, 6)
        self.assertEqual(summary['valid_predictions'], 0)
        self.assertEqual(self.predictions()[0]['prediction'], '')
        self.assertIn('No completed evidence snapshot',
                      (self.out/'evidence/0.md').read_text(encoding='utf-8'))

    def test_last_result_does_not_wait_a_full_case_for_worker_exit(self):
        summary, wall = self.run_worker('fast_then_stuck_exit', count=2, case_seconds=8)
        self.assertEqual(summary['completed'], 2)
        self.assertEqual(summary['valid_predictions'], 2,
                         (self.out/'supervision/worker.log').read_text(encoding='utf-8', errors='replace'))
        self.assertEqual(set(self.predictions()), {0, 1})
        self.assertLess(wall, 4, 'finished cases waited for the stalled worker close')
        self.assertFalse(summary['deadline_events'])


if __name__ == '__main__':
    unittest.main()
