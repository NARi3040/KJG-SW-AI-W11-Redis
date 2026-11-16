"""Runtime: single worker, FIFO queue, periodic deadlines and lifecycle.

명세: docs/http-and-runtime.md, docs/planning.md 3.5·3.7·5.4절.
작업자 하나가 C 테이블을 만들고, 스냅샷을 복구하고, Store를 게시한 뒤 요청·청소·저장을
순서대로 실행한다. 생성자는 스레드, 테이블, 파일 접근을 시작하지 않는다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Callable

from .contracts import BusyError, Operation, UnavailableError, WorkerFailedError
from .hashtable import HashTable
from .snapshot import read_snapshot, write_snapshot
from .store import Store

logger = logging.getLogger(__name__)

_QUEUE_SIZE = 64
_CLEANUP_INTERVAL = 1.0
_SNAPSHOT_INTERVAL = 60.0
_MAX_IDLE_WAIT = 0.1

_Task = tuple[Operation, str, str | None, int | None, Future]


class Runtime:
    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.data_dir = data_dir
        self.clock = clock
        self.monotonic = monotonic

        self._lock = threading.Lock()
        self._state = "STARTING"
        self._queue: queue.Queue[_Task] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._ready: Future = Future()
        self._stop_event = threading.Event()
        self._activation = threading.Event()
        self._worker: threading.Thread | None = None
        self._store: Store | None = None
        self._activated = False
        self._final_snapshot = False
        self._cleanup_deadline = 0.0
        self._snapshot_deadline = 0.0
        self._inflight: Future | None = None
        self._exit_code = 0
        self._snapshot_path = data_dir / "snapshot.json"

    def start(self) -> None:
        assert self._worker is None, "start called twice"
        self._worker = threading.Thread(
            target=self._worker_main, name="mini-redis-worker", daemon=False
        )
        self._worker.start()

    def wait_ready(self) -> None:
        self._ready.result()

    def activate(self) -> None:
        assert self._worker is not None, "activate before start"
        self._ready.result()
        with self._lock:
            assert self._state == "STARTING", f"cannot activate in {self._state}"
            start = self.monotonic()
            self._cleanup_deadline = start + _CLEANUP_INTERVAL
            self._snapshot_deadline = start + _SNAPSHOT_INTERVAL
            self._activated = True
            self._state = "RUNNING"
        self._activation.set()

    def submit(
        self,
        operation: Operation,
        *,
        key: str,
        value: str | None = None,
        ttl: int | None = None,
    ) -> Future:
        future: Future = Future()
        with self._lock:
            if self._state != "RUNNING":
                raise UnavailableError(f"runtime is {self._state}")
            try:
                self._queue.put_nowait((operation, key, value, ttl, future))
            except queue.Full:
                raise BusyError("waiting queue is full") from None
        return future

    def request_stop(self, *, final_snapshot: bool = True) -> None:
        with self._lock:
            if self._state in ("STARTING", "RUNNING"):
                self._state = "STOPPING"
                # 첫 성공 활성화 전에는 서비스 변경이 없으므로 최종 저장을 생략한다.
                self._final_snapshot = final_snapshot and self._activated
        self._stop_event.set()
        self._activation.set()

    def join(self) -> int:
        assert self._worker is not None, "join before start"
        self._worker.join()
        return self._exit_code

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _worker_main(self) -> None:
        table: HashTable | None = None
        try:
            table = HashTable()
            self._recover(table)
            store = Store(table, clock=self.clock)
            with self._lock:
                self._store = store
            self._ready.set_result(None)

            while not self._activation.is_set() and not self._stop_event.is_set():
                self._activation.wait(_MAX_IDLE_WAIT)

            if self._stop_event.is_set():
                self._drain()
            else:
                self._run_loop()
        except BaseException as exc:
            self._fail(exc)
        finally:
            if table is not None:
                try:
                    table.close()
                except BaseException as exc:
                    logger.error("hashtable close failed (%s)", type(exc).__name__)
                    self._exit_code = 1
            self._finish_state()

    def _recover(self, table: HashTable) -> None:
        entries = read_snapshot(self._snapshot_path)
        if not entries:
            return
        # 복구 시각은 한 번만 읽고 원래 절대 만료 시각으로 삽입한다. TTL을 다시 시작하지 않는다.
        now = self.clock()
        for key, value, expires_at in entries:
            if expires_at is None or now < expires_at:
                table.put(
                    key.encode("utf-8", "surrogatepass"),
                    value.encode("utf-8", "surrogatepass"),
                    expires_at,
                )

    def _run_loop(self) -> None:
        pending: _Task | None = None
        while True:
            if self._halt_requested():
                if pending is not None:
                    self._execute(pending)
                    pending = None
                self._drain()
                return

            self._run_due_work()
            if self._halt_requested():
                continue

            if pending is None:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    now = self.monotonic()
                    timeout = min(
                        _MAX_IDLE_WAIT,
                        max(0.0, self._cleanup_deadline - now),
                        max(0.0, self._snapshot_deadline - now),
                    )
                    try:
                        pending = self._queue.get(timeout=timeout)
                    except queue.Empty:
                        continue
                    # 꺼낸 작업의 Future를 즉시 작업자 소유로 잡아 실패 시 해제한다.
                    self._inflight = pending[4]
                    # 유휴 대기 중 도래한 기한을 이 작업 전에 한 번 더 확인한다.
                    continue
                self._inflight = pending[4]

            self._execute(pending)
            pending = None

    def _run_due_work(self) -> None:
        """한 번의 due 배치: 청소 최대 1회, 스냅샷 최대 1회. 밀린 횟수를 반복하지 않는다."""
        now = self.monotonic()
        if now >= self._cleanup_deadline and not self._halt_requested():
            self._cleanup()
            self._cleanup_deadline = self.monotonic() + _CLEANUP_INTERVAL
        now = self.monotonic()
        if now >= self._snapshot_deadline and not self._halt_requested():
            self._snapshot()
            self._snapshot_deadline = self.monotonic() + _SNAPSHOT_INTERVAL

    def _halt_requested(self) -> bool:
        with self._lock:
            state = self._state
        return state != "RUNNING" or self._stop_event.is_set()

    def _drain(self) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            self._execute(task)
        if self._final_snapshot:
            self._final_save()

    def _execute(self, task: _Task) -> None:
        operation, key, value, ttl, future = task
        assert self._store is not None
        self._inflight = future
        try:
            if operation == "set":
                result = self._store.set(key, value, ttl=ttl)
            elif operation == "get":
                result = self._store.get(key)
            elif operation == "delete":
                result = self._store.delete(key)
            else:
                raise AssertionError(f"unknown operation {operation!r}")
        except MemoryError as exc:
            future.set_exception(exc)
            self._inflight = None
        else:
            future.set_result(result)
            self._inflight = None
        # 그 밖의 예외는 그대로 전파해 최외곽에서 FAILED로 전환하고 이 Future를 해제한다.

    def _cleanup(self) -> None:
        try:
            assert self._store is not None
            self._store.cleanup()
        except MemoryError as exc:
            logger.warning(
                "cleanup failed (%s); retrying at next deadline", type(exc).__name__
            )

    def _snapshot(self) -> None:
        try:
            assert self._store is not None
            write_snapshot(self._snapshot_path, self._store.snapshot_entries())
        except (OSError, ValueError, MemoryError) as exc:
            logger.warning(
                "snapshot failed (%s); retrying at next deadline", type(exc).__name__
            )

    def _final_save(self) -> None:
        try:
            assert self._store is not None
            write_snapshot(self._snapshot_path, self._store.snapshot_entries())
        except (OSError, ValueError, MemoryError) as exc:
            logger.warning("final snapshot failed (%s)", type(exc).__name__)
            self._exit_code = 1

    def _fail(self, exc: BaseException) -> None:
        logger.error("worker failed: %s", type(exc).__name__)
        with self._lock:
            self._state = "FAILED"
            self._final_snapshot = False
        if not self._ready.done():
            self._ready.set_exception(exc)
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            inflight.set_exception(WorkerFailedError("worker failed"))
        while True:
            try:
                *_, future = self._queue.get_nowait()
            except queue.Empty:
                break
            if not future.done():
                future.set_exception(WorkerFailedError("worker failed"))
        self._exit_code = 1

    def _finish_state(self) -> None:
        with self._lock:
            if self._state != "FAILED":
                self._state = "STOPPED"
