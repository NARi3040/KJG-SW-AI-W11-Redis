"""Runtime API skeleton. Worker behavior is not implemented."""

from __future__ import annotations

import time
from concurrent.futures import Future
from pathlib import Path
from typing import Callable

from .contracts import Operation
from .hashtable import HashTable


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

    def start(self) -> None:
        raise NotImplementedError

    def wait_ready(self) -> None:
        raise NotImplementedError

    def activate(self) -> None:
        raise NotImplementedError

    def submit(
        self,
        operation: Operation,
        *,
        key: str,
        value: str | None = None,
        ttl: int | None = None,
    ) -> Future:
        raise NotImplementedError

    def request_stop(self, *, final_snapshot: bool = True) -> None:
        raise NotImplementedError

    def join(self) -> int:
        raise NotImplementedError

    @property
    def state(self) -> str:
        raise NotImplementedError
