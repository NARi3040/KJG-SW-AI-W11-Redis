"""Controller entry point skeleton."""

from __future__ import annotations

from typing import Sequence

from .http import create_app
from .runtime import Runtime


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
