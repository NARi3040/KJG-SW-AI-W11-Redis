"""서버 진입점: 데이터 경로, 서버 조립, 신호와 종료 코드."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from werkzeug.serving import make_server

from .http import create_app
from .runtime import Runtime

logger = logging.getLogger(__name__)

_CONTROL_POLL = 0.1


def _signals() -> list[int]:
    names = ["SIGINT", "SIGTERM"]
    if sys.platform == "win32":
        names.append("SIGBREAK")
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, object]:
    previous = {}

    def _handler(signum, frame):
        stop_event.set()

    for sig in _signals():
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, _handler)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mini Redis HTTP server")
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)
    runtime = None
    server = None
    server_thread: threading.Thread | None = None
    started = False
    activated = False
    exit_code = 0

    def fail(stage: str, exc: BaseException) -> None:
        nonlocal exit_code
        logger.error("%s failed (%s)", stage, type(exc).__name__)
        exit_code = 1

    try:
        try:
            data_dir = args.data_dir.resolve()
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            fail("data directory setup", exc)
            return exit_code

        try:
            runtime = Runtime(data_dir)
            runtime.start()
            started = True
        except Exception as exc:
            fail("startup", exc)
        else:
            try:
                runtime.wait_ready()
            except Exception as exc:
                fail("startup", exc)
            else:
                if runtime.state == "FAILED":
                    exit_code = 1

                if exit_code == 0 and not stop_event.is_set():
                    try:
                        app = create_app(runtime)
                    except Exception as exc:
                        fail("application setup", exc)
                    else:
                        try:
                            server = make_server(
                                "127.0.0.1", 5000, app, threaded=True
                            )
                        except (Exception, SystemExit) as exc:
                            # Werkzeug의 바인드 실패는 SystemExit으로 전달될 수 있다.
                            fail("bind", exc)
                    if server is not None and exit_code == 0:
                        if stop_event.is_set() or runtime.state != "STARTING":
                            if runtime.state == "FAILED":
                                exit_code = 1
                        else:
                            try:
                                runtime.activate()
                            except Exception as exc:
                                fail("activation", exc)
                            else:
                                activated = True
                                if stop_event.is_set() or runtime.state != "RUNNING":
                                    if runtime.state == "FAILED":
                                        exit_code = 1
                                else:
                                    thread = threading.Thread(
                                        target=server.serve_forever,
                                        args=(0.1,),
                                        name="mini-redis-server",
                                        daemon=False,
                                    )
                                    try:
                                        thread.start()
                                    except Exception as exc:
                                        fail("server start", exc)
                                    else:
                                        server_thread = thread
                                        while not stop_event.wait(_CONTROL_POLL):
                                            if (
                                                runtime.state == "FAILED"
                                                or not server_thread.is_alive()
                                            ):
                                                exit_code = 1
                                                break
    finally:
        try:
            if runtime is not None and started:
                try:
                    runtime.request_stop(final_snapshot=activated)
                except Exception as exc:
                    fail("stop", exc)
                if server_thread is not None:
                    if server_thread.is_alive():
                        try:
                            server.shutdown()
                        except Exception as exc:
                            fail("server shutdown", exc)
                    try:
                        server_thread.join()
                    except Exception as exc:
                        fail("server join", exc)
                if server is not None:
                    try:
                        server.server_close()
                    except Exception as exc:
                        fail("server close", exc)
                try:
                    if runtime.join() != 0:
                        exit_code = 1
                except Exception as exc:
                    fail("join", exc)
        finally:
            _restore_signal_handlers(previous)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
