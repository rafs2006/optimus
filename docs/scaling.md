# Scaling optimus for huge servers and large fleets

This is the consolidated operator guide for running optimus at scale — whether
that means a handful of very large, very active Discord servers or a fleet of
thousands of guilds. It ties together the individual scaling levers (each
documented in depth elsewhere) into one place: what to turn, in what order, and
how to watch the result.

optimus is six independent services communicating over JetStream-backed NATS
(see [architecture.md](architecture.md)). Each scales independently, so the goal
is to find the bottleneck, scale only that, and confirm with metrics before
moving on. Almost nothing here is required for a small self-host — the defaults
are correct for a single replica.

For a worked, measured capacity study of one very large server (can optimus run a
single 800,000-member Discord guild?), including index-scaling, burst-absorption,
REST-budget and Postgres-growth numbers and a tuned deployment recipe, see
[capacity.md](capacity.md).

## At a glance

| Pressure | Symptom | Lever | Section |
| -------- | ------- | ----- | ------- |
| Many guilds (>2,500) | Discord rejects the gateway identify | Gateway sharding | [Sharding](#1-gateway-sharding) |
| High image volume | Detection in-flight pinned at max; throughput flat | More detection replicas | [Detection replicas](#2-detection-replicas-the-throughput-bottleneck) |
| Multiple replicas | Effective rate limits multiplied by replica count | Redis rate-limit backend | [Distributed rate limiting](#3-distributed-rate-limiting) |
| Detection backlog | In-flight queue deepens, latency rises | `detection_max_inflight` tuning | [In-flight tuning](#4-in-flight-concurrency) |
| Unbounded data growth | DB grows forever | Retention purge | [Retention](#5-retention) |
| DB connection exhaustion | Pool timeouts at high replica count | Pool sizing | [Connection pooling](#6-connection-pooling) |
| Need visibility | Flying blind under load | Scrape `/metrics`, alert on it | [Metrics and alerting](#7-metrics-and-alerting) |

## 1. Gateway sharding

The **gateway** holds the only Discord gateway connection and is the one
component whose load grows directly with guild count. Discord *requires* sharding
past ~2,500 guilds, and heavy single servers can saturate one connection well
before that.

Configuration-only via `OPTIMUS_SHARD_COUNT` (fleet-wide total, every replica
must agree) and `OPTIMUS_SHARD_IDS` (which shards this replica runs). Small
deployments should leave both unset and let hikari auto-negotiate a single
shard. The full mechanics — shard assignment, multi-replica splits, `/readyz`
shard checks, and `max_concurrency` identify pacing — are in
[sharding.md](sharding.md).

## 2. Detection replicas (the throughput bottleneck)

Detection is CPU-bound on image decode and is the component that limits
end-to-end throughput. The load harness (`python -m benchmarks.load`, see
[performance-notes.md](performance-notes.md)) characterizes one replica: a 2-vCPU
replica sustains ~7 images/sec single-flight and saturates at ~9–10 images/sec.

**Sizing rule of thumb: budget roughly `~3.5 images/sec per vCPU` per detection
replica.** To handle a target rate `R` images/sec on `C`-vCPU instances:

```
replicas ≈ ceil(R / (3.5 * C))
```

Detection is horizontally scalable: every replica is a competing pull consumer
on the same JetStream stream, so adding replicas adds throughput linearly until
some other resource (DB, Redis, Discord) becomes the limit. The
[`detection`](../src/optimus/services/detection/) service is stateless beyond its
in-memory hash index, which each replica rebuilds independently.

**How to know you need more:** the *Detection in-flight vs max* panel pins at the
configured ceiling and *Pipeline throughput* plateaus. That is the saturation
signature — add replicas, do not just raise `detection_max_inflight` (see below).

## 3. Distributed rate limiting

The default rate limiter (`OPTIMUS_RATELIMIT_BACKEND=memory`) uses per-process
token buckets. That is correct for a single replica, but with N replicas the
effective limit is multiplied by N because each process limits independently.

For multi-replica deployments set:

```bash
OPTIMUS_RATELIMIT_BACKEND=redis
```

This shares one bucket across all replicas via Redis, so effective limits do not
multiply with replica count. If Redis becomes unreachable the limiter falls back
to in-memory buckets (bounding load per replica during the outage) and increments
`optimus_ratelimit_redis_fallback_total`; the shared limit is temporarily
multiplied by replica count again until Redis recovers — strictly safer than
failing requests. Watch the *Ratelimit Redis fallbacks* panel and the
`OptimusRatelimitRedisFallback` alert. Details and the rationale are in
[performance-notes.md](performance-notes.md) (§ distributed rate limiting).

## 4. In-flight concurrency

`OPTIMUS_DETECTION_MAX_INFLIGHT` (default 10) bounds how many messages a
detection replica processes concurrently. Set it **near the replica's vCPU
count**. Because detection is CPU-bound, raising it past the core count does
*not* raise throughput — it only deepens the in-flight queue and adds per-image
latency. Excess buffering belongs in JetStream, not in replica memory, where it
would balloon RSS under a raid. Scale throughput with replicas (§2), not with a
deeper in-flight setting.

## 5. Retention

By default optimus keeps everything: `OPTIMUS_DETECTION_RETENTION_DAYS` is unset,
which disables the scheduler's `retention_purge` job. On a large deployment the
`detections` and appeal tables grow unbounded, so set a positive value to enable
bounded cleanup:

```bash
OPTIMUS_DETECTION_RETENTION_DAYS=90
OPTIMUS_RETENTION_BATCH_SIZE=1000          # rows per DELETE, keeps locks short
OPTIMUS_RETENTION_BATCH_PAUSE_SECONDS=0.5  # pause between batches on huge tables
```

The purge runs in the **scheduler** service on a daily cadence, deleting in
bounded batches so locks and transactions stay short even on huge tables. Purge
volume is observable via `optimus_scheduler_rows_affected_total{task="retention_purge"}`
(the *Retention purges* panel). This operator-level floor is independent of the
per-guild `retention_days` config consumed by the legacy retention job.

> Retention is also a privacy lever: it bounds how long detection metadata and
> appeals are stored. See [privacy-policy-template.md](privacy-policy-template.md).

## 6. Connection pooling

Each service opens a SQLAlchemy async pool: `OPTIMUS_DB_POOL_SIZE` (default 5)
plus `OPTIMUS_DB_MAX_OVERFLOW` (default 10) burst connections. The real cap on
the database is `replicas * (pool_size + max_overflow)`, so at large fleets this
is how you exhaust Postgres connection slots. Size the pool down per replica as
you scale replicas up, or raise Postgres `max_connections` (and front it with a
pooler such as PgBouncer) to match `total_replicas * per_replica_cap`. Postgres
operational guidance is in [operations.md](operations.md).

## 7. Metrics and alerting

Every service exposes `/metrics` (Prometheus text format), `/healthz`, and
`/readyz` on `OPTIMUS_HEALTH_PORT` (default 8080). **No collector ships with
this repo** — bring your own Prometheus, Grafana Agent, Datadog agent, or
anything else that speaks OpenMetrics, and point it at those endpoints. Scraping
every 15s is plenty; the endpoint is cheap to serve.

`/metrics` is unauthenticated by design — it assumes a scraper on a private
network — and it shares the health port. If that port is reachable from the
public internet, restrict the scrape path with your platform's network controls.

If all you need is a quick read on whether the bot is keeping up, `/stats` in
Discord reports the same pipeline counters to moderators, no scraper required.
See [moderator-guide.md](moderator-guide.md).

### Worth graphing

Every lever above has a metric that confirms the change worked:

| Question | Metric |
| -------- | ------ |
| Is the pipeline moving? | rate of `optimus_ingest_images_fetched_total` and the detection verdict counters |
| Is detection saturated? | detection in-flight gauge against `detection_max_inflight` |
| Are actions reaching Discord? | dispatch latency histogram, moderation circuit-breaker state gauge |
| Is moderation backing up? | `optimus_moderation_priority_queue_depth` |
| Are we losing work? | `optimus_gateway_images_dropped_total`, `optimus_ingest_images_rejected_total`, `optimus_ingest_rate_limited_total` |
| Is retention running? | the retention-purge counter ([operations.md](operations.md)) |

Metrics carry no `guild_id` label — cardinality would grow with every server the
bot joins — so all of these are deployment-wide, never per-server.

### Worth paging on

Starting points, not tuned thresholds. Use conservative `for:` windows so
deploys and traffic bursts do not flap the pager:

| Condition | Suggested rule |
| --------- | -------------- |
| A service is gone | `up == 0` for a service for >2m |
| A consumer stalled | service up but acking nothing while messages are in-flight, >10m |
| Actions not reaching Discord | moderation circuit breaker OPEN >5m |
| Moderation backing up | per-priority queue depth >100 sustained 10m |
| Rate limiting degraded | Redis ratelimit fallback active >5m |
| Losing work | reject/drop counters rising >1/s over 5m |

## Recommended scale-up order

1. **Shard the gateway** if guild count requires it (§1).
2. **Start scraping `/metrics`** (§7) so the next steps are measurable.
3. **Scale detection replicas** to your target image rate (§2), confirming with
   the in-flight and throughput metrics.
4. **Switch the ratelimit backend to Redis** once you run >1 replica of anything
   rate-limited (§3).
5. **Size pools and Postgres** for `replicas * per-replica-cap` (§6).
6. **Enable retention** to bound data growth (§5).
7. **Wire alerts** to your pager (§7).
