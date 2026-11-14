This repository is a student team project: a mini Redis. It is a key-value cache server with a hand-written C hash table (djb2, chaining, resize at load factor 0.75) called from Python through `ctypes`, a local Flask HTTP API (`POST /set`, `/get`, `/delete`), TTL expiry, and a single worker thread behind a queue. Snapshot persistence and recovery are optional. The team writes design documents under `docs/`, and an AI implements code from them. A vague or wrong document therefore turns directly into wrong code.

The assignment puts a working result first: required work is the hash table, the HTTP API, TTL handling, a demo that caches an origin API response (D1, D2), and a benchmark with and without the cache (D3).

Reference documents for consistency checks:

- `docs/planning.md`: shared contracts, file ownership, the test-case index, and the integration order.
- `docs/c-hashtable-and-bindings.md`, `docs/store-and-snapshots.md`, `docs/http-and-runtime.md`: per-area contracts and test cases.
- `docs/issues.md`: work items MR-00 to MR-10 and their completion checklists.
- `docs/overview.md`: a short summary.

Additional checks:

- A contract stated in more than one document must not differ: function signatures, C status codes, HTTP status and error codes, file lists and owners.
- Each test-case ID (T1 to T21, S-T1 to S-T14, H1 to H12, B1 to B6, R1 to R6, D1 to D3) is defined in exactly one document. Ranges cited elsewhere must match the index in `docs/planning.md`.
- Required work must not depend on optional work (snapshot, recovery, final save on shutdown).

Code checks:

- Check that the implementation matches its design document: C function prototypes and structs in `docs/c-hashtable-and-bindings.md`, Store call order and TTL rules in `docs/store-and-snapshots.md`, HTTP responses and worker lifecycle in `docs/http-and-runtime.md`.
- Report defects: wrong behavior, missing edge cases (empty key or value, NUL bytes, TTL boundaries, hash collisions, resize), unhandled errors, memory that is not freed, and behavior changes without a test.
- Do not ask for defensive code or tests for states the design makes unreachable. Only the worker thread touches the table, and the HTTP layer validates input before submitting.
- Skip style preferences that the surrounding code already settles.

Priorities: for documents, rank content that would make the implementing AI guess or build the wrong thing (missing inputs or outputs, undefined edge cases, documents that contradict each other) above everything else. For code, rank defects and deviations from the design documents above style.

For the comment labels, use `**원칙:**`, `**이유:**`, and `**수정 제안:**`.
