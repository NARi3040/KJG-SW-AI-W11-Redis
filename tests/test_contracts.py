"""Passing declaration checks only; these do not validate server behavior."""

import importlib.util
import inspect
import subprocess
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Literal, Sequence, get_type_hints

import pytest
from flask import Flask, Response

from mini_redis import __main__, contracts, http, snapshot, store
from mini_redis.hashtable import HashTable
from mini_redis.runtime import Runtime
from mini_redis.store import Store

ROOT = Path(__file__).resolve().parents[1]


def load_demo(name):
    """demo는 패키지가 아니므로 파일 경로로 불러온다."""
    path = ROOT / "demo" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"demo_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_origin = load_demo("origin")
_benchmark = load_demo("benchmark")
_client = load_demo("client")


def test_shared_types_and_errors():
    assert contracts.Operation == Literal["set", "get", "delete"]
    assert contracts.Entry == tuple[str, str, float | None]
    assert contracts.BusyError.__bases__ == (RuntimeError,)
    assert contracts.UnavailableError.__bases__ == (RuntimeError,)
    assert contracts.WorkerFailedError.__bases__ == (contracts.UnavailableError,)


_SELF = ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)

OWNER1_SIGNATURES = [
    (HashTable, [], {"return": type(None)}),
    *[
        (getattr(HashTable, name), parameters, hints)
        for name, parameters, hints in [
            ("put", [_SELF,
                     ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                     ("value", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                     ("expires_at", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"key": bytes, "value": bytes, "expires_at": float | None, "return": type(None)}),
            ("is_live", [_SELF,
                         ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
                         ("cutoff", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"key": bytes, "cutoff": float, "return": bool}),
            ("get", [_SELF,
                     ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"key": bytes, "return": tuple[bytes, float | None] | None}),
            ("remove", [_SELF,
                        ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"key": bytes, "return": bool}),
            ("remove_expired", [_SELF,
                                ("cutoff", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"cutoff": float, "return": type(None)}),
            ("copy_live", [_SELF,
                           ("cutoff", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
             {"cutoff": float, "return": list[tuple[bytes, bytes, float | None]]}),
            ("close", [_SELF], {"return": type(None)}),
            ("_test_fail_alloc_after", [_SELF,
                                        ("successful_allocations", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                         inspect.Parameter.empty)],
             {"successful_allocations": int, "return": type(None)}),
            ("_test_dump", [_SELF], {"return": tuple[list[tuple[bytes, bytes, float | None]], int, int]}),
        ]
    ],
    (_origin.create_app, [], {"return": Flask}),
    (_origin.main,
     [("argv", inspect.Parameter.POSITIONAL_OR_KEYWORD, None)],
     {"argv": Sequence[str] | None, "return": int}),
    (_benchmark.main,
     [("argv", inspect.Parameter.POSITIONAL_OR_KEYWORD, None)],
     {"argv": Sequence[str] | None, "return": int}),
]

OWNER2_SIGNATURES = [
    (Store,
     [
        ("table", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("clock", inspect.Parameter.KEYWORD_ONLY, time.time),
     ],
     {"table": HashTable, "clock": Callable[[], float], "return": type(None)}),
    (Store.set,
     [
        _SELF,
        ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("value", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("ttl", inspect.Parameter.KEYWORD_ONLY, None),
     ],
     {"key": str, "value": str, "ttl": int | None, "return": bool}),
    (Store.get, [_SELF, ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
     {"key": str, "return": str | None}),
    (Store.delete, [_SELF, ("key", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
     {"key": str, "return": bool}),
    (Store.cleanup, [_SELF], {"return": type(None)}),
    (Store.snapshot_entries, [_SELF], {"return": list[contracts.Entry]}),
    (snapshot.read_snapshot,
     [("path", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)],
     {"path": Path, "return": list[contracts.Entry] | None}),
    (snapshot.write_snapshot,
     [
        ("path", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("entries", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
     ],
     {"path": Path, "entries": list[contracts.Entry], "return": type(None)}),
    (_client.main,
     [("argv", inspect.Parameter.POSITIONAL_OR_KEYWORD, None)],
     {"argv": Sequence[str] | None, "return": int}),
]

OWNER3_SIGNATURES = [
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
]


@pytest.mark.parametrize(
    ("function", "parameters", "hints"),
    [*OWNER1_SIGNATURES, *OWNER2_SIGNATURES, *OWNER3_SIGNATURES],
)
def test_signatures_and_defaults(function, parameters, hints):
    assert [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()] == parameters
    target = function.__init__ if inspect.isclass(function) else function
    assert get_type_hints(target, localns={"Runtime": Runtime, "HashTable": HashTable, "Flask": Flask}) == hints


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
        lambda r: __main__.main(),
        lambda r: __main__.main(["--data-dir", "unused"]),
    ],
)
def test_unimplemented_behavior_fails_fast(call, tmp_path):
    with pytest.raises(NotImplementedError):
        call(Runtime(tmp_path))


@pytest.mark.parametrize(
    "path,handler,payload,result",
    [
        ("/set", http.handle_set, {"key": "k", "value": "v"}, True),
        ("/get", http.handle_get, {"key": "k"}, "v"),
        ("/delete", http.handle_delete, {"key": "k"}, False),
    ],
)
def test_implemented_http_handlers(path, handler, payload, result):
    from unittest.mock import Mock

    future = Future()
    future.set_result(result)
    runtime = Mock(submit=Mock(return_value=future))
    app = http.create_app(runtime)
    assert isinstance(app, Flask)
    with app.test_request_context(path, method="POST", json=payload):
        response = handler()
    assert isinstance(response, Response)
    assert response.get_json() == {"result": result}
    runtime.submit.assert_called_once()


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
    assert {m for m in sys.modules if m.startswith("mini_redis.")} == {"mini_redis.contracts"}
    import mini_redis.hashtable
    import mini_redis.store
    import mini_redis.snapshot
    assert "mini_redis.runtime" not in sys.modules
    assert "mini_redis.http" not in sys.modules
    before_client = {m for m in sys.modules if m.startswith("mini_redis")}
    import demo.client
    assert {m for m in sys.modules if m.startswith("mini_redis")} == before_client
    import mini_redis.runtime
    assert "mini_redis.http" not in sys.modules
    import mini_redis.http
    import mini_redis.__main__
    mini_redis.runtime.Runtime(Path("unused"), clock=forbidden, monotonic=forbidden)
    mini_redis.store.Store(object(), clock=forbidden)
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
