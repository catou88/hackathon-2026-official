"""Isolate blocking SDK calls so the caller can enforce a wall-clock deadline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace


class TransportError(RuntimeError):
    """A sanitized error returned by the request worker; never contains headers."""
    def __init__(self, error_type, status_code=None, service_fault=False):
        super().__init__(f'Model request failed: {error_type}')
        self.error_type = error_type
        self.status_code = status_code
        self.service_fault = service_fault


class HardTimeoutError(TimeoutError):
    pass


def _attributes(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _attributes(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_attributes(item) for item in value]
    return value


class ProviderResponse:
    """Expose the SDK attributes used by GLMClient while retaining exact JSON."""
    def __init__(self, data):
        self._data = data
        for key, value in data.items():
            # Provider error must remain JSON-serializable in the response audit.
            setattr(self, key, value if key == 'error' else _attributes(value))

    def model_dump(self, mode='json'):
        return self._data


def request_completion(kwargs, deadline, on_start=None):
    """Return before the absolute deadline plus bounded process cleanup overhead.

    The child inherits credentials through its environment. Neither argv nor the
    pipe request contains credentials. Killing it closes the socket, but cannot
    guarantee cancellation of generation already accepted by the remote service.
    """
    if deadline <= time.monotonic():
        raise HardTimeoutError('Model request deadline reached before startup')
    command = [sys.executable, '-B', str(Path(__file__).with_name('transport_worker.py'))]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        if on_start:
            on_start(process.pid)
        payload = json.dumps({'parent_pid': os.getpid(), 'request': kwargs}, ensure_ascii=False).encode('utf-8')
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise HardTimeoutError('Model request deadline reached during startup')
        try:
            stdout, _ = process.communicate(payload, timeout=remaining)
        except subprocess.TimeoutExpired:
            raise HardTimeoutError('Model request exceeded hard wall-clock deadline') from None
        # Scheduling can delay communicate() returning even when output is ready.
        # A late response must never mutate usage, circuit state, or predictions.
        if time.monotonic() >= deadline:
            raise HardTimeoutError('Model response arrived after hard deadline')
        if process.returncode:
            raise TransportError('WorkerExit', service_fault=True)
        try:
            envelope = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise TransportError('WorkerProtocolError') from None
        if not isinstance(envelope, dict):
            raise TransportError('WorkerProtocolError')
        if 'error' in envelope:
            error = envelope['error']
            raise TransportError(error.get('type', 'WorkerError'), error.get('status_code'), error.get('service_fault', False))
        data = envelope.get('response')
        if not isinstance(data, dict):
            raise TransportError('WorkerProtocolError')
        return ProviderResponse(data)
    finally:
        if process.poll() is None:
            process.kill()
        # kill is immediate on supported Windows/Linux hosts; bounded reaping
        # avoids another unbounded wait in the deadline enforcement itself.
        try:
            process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.5)
        for stream in (process.stdin, process.stdout):
            if stream and not stream.closed:
                stream.close()
