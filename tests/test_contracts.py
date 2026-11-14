"""Passing declaration checks only; these do not validate server behavior."""

import inspect
import subprocess
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Literal, Sequence, get_type_hints

import pytest
from flask import Flask, Response

from mini_redis import __main__, contracts, http
from mini_redis.runtime import Runtime


def test_shared_types_and_errors():
    assert contracts.Operation == Literal["set", "get", "delete"]
    assert contracts.Entry == tuple[str, str, float | None]
    assert contracts.BusyError.__bases__ == (RuntimeError,)
    assert contracts.UnavailableError.__bases__ == (RuntimeError,)
    assert contracts.WorkerFailedError.__bases__ == (contracts.UnavailableError,)


@pytest.mark.parametrize(
    ("function", "parameters", "hints"),
    [
        (
            Runtime,
            [
                ("data_dir", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                ("clock", inspect.Parameter.KEYWORD_ONLY, time.time),
                ("monotonic", inspect.Parameter.KEYWORD_ONLY, time.monotonic),
            ],
            {"data_dir": Path, "clock": Callable[[], float], "monotonic": Callable[[], float]},
        ),
        *[
            (getattr(Runtime, name), [("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)], {"return": result})
            for name, result in [
                ("start", type(None)), ("wait_ready", type(None)),
                ("activate", type(None)), ("join", int),
            ]
        ],
        (
            Runtime.submit,
            [
                ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                ("operation", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                ("key", inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.empty),
                ("value", inspect.Parameter.KEYWORD_ONLY, None),
                ("ttl", inspect.Parameter.KEYWORD_ONLY, None),
            ],
            {"operation": contracts.Operation, "key": str, "value": str | None, "ttl": int | None, "return": Future},
        ),
        (
            Runtime.request_stop,
            [
                ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                ("final_snapshot", inspect.Parameter.KEYWORD_ONLY, True),
            ],
            {"final_snapshot": bool, "return": type(None)},
        ),
        (
            Runtime.state.fget,
            [("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
            {"return": str},
        ),
        (
            http.create_app,
            [("runtime", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
            {"runtime": Runtime, "return": Flask},
        ),
        *[(handler, [], {"return": Response}) for handler in (http.handle_set, http.handle_get, http.handle_delete)],
        (
            __main__.main,
            [("argv", inspect.Parameter.POSITIONAL_OR_KEYWORD, None)],
            {"argv": Sequence[str] | None, "return": int},
        ),
    ],
)
def test_signatures_and_defaults(function, parameters, hints):
    assert [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()] == parameters
    target = function.__init__ if function is Runtime else function
    assert get_type_hints(target, localns={"Runtime": Runtime}) == hints


def test_constructor_only_retains_inputs(tmp_path):
    def unread_clock():
        raise AssertionError("constructor read a clock")

    data_dir = tmp_path / "not-created"
    runtime = Runtime(data_dir, clock=unread_clock, monotonic=unread_clock)
    assert runtime.data_dir is data_dir
    assert runtime.clock is unread_clock
    assert runtime.monotonic is unread_clock
    assert not data_dir.exists()
    assert Runtime.state.fset is None


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.start(),
        lambda r: r.wait_ready(),
        lambda r: r.activate(),
        lambda r: r.submit("set", key="k", value="v"),
        lambda r: r.submit("get", key="k"),
        lambda r: r.submit("delete", key="k"),
        lambda r: r.request_stop(),
        lambda r: r.request_stop(final_snapshot=False),
        lambda r: r.join(),
        lambda r: r.state,
        http.create_app,
        lambda r: http.handle_set(),
        lambda r: http.handle_get(),
        lambda r: http.handle_delete(),
        lambda r: __main__.main(),
        lambda r: __main__.main(["--data-dir", "unused"]),
    ],
)
def test_unimplemented_behavior_fails_fast(call, tmp_path):
    with pytest.raises(NotImplementedError):
        call(Runtime(tmp_path))


def test_imports_and_construction_have_no_external_side_effects():
    code = """
import sys
sys.dont_write_bytecode = True
import ctypes
import socket
import threading
from pathlib import Path
from unittest.mock import patch
import flask

def forbidden(*args, **kwargs):
    raise AssertionError("import or constructor caused an external side effect")

def audit(event, args):
    if event == "open":
        mode, flags = args[1], args[2]
        import os
        if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            forbidden()
    if event in ("os.mkdir", "os.remove", "os.rename", "ctypes.dlopen", "socket.bind", "socket.connect"):
        forbidden()

sys.addaudithook(audit)
with patch.object(threading.Thread, "start", forbidden), patch.object(ctypes, "CDLL", forbidden), patch.object(socket.socket, "bind", forbidden):
    import mini_redis.contracts
    import mini_redis.runtime
    assert "mini_redis.http" not in sys.modules
    import mini_redis.http
    import mini_redis.__main__
    mini_redis.runtime.Runtime(Path("unused"), clock=forbidden, monotonic=forbidden)
    assert "mini_redis.store" not in sys.modules
    assert "mini_redis.snapshot" not in sys.modules
    assert "mini_redis.hashtable" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_module_guard_fails_explicitly():
    result = subprocess.run(
        [sys.executable, "-m", "mini_redis"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "NotImplementedError" in result.stderr
