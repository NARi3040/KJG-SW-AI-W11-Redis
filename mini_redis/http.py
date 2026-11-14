"""HTTP API skeleton; no app or server is created on import."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, Response

if TYPE_CHECKING:
    from .runtime import Runtime


def create_app(runtime: Runtime) -> Flask:
    raise NotImplementedError


def handle_set() -> Response:
    raise NotImplementedError


def handle_get() -> Response:
    raise NotImplementedError


def handle_delete() -> Response:
    raise NotImplementedError
