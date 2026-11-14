"""스냅샷 파일 단위 사례 뼈대. 사례 정의: docs/store-and-snapshots.md

MR-00 뼈대: 사례마다 자리만 두고 NotImplementedError로 실패시킨다. 통과로 표시하지 않는다.
정지와 재시작이 필요한 사례는 tests/test_integration_store.py에 둔다.
"""


def test_s_t6_only_last_snapshot_in_file():
    """성공한 스냅샷 파일은 그 시각의 항목만 담는다. 이후 변경은 파일에 반영되지 않는다."""
    raise NotImplementedError


def test_s_t7_reads_original_absolute_expiry():
    """E=110.25와 무기한 항목을 원래 절대 만료 시각 그대로 읽는다. TTL을 처음부터 다시 시작하지 않는다. 복구 시각 필터링은 Runtime이 한다."""
    raise NotImplementedError


def test_s_t8_write_stage_failures():
    """JSON 기록, flush, fsync, os.replace 실패마다 저장 실패를 보고하고 이전 정식 파일을 보존한다. 이번 임시 파일만 정리한다."""
    raise NotImplementedError


def test_s_t9_interrupted_write_file_level():
    """기록 중 중단은 이전 정식 파일을, 교체 완료 뒤 중단은 새 정식 파일을 쓰며 임시 파일을 자동 채택하지 않는다."""
    raise NotImplementedError


def test_s_t10_restore_validation_failures():
    """정식 파일 없음만 None이고 임시 파일만 있으면 경고 후 None이다. 잘림, 중복 필드·키, 알 수 없는 필드, 버전·타입 오류는 ValueError, 권한·읽기 오류는 OSError, 할당 실패는 MemoryError로 전달한다."""
    raise NotImplementedError


def test_s_t13_surrogate_and_base64():
    r"""보충 문자 1개인 "\U0001F600"과 surrogate 2개인 "\ud83d\ude00"을 키·값으로 파일 왕복해 서로 다른 문자열로 구별한다. 잘못된 Base64는 ValueError로 거부한다."""
    raise NotImplementedError
