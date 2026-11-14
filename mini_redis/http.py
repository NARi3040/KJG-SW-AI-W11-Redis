"""HTTP validation and result conversion; no runtime resources are created."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from flask import Flask, Response, current_app, jsonify, request

from .contracts import BusyError, Operation, UnavailableError

if TYPE_CHECKING:
    from .runtime import Runtime


def create_app(runtime: Runtime) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = None
    app.json.ensure_ascii = True
    app.extensions["mini_redis_runtime"] = runtime
    app.add_url_rule("/set", view_func=handle_set, methods=["POST"], provide_automatic_options=False)
    app.add_url_rule("/get", view_func=handle_get, methods=["POST"], provide_automatic_options=False)
    app.add_url_rule("/delete", view_func=handle_delete, methods=["POST"], provide_automatic_options=False)
    for exception, status, code in (
        (404, 404, "not_found"),
        (405, 405, "method_not_allowed"),
        (BusyError, 503, "busy"),
        (UnavailableError, 503, "unavailable"),
        (MemoryError, 500, "memory_error"),
        (Exception, 500, "internal_error"),
    ):
        app.register_error_handler(
            exception, lambda error, status=status, code=code: _error(status, code)
        )
    return app


def _error(status: int, code: str) -> Response:
    response = jsonify(error={"code": code})
    response.status_code = status
    return response


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError
        fields[key] = value
    return fields


def _reject_constant(token: str) -> None:
    raise ValueError


def _handle(operation: Operation) -> Response:
    if request.mimetype != "application/json":
        return _error(415, "unsupported_media_type")
    try:
        fields = json.loads(
            request.get_data().decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
        )
        required = {"key", "value"} if operation == "set" else {"key"}
        allowed = required | {"ttl"} if operation == "set" else required
        if not isinstance(fields, dict) or not required <= fields.keys() <= allowed:
            raise ValueError
        if any(not isinstance(fields[key], str) for key in required):
            raise ValueError
        if "ttl" in fields and (
            type(fields["ttl"]) is not int or not 1 <= fields["ttl"] <= 2_147_483_647
        ):
            raise ValueError
    except (ValueError, RecursionError):
        return _error(400, "invalid_input")

    if operation == "set":
        fields["ttl"] = fields.get("ttl")
    result = current_app.extensions["mini_redis_runtime"].submit(
        operation, **fields
    ).result()
    return jsonify(result=result)


def handle_set() -> Response:
    return _handle("set")


def handle_get() -> Response:
    return _handle("get")


def handle_delete() -> Response:
    return _handle("delete")
