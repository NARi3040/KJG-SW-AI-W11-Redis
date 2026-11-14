"""스냅샷 파일 저장·검증. 명세: docs/store-and-snapshots.md

파일 전체를 검증해 독립 문자열 항목을 반환하고, 임시 파일 기록·동기화·교체로 정식 파일을 바꾼다.
살아 있는 C 테이블을 받지 않으며 Runtime이나 HTTP를 import하지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .contracts import Entry

logger = logging.getLogger(__name__)

_VERSION = 1
_TOP_LEVEL_FIELDS = {"version", "entries"}
_ENTRY_FIELDS = {"key_b64", "value_b64", "expires_at"}


def _decoded_base64(raw: object) -> str:
    """문자열 Base64를 엄격하게 검증하고 UTF-8/surrogatepass 문자열로 되돌린다."""
    if not isinstance(raw, str):
        raise ValueError("base64 field is not a string")
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("invalid base64") from exc
    if base64.b64encode(decoded).decode("ascii") != raw:
        raise ValueError("non-canonical or unpadded base64")
    return decoded.decode("utf-8", "surrogatepass")


def _encoded_base64(text: str) -> str:
    """UTF-8/surrogatepass 바이트를 표준 Base64 ASCII 문자열로 기록한다."""
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


def _checked_expiry(raw: object) -> float | None:
    """만료 시각을 null 또는 불리언이 아닌 유한 숫자로 검증하고 float로 바꾼다."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("expires_at is not a number")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError("expires_at is not representable") from exc
    if not math.isfinite(value):
        raise ValueError("expires_at is not finite")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """중복 JSON 필드를 거부한다."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    """NaN, Infinity, -Infinity를 거부한다."""
    raise ValueError(f"{name} is not allowed")


def _warn_leftover_temp_files(path: Path) -> None:
    """남은 임시 파일을 파일 이름과 개수만으로 경고한다."""
    pattern = path.name + ".*.tmp"
    names = sorted(p.name for p in path.parent.glob(pattern))
    if names:
        logger.warning(
            "남은 스냅샷 임시 파일 %d개를 무시한다: %s", len(names), ", ".join(names)
        )


def read_snapshot(path: Path) -> list[Entry] | None:
    """정식 스냅샷 파일 전체를 검증하고 독립 문자열 항목 목록을 반환한다.

    None은 정식 파일이 없어 빈 시작을 뜻한다. 빈 리스트는 유효한 빈 스냅샷 파일을 뜻한다.
    만료 항목도 검증하며 시간 기준 필터링은 하지 않는다. 남은 임시 파일은 복구 데이터로
    쓰지 않고 경고만 남긴다.
    """
    _warn_leftover_temp_files(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return None

    text = raw_bytes.decode("utf-8")
    document = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )

    if not isinstance(document, dict) or set(document) != _TOP_LEVEL_FIELDS:
        raise ValueError("unexpected top-level structure")
    version = document["version"]
    if not (type(version) is int and version == _VERSION):
        raise ValueError("unsupported version")
    raw_entries = document["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError("entries is not a list")

    entries: list[Entry] = []
    seen_keys: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _ENTRY_FIELDS:
            raise ValueError("unexpected entry structure")
        key = _decoded_base64(raw_entry["key_b64"])
        value = _decoded_base64(raw_entry["value_b64"])
        expires_at = _checked_expiry(raw_entry["expires_at"])
        if key in seen_keys:
            raise ValueError("duplicate key after decoding")
        seen_keys.add(key)
        entries.append((key, value, expires_at))
    return entries


def write_snapshot(path: Path, entries: list[Entry]) -> None:
    """임시 파일 기록, 동기화, 교체를 마친 뒤 반환한다.

    스냅샷 시각을 다시 읽거나 항목을 다시 거르지 않는다. 받은 목록을 그대로 기록한다.
    """
    payload = {
        "version": _VERSION,
        "entries": [
            {
                "key_b64": _encoded_base64(key),
                "value_b64": _encoded_base64(value),
                "expires_at": expires_at,
            }
            for key, value, expires_at in entries
        ],
    }
    data = json.dumps(payload, allow_nan=False).encode("ascii")

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "wb")
    except BaseException:
        # 파일 객체가 fd를 넘겨받지 못했으므로 직접 닫아야 Windows에서 임시 파일을 지울 수 있다.
        try:
            os.close(fd)
        except OSError:
            pass
        _remove_quietly(tmp_path)
        raise
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_path, path)
    except BaseException:
        # flush 실패 뒤에도 버퍼가 남아 있으므로 닫기가 다시 예외를 낼 수 있다. 이미 닫혔으면 아무것도 하지 않는다.
        try:
            handle.close()
        except OSError:
            pass
        _remove_quietly(tmp_path)
        raise


def _remove_quietly(path: Path) -> None:
    """이번 시도의 임시 파일을 지운다. 정리 실패가 원래 예외를 가리지 않게 한다."""
    try:
        os.unlink(path)
    except OSError:
        pass
