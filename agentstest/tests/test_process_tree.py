import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mini_rca.process_tree import TreeCleanupError, attach_tree, close_tree, stop_tree


def alive(pid):
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
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ProcessTreeTests(unittest.TestCase):
    def spawn_tree(self, root, exit_parent=False):
        child_code = """import json,os,subprocess,sys,time
from pathlib import Path
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'],creationflags=flags)
Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
"""
        parent_code = """import os,subprocess,sys,time
from pathlib import Path
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
child=subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],creationflags=flags)
if sys.argv[3]=='exit':
 while not Path(sys.argv[2]).exists():time.sleep(.01)
else:time.sleep(60)
"""
        opts = {'creationflags': subprocess.CREATE_NO_WINDOW | 0x4} if os.name == 'nt' else {'start_new_session': True}
        process = subprocess.Popen([sys.executable, '-B', '-c', parent_code, child_code,
                                   str(root/'pids.json'), 'exit' if exit_parent else 'stay'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **opts)
        if os.name == 'nt':
            process._mini_rca_suspended = True
        attach_tree(process)
        try:
            deadline = time.monotonic()+5
            while not (root/'pids.json').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            descendants = json.loads((root/'pids.json').read_text())
            self.assertTrue(all(alive(pid) for pid in descendants))
            return process, descendants
        except Exception:
            stop_tree(process)
            raise

    def test_kills_and_confirms_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, descendants = self.spawn_tree(Path(tmp))
            started = time.monotonic()
            stop_tree(process)
            self.assertLess(time.monotonic()-started, 1)
            self.assertFalse(alive(process.pid))
            self.assertTrue(all(not alive(pid) for pid in descendants))
            stop_tree(process)  # Confirmed cleanup is idempotent.

    def test_normal_parent_exit_does_not_leave_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, descendants = self.spawn_tree(Path(tmp), exit_parent=True)
            process.wait(timeout=3)
            self.assertTrue(all(alive(pid) for pid in descendants))
            close_tree(process)
            self.assertTrue(all(not alive(pid) for pid in descendants))

    def test_unattached_tree_is_not_claimed_as_clean(self):
        class Unattached:
            pass
        with self.assertRaises(TreeCleanupError):
            stop_tree(Unattached())


if __name__ == '__main__':
    unittest.main()
