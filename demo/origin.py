"""시연용 로컬 원본 API. 결과를 바꾸고 호출 횟수를 확인한다. 명세: docs/c-hashtable-and-bindings.md

JSON POST로 원본 응답을 제공하고 버전을 키마다 올려 결과를 바꾼다.
상태는 앱마다 갖고 스레드 잠금으로 보호한다. 캐시 서버와 다른 역할이며 mini_redis를 import하지 않는다.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, Sequence

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

if TYPE_CHECKING:
    from flask import Response

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001


class _OriginState:
    """키별 버전과 /fetch 호출 횟수를 앱마다 보관한다."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.versions: dict[str, int] = {}
        self.calls = 0

    def value(self, key: str, version: int) -> str:
        """원본 값 형식을 만든다."""
        return f"{key}:v{version}"


def _reject() -> tuple[Response, int]:
    """잘못된 요청 본문을 400으로 응답한다."""
    return jsonify({"error": "invalid_input"}), 400


def _requested_key() -> Any:
    """JSON 객체 본문에서 문자열 key를 꺼낸다. 형식이 틀리면 None을 돌려준다."""
    # get_json은 application/*+json도 받아들이므로 미디어 타입을 정확히 application/json으로 제한한다.
    if request.mimetype != "application/json":
        return None
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None
    key = body.get("key")
    if not isinstance(key, str):
        return None
    return key


def create_app() -> Flask:
    """원본 앱을 만들고 앱마다 새 상태를 둔다. 서버는 시작하지 않는다."""
    app = Flask(__name__)
    app.config.setdefault("ORIGIN_DELAY", 0.0)
    state = _OriginState()

    @app.post("/fetch")
    def fetch():
        key = _requested_key()
        if key is None:
            return _reject()
        # 지연은 락을 잡지 않고 기다려 동시 요청을 막지 않는다.
        delay = app.config["ORIGIN_DELAY"]
        if delay:
            time.sleep(delay)
        with state.lock:
            state.calls += 1
            version = state.versions.get(key, 1)
            value = state.value(key, version)
        return jsonify({"key": key, "value": value, "version": version})

    @app.post("/update")
    def update():
        key = _requested_key()
        if key is None:
            return _reject()
        with state.lock:
            version = state.versions.get(key, 1) + 1
            state.versions[key] = version
        return jsonify({"key": key, "version": version})

    @app.get("/stats")
    def stats():
        with state.lock:
            return jsonify({"calls": state.calls})

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """원본 API를 로컬에서 실행하고 실제 포트를 한 줄 출력한다."""
    parser = argparse.ArgumentParser(prog="origin", description="시연용 로컬 원본 API")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.delay < 0:
        parser.error("--delay must not be negative")

    app = create_app()
    app.config["ORIGIN_DELAY"] = args.delay

    server = make_server(args.host, args.port, app, threaded=True)
    # 안내 줄을 읽은 쪽이 곧바로 신호를 보내도 처리기가 이미 설치돼 있어야 한다.
    previous = _install_stop_signals()
    try:
        print(f"origin listening on http://{args.host}:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def _install_stop_signals() -> dict[int, Any]:
    """Ctrl+Break(Windows)와 SIGTERM도 Ctrl+C처럼 정상 종료하게 한다. main 스레드에서만 부른다."""

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, stop)
    return previous


if __name__ == "__main__":
    raise SystemExit(main())
