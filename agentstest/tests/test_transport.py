"""Real subprocess/socket tests; all traffic stays on a local synthetic server."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mini_rca.config import Config
from mini_rca.llm import GLMClient
from mini_rca.transport import HardTimeoutError, TransportError, request_completion

MODEL = 'zai-org/GLM-4.7-Flash'


class SyntheticHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get('Content-Length', 0))
        self.rfile.read(size)
        self.server.requests += 1
        self.server.received.set()
        if self.server.mode == 'hang':
            # No data at all. The timeout must kill the process, not just stop
            # waiting for a background thread that still owns the HTTP request.
            self.server.release.wait(15)
        if self.server.mode == 'reject':
            body = {'error': {'message': 'synthetic-secret must never escape', 'type': 'capacity'}}
            self.send_response(503)
        else:
            body = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': MODEL,
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant', 'content': '', 'reasoning': '{"action":"answer"}'}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
            self.send_response(200)
        payload = json.dumps(body).encode('utf-8')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        try:
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def process_running(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        proc_status = Path(f'/proc/{pid}/stat')
        if proc_status.exists() and proc_status.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticHandler)
        self.server.mode = 'ok'
        self.server.requests = 0
        self.server.received = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(os.environ, {'FEATHERLESS_API_KEY': 'synthetic-secret',
            'FEATHERLESS_BASE_URL': f'http://127.0.0.1:{self.server.server_port}/v1'})
        self.env.start()

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()

    def request(self, deadline, **options):
        kwargs = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'synthetic'}],
                  'max_tokens': 50, 'timeout': 30}
        return request_completion(kwargs, deadline, **options)

    def test_real_worker_preserves_full_response_side_channel_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = GLMClient(Config(mode='single'), Path(tmp))
            llm.begin_case(42)
            result = llm.ask([{'role': 'user', 'content': 'synthetic'}], MODEL, time.monotonic()+10)
            self.assertEqual(result, {'action': 'answer'})
            event = llm.events[-1]
            self.assertEqual(event['response_channel'], 'reasoning')
            self.assertEqual(event['transport'], 'subprocess')
            self.assertEqual(event['completion_tokens'], 20)
            self.assertFalse(process_running(event['worker_pid']))
            audit = (Path(tmp)/'42/01.json').read_text(encoding='utf-8')
            self.assertNotIn('synthetic-secret', audit)
            self.assertEqual(json.loads(audit)['raw_provider_response']['choices'][0]['message']['reasoning'], '{"action":"answer"}')

    def test_hanging_request_is_killed_and_reaped_at_absolute_deadline(self):
        self.server.mode = 'hang'
        pids = []
        started = time.monotonic()
        with self.assertRaises(HardTimeoutError):
            self.request(started+2.5, on_start=pids.append)
        self.assertTrue(self.server.received.is_set(), 'Synthetic HTTP request was never reached')
        self.assertLess(time.monotonic()-started, 3.5)
        self.assertEqual(len(pids), 1)
        self.assertFalse(process_running(pids[0]))

    def test_hard_deadline_retains_cost_and_cannot_retry_past_case(self):
        self.server.mode = 'hang'
        llm = GLMClient(Config(mode='routed', service_attempts=3))
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            llm.ask([{'role': 'user', 'content': 'synthetic'}], MODEL, started+3)
        self.assertLess(time.monotonic()-started, 3.5)
        requests = [event for event in llm.events if 'attempt' in event]
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.server.requests, 1)
        event = requests[0]
        self.assertTrue(event['hard_timeout'])
        self.assertTrue(event['usage_estimated'])
        self.assertEqual(event['cost_dollars'], event['reserved_dollars'])
        self.assertEqual(llm.total_dollars, event['reserved_dollars'])
        self.assertFalse(process_running(event['worker_pid']))

    def test_explicit_http_rejection_has_no_generation_cost_or_secret(self):
        self.server.mode = 'reject'
        llm = GLMClient(Config(mode='single', service_attempts=1))
        with self.assertRaises(RuntimeError):
            llm.ask([{'role': 'user', 'content': 'synthetic'}], MODEL, time.monotonic()+10)
        event = llm.events[-1]
        self.assertEqual(event['failure_kind'], 'service')
        self.assertEqual(event['provider_error_type'], 'InternalServerError')
        self.assertEqual(llm.case_cost, 0)
        self.assertNotIn('synthetic-secret', json.dumps(llm.events))

    def test_expired_deadline_does_not_spawn_or_send(self):
        with patch('mini_rca.transport.subprocess.Popen') as popen:
            with self.assertRaises(HardTimeoutError):
                self.request(time.monotonic()-1)
        popen.assert_not_called()
        self.assertEqual(self.server.requests, 0)

    def test_worker_exits_when_outer_supervisor_kills_its_parent(self):
        self.server.mode = 'hang'
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp)/'worker.pid'
            code = """import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from mini_rca.transport import request_completion
request_completion({'model':'zai-org/GLM-4.7-Flash','messages':[{'role':'user','content':'synthetic'}],'max_tokens':50,'timeout':30},time.monotonic()+60,on_start=lambda pid:Path(sys.argv[2]).write_text(str(pid)))
"""
            parent = subprocess.Popen([sys.executable, '-B', '-c', code,
                str(Path(__file__).resolve().parents[1]), str(pid_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            worker_pid = None
            try:
                self.assertTrue(self.server.received.wait(8), 'Synthetic request did not start')
                worker_pid = int(pid_path.read_text())
                self.assertTrue(process_running(worker_pid))
                parent.kill()
                parent.wait(timeout=2)
                deadline = time.monotonic()+2
                while process_running(worker_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_running(worker_pid), 'HTTP worker survived its parent')
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=2)
                if worker_pid and process_running(worker_pid):
                    import signal
                    os.kill(worker_pid, signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
