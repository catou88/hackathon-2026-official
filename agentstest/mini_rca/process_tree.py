"""Own and synchronously stop the process tree created by a case supervisor."""
import os
import signal
import subprocess
import time
from pathlib import Path


class TreeCleanupError(RuntimeError):
    """Cleanup was not confirmed; the supervisor must not reuse worker output."""


def _windows_api():
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class BasicAccounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ('TotalUserTime', 'TotalKernelTime',
                    'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                    ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                    ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    return ctypes, kernel, ExtendedLimits, BasicAccounting


def _job_members(ctypes, kernel, job):
    """Retain synchronization handles for job members until each has exited."""
    # JobObjectBasicProcessIdList: two DWORDs, followed by ULONG_PTR process IDs.
    # This worker has few descendants; a full buffer is uncertainty, not success.
    buffer = ctypes.create_string_buffer(65536)
    if not kernel.QueryInformationJobObject(job, 3, buffer, len(buffer), None):
        raise TreeCleanupError('Could not enumerate Windows job members')
    count = ctypes.c_uint32.from_buffer(buffer, 4).value
    assigned = ctypes.c_uint32.from_buffer(buffer, 0).value
    if count != assigned or 8+count*ctypes.sizeof(ctypes.c_size_t) > len(buffer):
        raise TreeCleanupError('Windows job membership changed during cleanup')
    handles = []
    for offset in range(count):
        pid = ctypes.c_size_t.from_buffer(buffer, 8+offset*ctypes.sizeof(ctypes.c_size_t)).value
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE.
        if handle:
            handles.append(handle)
        elif ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: PID already exited.
            for previous in handles:
                kernel.CloseHandle(previous)
            raise TreeCleanupError('Could not retain a Windows job member for exit confirmation')
    return handles


def attach_tree(process):
    """Attach ownership immediately after Popen, before the worker can spawn.

    On Windows create with CREATE_SUSPENDED | CREATE_NO_WINDOW and set
    process._mini_rca_suspended = True before this call. The process resumes only
    after assignment to a job that disallows breakaway and kills all members when
    the owning handle closes. This closes the spawn-before-assignment race.
    Linux callers must create their worker with start_new_session=True.
    """
    if getattr(process, '_mini_rca_tree', None) is not None:
        raise TreeCleanupError('Worker tree is already attached')
    if os.name != 'nt':
        if os.getpgid(process.pid) != process.pid:
            raise TreeCleanupError('Worker must lead a separate process group')
        process._mini_rca_tree = {'kind': 'process_group', 'pgid': process.pid}
        return
    ctypes, kernel, Limits, _ = _windows_api()
    job = kernel.CreateJobObjectW(None, None)
    try:
        if not job:
            raise TreeCleanupError('Could not create Windows process-tree job')
        limits = Limits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise TreeCleanupError('Could not configure Windows process-tree job')
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise TreeCleanupError('Could not assign worker to Windows process-tree job')
        process._mini_rca_tree = {'kind': 'windows_job', 'handle': job}
        if getattr(process, '_mini_rca_suspended', False):
            ntdll = ctypes.WinDLL('ntdll')
            ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            if ntdll.NtResumeProcess(int(process._handle)) != 0:
                raise TreeCleanupError('Could not resume supervised worker')
            process._mini_rca_suspended = False
    except Exception:
        if job:
            kernel.TerminateJobObject(job, 125)
            kernel.CloseHandle(job)
        process._mini_rca_tree = None
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # A failed assignment is never silently replaced by parent-only cleanup.
        raise


def stop_tree(process):
    """Stop every owned member, confirm termination, and reap the Popen child.

    The bounded cleanup raises on uncertainty. Callers must abort instead of
    starting another worker or treating partial cleanup as successful.
    """
    if getattr(process, '_mini_rca_tree_closed', False):
        return
    tree = getattr(process, '_mini_rca_tree', None)
    if tree is None:
        raise TreeCleanupError('Worker tree was not attached; cleanup cannot be confirmed')
    deadline = time.monotonic()+0.75
    if tree['kind'] == 'windows_job':
        ctypes, kernel, _, Accounting = _windows_api()
        job = tree['handle']
        confirmed = False
        handles = []
        try:
            handles = _job_members(ctypes, kernel, job)
            if not kernel.TerminateJobObject(job, 125):
                raise TreeCleanupError('Windows rejected process-tree termination')
            while time.monotonic() < deadline:
                accounting = Accounting()
                if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    raise TreeCleanupError('Could not confirm Windows process-tree termination')
                if accounting.ActiveProcesses == 0:
                    waits = [kernel.WaitForSingleObject(handle, 0) for handle in handles]
                    if any(value == 0xFFFFFFFF for value in waits):
                        raise TreeCleanupError('Could not confirm a Windows job member exited')
                    if all(value == 0 for value in waits):
                        confirmed = True
                        break
                time.sleep(0.005)
            if not confirmed:
                raise TreeCleanupError('Windows process-tree termination was not confirmed before cleanup deadline')
        finally:
            # Also kills members if the supervisor exits during cleanup. Handles
            # are not inherited by children, so no descendant can keep it alive.
            kernel.CloseHandle(job)
            for handle in handles:
                kernel.CloseHandle(handle)
            process._mini_rca_tree = None
    else:
        try:
            os.killpg(tree['pgid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
        # SIGKILL closes descendants' descriptors even when container PID 1
        # delays reaping their zombie entries. Reap the direct child ourselves.
    try:
        process.wait(timeout=max(0.001, deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        raise TreeCleanupError('Worker did not exit before cleanup deadline') from None
    if tree['kind'] == 'process_group':
        while _group_has_live_members(tree['pgid']):
            if time.monotonic() >= deadline:
                raise TreeCleanupError('Process-group termination was not confirmed before cleanup deadline')
            time.sleep(0.005)
    process._mini_rca_tree = None
    process._mini_rca_tree_closed = True


def _group_has_live_members(pgid):
    proc = Path('/proc')
    if proc.is_dir():
        # Linux /proc lets us ignore exited zombies that a container's PID 1 has
        # not reaped. We only inspect and signal the group created by our Popen.
        for directory in proc.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                fields = (directory/'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in {'Z', 'X'}:
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def close_tree(process):
    """Release tree ownership; also terminate descendants left after normal exit."""
    stop_tree(process)
