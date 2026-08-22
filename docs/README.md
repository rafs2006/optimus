# Optimus docs

**You don't need any of this to run the bot.** A bot token and one command
(`uvx optimus` or `docker run`) is the whole story — see the
[README quickstart](../README.md#quickstart).

Everything else is here, grouped by who reads it. Start with the row that
describes you.

| If you are… | Read |
| --- | --- |
| a **member** of a server that runs the bot | [for-members.md](for-members.md) |
| a **moderator** deciding on review cards | [moderator-guide.md](moderator-guide.md) |
| the person **running** the bot | [running-optimus.md](running-optimus.md) |
| **working on** the bot | [../CONTRIBUTING.md](../CONTRIBUTING.md) |

## For members

- [for-members.md](for-members.md) — plain language, no commands assumed: what
  the bot looks at, what it keeps and does not keep, what happens if you are
  flagged by mistake, and how to report a scam. Written to be pinned in a
  server.

## For moderators

- [moderator-guide.md](moderator-guide.md) — the day-to-day guide: first-time
  setup, the shared review channel, how to judge a card in a few seconds,
  scammer vs. stolen account, what every button actually does, and the full
  command and settings reference.
- [dashboard.md](dashboard.md) — the optional read-only web view: scan activity,
  detections, and audit logs in a browser, behind Discord login. Moderators get
  their own server's pages automatically when the host enables it.

## For whoever runs it

- [running-optimus.md](running-optimus.md) — start here: the shape of a live
  single-process deployment, the three ways to see what it is doing, how to read
  the pipeline-load numbers, which knobs trade cost against coverage, and a
  triage table for the usual "the bot is broken" reports.
- [simple-mode.md](simple-mode.md) — how the default single-process mode
  composes the whole bot with zero external services, and the one durability
  trade-off.
- [operations.md](operations.md) — Postgres operations: retention, connection
  pooling, the pgbouncer/asyncpg caveat, and backups.

### Running at scale (distributed mode)

Only needed once one process is no longer enough.

- [scaling.md](scaling.md) — the consolidated operator guide: what to scale, in
  what order, how to confirm it worked, and what to graph and page on.
- [capacity.md](capacity.md) — a measured capacity study (can one 800k-member
  server run on Optimus?) with the throughput baseline and a tuned recipe.
- [sharding.md](sharding.md) — gateway sharding mechanics for large fleets.
- [performance-notes.md](performance-notes.md) — the throughput baseline and the
  scale-hardening internals (distributed rate limiting, idempotency &
  back-pressure, payload hardening) behind the levers in scaling.md.

## Design and internals

- [architecture.md](architecture.md) — the system design: message flow, the
  detection pipeline, where state lives, and the resilience controls.
- [detection-eval.md](detection-eval.md) — how detection quality is measured; the
  offline benchmark and what the numbers mean. Headline results live in
  [eval/baseline.md](eval/baseline.md).
- [security-audit.md](security-audit.md) — the security audit record: findings,
  what was verified clean, and low-severity observations.

## Compliance

- [privacy-policy-template.md](privacy-policy-template.md) — a privacy-policy
  template for Discord bot verification.
