# R1c Task 3 — dispatcher 退纯转发 + 生命周期→状态映射 + 单测

Branch: `fix/r1c-route-ownership`
Commit: `8cdcfec7b500bda4dda9f5d921dc032b740f2914`
Message: `R1c §3: dispatcher 退纯转发（删 resolve_by_path + 强制 header + retired→410）`

## Changes per file

### `services/libs/apihub-core/src/apihub_core/errors.py`
- Added `API_RETIRED = 30005` to `ErrorCode` (after `API_DOWN = 30004`).
- Added `ErrorCode.API_RETIRED: 410` to `_HTTP_STATUS_MAP`.
- Missing-header case reuses existing `INVALID_PARAMS = 10001` (→400); no new code added for it, per brief.

### `services/services/dispatcher/src/dispatcher/resolver.py`
- Rewrote `resolve_by_header`: SQL now `WHERE id = $1 AND status IN ('published', 'deprecated')`; on miss, `fetchval status` inside the same `meta_db_session` block → `retired` raises `API_RETIRED` (410), else `API_NOT_PUBLISHED` (404). Redis cache path unchanged.
- Deleted `resolve_by_path` (verified via codegraph: sole caller was `dispatch`'s else-branch, no tests).
- Deleted `_match_path` (codegraph: sole caller was `resolve_by_path`).
- Removed now-unused `from apihub_core.tenant import require_tenant` (ruff would have flagged F401).
- Updated module docstring to "唯一入口".
- **Kept `_extract_path_params`** — already dead (codegraph: zero callers) but not in the brief's delete list and it has dedicated unit tests; left untouched to stay in scope.

### `services/services/dispatcher/src/dispatcher/routes.py`
- Removed `resolve_by_path` from the `from dispatcher.resolver import ...` line.
- Rewrote `dispatch` handler: requires `X-API-Version-Id` (missing → `INVALID_PARAMS` 400 via `ApiError`); removed `method`/`rest` computation and the path-resolution `else` branch. Visibility check, backend_type routing, sandbox rewrite — all unchanged.
- Updated module docstring.

### `services/services/dispatcher/tests/test_resolver.py` (existing — adapted)
- Removed `TestPathMatch` class (9 parametrized cases) — it tested the deleted `_match_path`.
- Kept `TestExtractPathParams` (4 cases) — `_extract_path_params` is retained.
- Removed now-unused `import pytest`; updated docstring noting the removal.

### `services/services/dispatcher/tests/test_resolver_lifecycle.py` (new)
- 5 tests, per brief: published OK / deprecated OK / retired→410 / dispatch missing header→400 / `resolve_by_path` removed.

## TDD RED → GREEN

**RED** (after writing the new test, before source edits):
```
.venv/bin/python -m pytest services/services/dispatcher/tests/test_resolver_lifecycle.py -v
→ 5 failed
  test_resolve_published_ok            TypeError: 'coroutine' object does not support async CM
  test_resolve_deprecated_ok           (same)
  test_resolve_retired_returns_410     (same; also API_RETIRED absent)
  test_dispatch_missing_header_returns_400   assert 401 == 400
  test_resolve_by_path_removed         assert not True (resolve_by_path still present)
```

**GREEN** (after source edits + test-harness fixes):
```
.venv/bin/python -m pytest services/services/dispatcher/tests/ -v
→ 51 passed in 0.79s
```

Cross-service regression sweep (shared `errors.py` changed):
```
.venv/bin/python -m pytest services/libs/apihub-core/tests/ services/services/api-registry/tests/ \
  services/services/dispatcher/tests/ --ignore=services/libs/apihub-core/tests/test_db_rls.py -q
→ 165 passed, 10 warnings in 0.53s
```
(`test_db_rls.py` ignored — pre-existing unregistered `integration` marker; verified it errors identically on stashed/original code.)

Lint:
```
ruff check services/libs/apihub-core/src/apihub_core/errors.py services/services/dispatcher/
→ All checks passed!
```

## Deviations from the brief's verbatim test code

The brief's `test_resolver_lifecycle.py` had three test-harness bugs that would have left 3 of the 5 tests unpassable regardless of any source change. The brief's *assertions and intent are unchanged*; only mock plumbing was fixed:

1. **`_meta_session._factory` declared `async def`** → calling it returned a coroutine, so `async with db.meta_db_session()` raised `TypeError`. Fixed to `def _factory` so the call returns the async-CM instance. (`meta_db_session` is a `@asynccontextmanager`, i.e. a callable returning an async CM.)
2. **Same bug duplicated inline** in `test_resolve_retired_returns_410` (its own local `_factory`). Fixed identically.
3. **`_no_cache` autouse fixture only stubbed `redis.t_get`** → on a cache miss the success path calls `redis.t_set(...)`, which raised `RuntimeError: Redis not initialized`. Added a `_noop_set` stub for `redis.t_set`.
4. **`test_dispatch_missing_header_returns_400` sent no headers** → the tenant middleware returns 401 for a missing API key *before* dispatch runs (never reaches `authenticate_request`). Added `headers={"X-API-Key": "ak_test_a_demo001"}` (same header the existing `test_jobs.py` uses) so the request clears auth and reaches dispatch, which then returns 400 for the missing `X-API-Version-Id`.

## Existing tests adapted

- `test_resolver.py::TestPathMatch` (9 parametrized cases) — **removed**: tested `_match_path`, which was deleted along with `resolve_by_path`.
- `test_resolver.py::TestExtractPathParams` (4 cases) — **kept unchanged**: `_extract_path_params` was not in the brief's delete list.
- All other existing dispatcher tests (`test_jobs.py`, `test_masking.py`, `test_visibility.py`, `test_event.py`) — **unchanged, all pass**; none referenced `resolve_by_path` or relied on headerless `/dispatch` (they target `/v1/jobs` or pure functions).

## Self-review

- `fetchval` status lookup sits inside the same `async with db.meta_db_session() as conn` block (same connection, one transaction) — consistent with the brief.
- `method = request.method` was removed from `dispatch` (only `resolve_by_path` had consumed it); confirmed no other reference in the handler. The forwarder reads method off `request` itself.
- `_extract_path_params` is now confirmed-dead code (codegraph: zero callers anywhere). Kept deliberately (out of brief scope, has tests). Flagging here for a future cleanup.
- Lifecycle status strings (`published` / `deprecated` / `retired`) cross-checked against `api-registry/routes.py` retire/deprecate SQL — consistent.
- The header name `X-API-Version-Id` matches what Tasks 1+2 configured APISIX to inject.

## Concerns

- **None blocking.** Two minor notes:
  1. The brief's test file shipped with mock-plumbing bugs (documented above); fixed in place rather than reported as a blocker, since the assertions themselves were correct.
  2. `_extract_path_params` is dead code (no callers). Left in scope; recommend a follow-up cleanup task.
- Pre-existing `test_db_rls.py` collection error (`integration` marker unregistered) is unrelated to this task — verified it reproduces on the original tree.

## Final-review fixes

Two focused fixes from the R1c final review (commit on `fix/r1c-route-ownership`).

### I1 — cache staleness defeats retire→410 (Important)

**Problem:** `resolve_by_header` cached the snapshot in Redis (`snapshot:{version_id}`, TTL 300s) and returned it directly on a cache hit. The cached `ApiVersionSnapshot` has no `status` field, so after a version went deprecated→retired, the stale snapshot was served (200) for up to 5 min instead of 410.

**Fix** (`services/services/dispatcher/src/dispatcher/resolver.py`): on a cache HIT, before returning, run a cheap PK-indexed `SELECT status FROM api_version WHERE id=$1` inside a `meta_db_session`. `retired` → `t_delete(cache_key)` + `ApiError(API_RETIRED, 410)`; any status not in `('published','deprecated')` → `t_delete` + `ApiError(API_NOT_PUBLISHED, 404)`. The existing cache-MISS path is unchanged. Self-contained in dispatcher — no Kafka/cross-service coupling.

**Redis delete fn:** confirmed `redis.t_delete(key)` exists in `services/libs/apihub-core/src/apihub_core/redis.py` (L65) and is used directly (tenant-prefixed delete). No rename needed.

**TDD:**
- RED — two new tests added to `test_resolver_lifecycle.py` (`test_resolve_cache_hit_stale_retired_returns_410`, `test_resolve_cache_hit_stale_other_status_returns_404`):
  ```
  .venv/bin/python -m pytest services/services/dispatcher/tests/test_resolver_lifecycle.py -q
  → 2 failed, 5 passed (Failed: DID NOT RAISE ApiError)
  ```
- GREEN — after the resolver fix:
  ```
  .venv/bin/python -m pytest services/services/dispatcher/tests/ -q --ignore=services/services/dispatcher/tests/test_db_rls.py
  → 53 passed in 0.13s
  ```
- Lint: `ruff check resolver.py test_resolver_lifecycle.py` → All checks passed!

### I2 — smoke script bypasses APISIX (Important)

**Problem:** `scripts/smoke/k8s-links.py::link1_sync` POSTed to dispatcher `/dispatch/...` with only `X-API-Key`, no `X-API-Version-Id` → would 400 post-R1c.

**Option chosen:** added the header inline. `link1_sync` and `link5_crossns` exercise different paths (direct port-forward vs through the APISIX gateway), so both are kept; removing `link1_sync` would lose the direct-dispatch coverage. Reused the existing module constant `SMOKE_VER_ID = "ver_smoke_sync_v1"` (the version `setup()` upserts and that L2/L3 reference) rather than introducing the brief's `smoke-ver-1` placeholder — the header must match a real `api_version.id` or resolve fails.

**Change** (one line): `headers={"X-API-Key": ADMIN_KEY, "X-API-Version-Id": SMOKE_VER_ID}`.
