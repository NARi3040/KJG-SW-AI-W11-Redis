"""시연용 캐시 소비자. HTTP로만 캐시 서버를 쓰고 자동 재시도하지 않는다. 명세: docs/store-and-snapshots.md"""

from __future__ import annotations

from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """원본 응답 저장·재사용과 원본 변경 후 캐시 키 명시적 삭제 흐름을 실행한다."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
