"""컨트롤러 시험용 프로세스 하네스와 자식 래퍼.

자식은 실제 C 테이블·Runtime·HTTP를 유지하고 기존 이름만 monkeypatch하며,
stdin/stdout 승인으로 시험 단계 시점을 고정한다.
"""

from __future__ import annotations

import argparse
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CHILD = Path(__file__).resolve()
_HELD_SOCKET: socket.socket | None = None
_APPROVALS: queue.Queue[str] = queue.Queue()


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _notify(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _wait_for_approval() -> None:
    while True:
        try:
            line = _APPROVALS.get(timeout=0.05)
        except queue.Empty:
            continue
        if line == "go":
            return
        raise SystemExit(2)


def _start_approval_reader() -> None:
    def read_stdin() -> None:
        assert sys.stdin is not None
        for line in sys.stdin:
            _APPROVALS.put(line.strip())

    threading.Thread(target=read_stdin, name="child-stdin", daemon=True).start()


def _gate_server(
    controller,
    *,
    occupied: bool,
    bind_gate: bool,
    exit_after_ready: bool,
    advance_after_bind,
):
    global _HELD_SOCKET
    real_make_server = controller.make_server
    held_socket = None
    occupied_port = 0
    if occupied:
        held_socket = socket.socket()
        held_socket.bind(("127.0.0.1", 0))
        held_socket.listen(1)
        occupied_port = held_socket.getsockname()[1]
        _HELD_SOCKET = held_socket

    class NotifyingServer:
        def __init__(self, server) -> None:
            self._server = server

        def serve_forever(self, *args, **kwargs):
            _notify(f"ready {self._server.server_port}")
            if exit_after_ready:
                return
            return self._server.serve_forever(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._server, name)

    def make_server(host, port, app, **kwargs):
        if occupied:
            assert held_socket is not None
        server = real_make_server(
            host, occupied_port if occupied else 0, app, **kwargs
        )
        if bind_gate:
            _notify(f"bound {server.server_port}")
            _wait_for_approval()
            if advance_after_bind is not None:
                advance_after_bind()
        return NotifyingServer(server)

    controller.make_server = make_server


def _gate_activation(controller) -> None:
    real_activate = controller.Runtime.activate

    def activate(self):
        real_activate(self)
        _notify("activated")
        _wait_for_approval()

    controller.Runtime.activate = activate


def _gate_wait_ready(controller) -> None:
    real_wait_ready = controller.Runtime.wait_ready

    def wait_ready(self):
        real_wait_ready(self)
        _notify("recovery_done")
        _wait_for_approval()

    controller.Runtime.wait_ready = wait_ready


def _report_deadlines(controller) -> None:
    real_activate = controller.Runtime.activate

    def activate(self):
        real_activate(self)
        _notify(f"deadlines {self._cleanup_deadline} {self._snapshot_deadline}")

    controller.Runtime.activate = activate


def _fail_activation(controller) -> None:
    def activate(self):
        raise RuntimeError("injected activation failure")

    controller.Runtime.activate = activate


def _trace_stop_signal(controller) -> None:
    real_install = controller._install_signal_handlers

    def install(stop_event):
        previous = real_install(stop_event)

        def handler(signum, frame):
            stop_event.set()
            _notify("stop_signal")

        for sig in controller._signals():
            signal.signal(sig, handler)
        return previous

    controller._install_signal_handlers = install


def _fake_runtime(runtime_module):
    clock = FakeClock(1000.0)

    class FixedRuntime(runtime_module.Runtime):
        def __init__(self, data_dir, **kwargs):
            kwargs.setdefault("monotonic", clock)
            super().__init__(data_dir, **kwargs)

    return FixedRuntime, clock


def _fail_requests(runtime_module) -> None:
    real_table = runtime_module.HashTable

    class FailingTable(real_table):
        def get(self, key):
            raise RuntimeError("injected request failure")

    runtime_module.HashTable = FailingTable


def _trace_worker(runtime_module) -> None:
    real_table = runtime_module.HashTable

    class TracedTable(real_table):
        def __init__(self):
            super().__init__()
            _notify("worker_started")

        def close(self):
            super().close()
            _notify("worker_closed")

    runtime_module.HashTable = TracedTable


def _fail_final_snapshot(runtime_module) -> None:
    def failing_write(path, entries):
        raise OSError("injected final snapshot failure")

    runtime_module.write_snapshot = failing_write


def _freeze_periodic_snapshot(runtime_module) -> None:
    # 짧은 자식 실행에서 기존 파일을 마지막 성공 스냅샷으로 고정한다.
    runtime_module._SNAPSHOT_INTERVAL = 10**9


def _gate_replace(snapshot_module, *, before: bool, after: bool) -> None:
    real_replace = snapshot_module.os.replace
    state = {"gated": False}

    def replace(src, dst):
        if before and not state["gated"]:
            state["gated"] = True
            _notify(f"before_replace {src}")
            _wait_for_approval()
            return real_replace(src, dst)
        result = real_replace(src, dst)
        if after and not state["gated"]:
            state["gated"] = True
            _notify(f"after_replace {dst}")
            _wait_for_approval()
        return result

    snapshot_module.os.replace = replace


def _child_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--bind-occupied", action="store_true")
    parser.add_argument("--bind-gate", action="store_true")
    parser.add_argument("--gate-activate", action="store_true")
    parser.add_argument("--gate-wait-ready", action="store_true")
    parser.add_argument("--fake-monotonic", action="store_true")
    parser.add_argument("--advance-after-bind", type=float, default=0.0)
    parser.add_argument("--report-deadlines", action="store_true")
    parser.add_argument("--fail-activate", action="store_true")
    parser.add_argument("--fail-request", action="store_true")
    parser.add_argument("--exit-after-ready", action="store_true")
    parser.add_argument("--trace-worker", action="store_true")
    parser.add_argument("--trace-stop-signal", action="store_true")
    parser.add_argument("--fail-final-save", action="store_true")
    parser.add_argument("--freeze-periodic", action="store_true")
    parser.add_argument("--gate-before-replace", action="store_true")
    parser.add_argument("--gate-after-replace", action="store_true")
    args = parser.parse_args()

    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    _start_approval_reader()

    import mini_redis.__main__ as controller
    import mini_redis.runtime as runtime_module
    import mini_redis.snapshot as snapshot_module

    advance_after_bind = None
    if args.fake_monotonic:
        fixed_runtime, clock = _fake_runtime(runtime_module)
        controller.Runtime = fixed_runtime
        if args.advance_after_bind:
            advance_after_bind = lambda: clock.advance(args.advance_after_bind)

    _gate_server(
        controller,
        occupied=args.bind_occupied,
        bind_gate=args.bind_gate,
        exit_after_ready=args.exit_after_ready,
        advance_after_bind=advance_after_bind,
    )
    if args.gate_wait_ready:
        _gate_wait_ready(controller)
    if args.gate_activate:
        _gate_activation(controller)
    if args.report_deadlines:
        _report_deadlines(controller)
    if args.fail_activate:
        _fail_activation(controller)
    if args.trace_worker:
        _trace_worker(runtime_module)
    if args.trace_stop_signal:
        _trace_stop_signal(controller)
    if args.fail_request:
        _fail_requests(runtime_module)
    if args.fail_final_save:
        _fail_final_snapshot(runtime_module)
    if args.freeze_periodic:
        _freeze_periodic_snapshot(runtime_module)
    if args.gate_before_replace or args.gate_after_replace:
        _gate_replace(
            snapshot_module,
            before=args.gate_before_replace,
            after=args.gate_after_replace,
        )

    return controller.main(["--data-dir", args.data_dir])


class Child:
    """줄 프로토콜로 조종하는 실제 mini_redis 자식 프로세스."""

    def __init__(self, data_dir: Path, *args: str) -> None:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [sys.executable, str(_CHILD), "--data-dir", str(data_dir), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(_ROOT),
            creationflags=creationflags,
        )
        self.messages: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        self.stderr: list[str] = []
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="child-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="child-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            text = line.rstrip("\n")
            self.seen.append(text)
            self.messages.put(text)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip("\n"))

    def read(self, timeout: float = 10.0) -> str:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"child message timeout; exit={self.proc.poll()}; "
                f"stderr={''.join(self.stderr)}"
            ) from None

    def expect(self, prefix: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            line = self.read(remaining)
            if line.startswith(prefix):
                return line

    def ready(self, timeout: float = 10.0) -> int:
        line = self.expect("ready ", timeout)
        return int(line.split(" ", 1)[1])

    def approve(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write("go\n")
        self.proc.stdin.flush()

    def wait(self, timeout: float = 15.0) -> int:
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            raise AssertionError(
                f"child did not exit; stderr={''.join(self.stderr)}"
            ) from None
        self._stdout_thread.join(2)
        return code

    def kill_wait(self, timeout: float = 15.0) -> int:
        if self.proc.poll() is None:
            self.proc.kill()
        return self.wait(timeout)

    def normal_stop(self) -> None:
        if sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.send_signal(signal.SIGTERM)

    def __enter__(self) -> Child:
        return self

    def __exit__(self, *exc_info) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(_child_main())
