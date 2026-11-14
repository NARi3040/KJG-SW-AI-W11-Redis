"""C 해시 테이블과 ctypes 연결 단위 시험. 사례 정의: docs/c-hashtable-and-bindings.md

MR-00 뼈대: 사례마다 자리만 두고 NotImplementedError로 실패시킨다. 통과로 표시하지 않는다.
"""


def test_t17_colliding_keys():
    """16버킷에서 같은 버킷인 "a", "q" 중 "a"만 덮어쓰기·삭제해도 "q"의 값과 TTL 유지."""
    raise NotImplementedError


def test_t19_resize_and_rehash():
    """12개까지 16버킷, 13번째 신규 키 전에 32버킷. 기존 값·TTL 유지."""
    raise NotImplementedError


def test_t20_resize_allocation_failure():
    """확장 준비 할당 실패는 MemoryError, 새 키 미저장, 기존 상태 보존."""
    raise NotImplementedError


def test_s_t2_resize_and_overwrite():
    """덮어쓰기는 16버킷 유지, 13번째 신규 키는 32버킷."""
    raise NotImplementedError


def test_s_t3_allocation_failure_per_site():
    """노드·키·값·새 배열 할당 실패마다 MemoryError와 변경 전 상태 보존."""
    raise NotImplementedError


def test_s_t4_string_roundtrip():
    """충돌 키, 빈 키, 한글, NUL, 보충 문자, 단독 surrogate의 저장·조회 왕복."""
    raise NotImplementedError


def test_s_t13_surrogate_keys():
    """"\\U0001F600"과 "\\ud83d\\ude00"을 서로 다른 키·값으로 구별."""
    raise NotImplementedError


def test_b1_minimal_c_connection():
    """생성 → put → get → remove → 없음 확인 → 해제. get만 HT_NOT_FOUND."""
    raise NotImplementedError


def test_b2_abi_and_declarations():
    """abi_probe 출력과 ctypes 구조체 크기·오프셋, 모든 함수의 argtypes/restype 대조."""
    raise NotImplementedError


def test_b3_copy_and_free():
    """독립 복사본은 원본 변경·닫기와 무관. Python 복사 실패에도 C 버퍼 해제."""
    raise NotImplementedError


def test_b4_empty_buffers_and_output_failure():
    """0길이 NULL 주소 허용, 빈 snapshot은 NULL/0, 실패 출력은 빈 상태."""
    raise NotImplementedError


def test_b5_lifetime():
    """close() 두 번은 안전, 닫힌 뒤 데이터 메서드는 RuntimeError."""
    raise NotImplementedError


def test_b6_test_hooks():
    """실패 주입 0, N, -1과 dump의 물리 항목·n·m. 훅 누락은 실패로 알림."""
    raise NotImplementedError
