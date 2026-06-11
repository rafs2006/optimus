# Performance & async-correctness notes

Findings from the Cycle 6 performance/async pass. Each entry is either **fixed**,
**documented** (real but deferred), or **ruled out** (investigated, no change).

## Fixed

### Blocking decode + hashing on the detection event loop
- **Location:** `src/optimus/services/detection/worker.py` `DetectionWorker.handle`
- **Issue:** `decode()` (a synchronous `subprocess.run` that blocks up to the
  decode wall timeout, default 5s) and `all_frame_hashes()` (numpy/Python
  perceptual hashing over up to `max_frames` frames) ran directly on the event
  loop. The detection bus consumer (`bus.consume`) processes messages
  sequentially and shares the loop with the health server and NATS heartbeats,
  so a single slow/large image stalled all detection progress and could delay
  liveness/readiness responses.
- **Fix:** the decode+hash block is offloaded with `asyncio.to_thread` via a new
  `DetectionWorker._decode_and_hash` helper. `decode()` stays synchronous (it is
  unit-tested that way and used at one async call site), so the change is
  surgical. Covered by `test_worker_offloads_decode_and_hash_to_thread`.

## Documented (real, deferred — too invasive for a single-fix pass)

### `IndexManager` per-guild index cache is never evicted
- **Location:** `src/optimus/services/detection/index.py` `IndexManager._guilds`
- **Observation:** `_guilds: dict[int, HashIndex]` retains one BK-tree index per
  guild ever queried, never evicting. Growth is bounded by the number of guilds
  the bot is in (not per-message/per-user churn), and each index is expensive to
  rebuild (a Postgres query per guild), so it is a legitimate hot-path cache —
  unlike the per-user rate-limiter map that motivated `evict_idle`.
- **Recommendation:** for very large fleets, add an LRU bound (e.g. cap entries,
  evict least-recently-used) so a bot in tens of thousands of guilds cannot hold
  every index resident simultaneously. Deferred because it changes rebuild-cost
  characteristics and needs a sizing decision + eviction tests.

### In-memory rate-limiter fallback is not swept in the ingest service
- **Location:** `src/optimus/services/ingest/service.py` `build_worker`
  (constructs `InMemoryRateLimiter()` when Redis is unavailable)
- **Observation:** `InMemoryRateLimiter` exposes `evict_idle` (added in a prior
  cycle) to bound its `_buckets` map, but nothing calls it for the ingest
  fallback. Keys are `guild:{id}`, so growth is bounded by guild count rather
  than user churn, and this path is only reached in a degraded (no-Redis) mode.
- **Recommendation:** when running with the in-memory fallback long-term, run a
  periodic `evict_idle` sweep (e.g. a small background loop or piggy-backed on
  the scheduler). Deferred because it only matters in a degraded mode and needs
  a sweep cadence decision.

### Unused `_use_embedding` flag on the detection worker
- **Location:** `src/optimus/services/detection/worker.py` (`use_embedding` /
  `_use_embedding`) and `src/optimus/hashing/embedding.py`
- **Observation:** the worker stores `_use_embedding` but never consults it;
  `embedding.embed()` (blocking ONNX inference) is not called from any async
  path. So embedding is not currently a blocking-on-loop risk. If embedding
  confirmation is wired into `handle` later, `embed()` MUST be offloaded the
  same way as decode/hash (it runs synchronous `session.run`).

## Ruled out (investigated, no change needed)

- **`hashing/bktree.py` query/add:** sub-linear (triangle-inequality pruning),
  pure-Python int Hamming ops, only a handful of lookups per frame. Not a
  meaningful loop-blocker relative to decode/hash.
- **`hashing/perceptual.py` (called inside the offloaded helper):** CPU-bound
  but now runs off-loop via the `_decode_and_hash` offload above.
- **`ingest/fetcher.py`:** fully async aiohttp with a total `ClientTimeout`,
  bounded chunked streaming with a hard size cap, and per-hop `ClientSession`
  created/closed via `async with` (intentional for SSRF re-pinning). No session
  leak, no missing timeout.
- **`services/detection/swarm.py`, `core/ratelimit.py` (Redis), `moderation/cooldown.py`:**
  single atomic Redis round trips (Lua `eval` / `SET NX EX`); no N+1 round trips,
  no in-memory growth. TTLs bound Redis-side state.
- **`core/guild_config.py` `GuildConfigCache`:** Redis cache with TTL + DB
  fallback; no per-message recompute, no unbounded process-local map.
- **`services/gateway/extract.py`:** URL regex compiled once at import; per-message
  dedup set is request-scoped and bounded by the message. No O(n^2).
- **`core/config.py` `get_settings`, `i18n/catalog.py`:** `lru_cache`d — parsed
  once, not per message.
- **Fire-and-forget tasks:** every `asyncio.create_task` in the service
  entrypoints (scheduler, moderation, ingest, detection) keeps a strong
  reference (held in a list and awaited via `gather`, or tracked in
  `GatewayService._inflight` and drained on shutdown). No GC'd-task risk.
- **`gather` usage:** `return_exceptions` is used only where appropriate
  (`GatewayService.drain` over best-effort in-flight publishes). The supervisor
  `gather`s in `_amain` intentionally propagate so a dead consumer triggers
  shutdown.
