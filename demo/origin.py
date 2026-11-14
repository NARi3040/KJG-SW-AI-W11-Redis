"""시연용 로컬 원본 API. 결과를 바꾸고 호출 횟수를 확인한다. 명세: docs/c-hashtable-and-bindings.md"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from flask import Flask


def create_app() -> Flask:
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
