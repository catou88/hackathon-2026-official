"""One model request per process. Invoked only by transport.request_completion."""
import json
import os
import sys
import threading
import time


def _watch_parent(parent_pid):
    """Close the request even if an outer supervisor terminates our caller."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only.
        if not handle:
            os._exit(125)
        try:
            result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            # A signaled parent or failed liveness check must close our socket.
            if result in (0, 0xFFFFFFFF):
                os._exit(125)
        finally:
            kernel.CloseHandle(handle)
    else:
        # Linux reparents an orphan immediately. Polling also works when prctl
        # is unavailable, and has no preexec_fn/thread interaction hazards.
        while os.getppid() == parent_pid:
            time.sleep(0.05)
        os._exit(125)


def main():
    envelope = json.load(sys.stdin)
    parent_pid = envelope['parent_pid']
    watcher = threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True)
    watcher.start()
    try:
        from openai import OpenAI
        key = os.environ.get('FEATHERLESS_API_KEY')
        if not key:
            raise RuntimeError('Missing credentials')
        with OpenAI(api_key=key, base_url=os.environ.get('FEATHERLESS_BASE_URL', 'https://api.featherless.ai/v1'), max_retries=0) as client:
            response = client.chat.completions.create(**envelope['request'])
        result = {'response': response.model_dump(mode='json')}
    except Exception as exc:
        # Types/status suffice for retries and accounting. Raw exception strings
        # can include URLs, response bodies, or headers and stay in this process.
        status = getattr(exc, 'status_code', None)
        service_fault = isinstance(exc, TimeoutError) or type(exc).__name__ in {'APIConnectionError', 'APITimeoutError'} or (
            isinstance(status, int) and (status in (408, 429) or status >= 500))
        result = {'error': {'type': type(exc).__name__, 'status_code': status, 'service_fault': service_fault}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
