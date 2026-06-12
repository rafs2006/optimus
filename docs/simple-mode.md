# Simple mode

Optimus runs in one of two modes, selected by `OPTIMUS_MODE`:

- **`simple`** (the default) — the whole bot in a single process with **zero
  external services**. No NATS, no Redis, no Postgres.
- **`distributed`** — the production topology described in
  [architecture.md](architecture.md): six services wired over NATS JetStream,
  with Postgres and Redis. Unchanged by simple mode.

Simple mode exists so you can run, develop against, and demo Optimus with nothing
but a Discord bot token.

## Running it

```sh
export OPTIMUS_DISCORD_TOKEN=your-bot-token
python -m optimus          # or: optimus
```

That is the entire setup. If `OPTIMUS_DISCORD_TOKEN` is unset, the process exits
with a clear error. `python -m optimus` only runs simple mode; for distributed
mode launch the per-service entrypoints (`python -m optimus.services.<name>`).

## What it composes

The six service runtimes are the *same code* as distributed mode — only the
wiring differs ([`app/simple.py`](../src/optimus/app/simple.py)):

| Distributed                          | Simple                                            |
| ------------------------------------ | ------------------------------------------------- |
| NATS JetStream (`EventBus`)          | in-process asyncio-queue bus (`InProcessBus`)     |
| Postgres                             | a SQLite file (alembic `upgrade head` on startup) |
| Redis rate limiter                   | in-memory token bucket                            |
| Redis idempotency / dedup guard      | in-memory key/value store                         |
| one health/metrics server per service| one shared `/readyz` + `/metrics` server          |

The detection core and every service's logic are untouched; the in-process bus
([`bus/inprocess.py`](../src/optimus/bus/inprocess.py)) mirrors the three
JetStream behaviours the pipeline relies on — publish dedup (by `msg_id`),
bounded in-flight (`detection_max_inflight`), and redelivery-then-drop up to
`max_deliver`.

The cross-guild swarm correlator is disabled in simple mode: it needs a real
Redis (`EVAL`), and a single-process bot has no fleet-wide signal to correlate.

## Durability trade-off

The in-process bus keeps queued-but-unprocessed messages in memory only, so a
restart in simple mode loses anything still in flight. SQLite still persists every
durable record (guild policy, registered hashes, detections). Run
`OPTIMUS_MODE=distributed` when at-least-once delivery across restarts matters.
