"""Shared data types and errors; no runtime dependencies."""

from typing import Literal

Operation = Literal["set", "get", "delete"]
Entry = tuple[str, str, float | None]


class BusyError(RuntimeError):
    """The waiting queue is full."""


class UnavailableError(RuntimeError):
    """The runtime is not accepting work."""


class WorkerFailedError(UnavailableError):
    """The worker failed; accepted work may already have run."""
