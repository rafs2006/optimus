# Architecture & improvement plan

> A one-page map to the running system, plus the near-term plan. For depth on
> the six-service topology, the event bus, the decode sandbox, and every
> resilience control, see [`docs/architecture.md`](docs/architecture.md);
> everything here links out to it and never re-states it.

## What Optimus is (one paragraph)

Optimus is a Discord moderation bot that removes scam/phishing images. Every
image goes through a four-hash **perceptual ensemble** (aHash, dHash, pHash,
wHash), matched against per-guild + optional global hash indexes via
**multi-index hashing (MIH)**, with an **OCR + QR risk-scan** lane as a
never-seen-before fallback and a **swarm-correlation** lane that escalates a
verdict when the same phash re-appears across guilds. The pipeline is
**fail-closed** — anything the decoder can't safely process is a non-decision,
never an action.

## Two modes, same service code

| Aspect                     | `simple` (default)                                          | `distributed`                                                                 |
| -------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Processes                  | one                                                         | six (`gateway`, `ingest`, `detection`, `moderation`, `interactions`, `scheduler`) |
| Bus                        | in-process asyncio-queue bus (`bus/inprocess.py`)           | NATS JetStream (`bus/nats.py`, stream `OPTIMUS_EVENTS`)                        |
| Durable state              | SQLite file (alembic `upgrade head` on start)               | PostgreSQL                                                                     |
| Ephemeral state / limits   | in-memory token bucket + kv                                 | Redis                                                                          |
| Health / metrics           | one shared `/readyz` + `/metrics`                           | one per service                                                                |
| When to pick it            | self-host, dev, demo — nothing but a bot token              | fleets past the single-gateway guild ceiling; horizontal scale                 |

Everything else — the detection code, the SSRF-hardened fetcher, the decode
sandbox, the versioned event contracts (`events.<name>.v1`), the at-least-once +
idempotency guarantees — is unchanged between modes.
[`docs/simple-mode.md`](docs/simple-mode.md) has the full delta.

## The six services in one diagram

```
Discord ──► gateway ──► message_image.v1 ──► ingest ──► image_fetched.v1 ──► detection
                                                                                │
                                     control.index_invalidate.v1 ◄── scheduler │
                                                                                ▼
                                                                        verdict.v1 / swarm_alert.v1
                                                                                │
                                                                                ▼
                                                       moderation ──► Discord REST (delete/timeout/ban)
                                                                                │
                                                                                ▼
                                                                        action_result.v1

interactions ◄──► Discord (slash commands, review buttons; no bus consumption)
```

Authoritative version with tables, subjects, and stream config lives at
[`docs/architecture.md`](docs/architecture.md).

## Resilience posture (unchanged; recorded here so it isn't lost)

- **Versioned contracts** on a bounded JetStream stream (`RetentionPolicy.LIMITS`,
  `DiscardPolicy.OLD`, 1 M msg / 1 GiB cap); malformed payloads are dropped as
  poison and counted, not retried.
- **Fail-closed safety**: any image the sandboxed decoder can't handle within
  its CPU / memory / pixel / frame / wall-time limits yields a `NON_DECISION`.
- **At-least-once + idempotency**: Redis-backed per-attachment idempotency keys
  make redelivery safe.
- **Discord-side controls** live in `moderation`: circuit breaker, per-guild
  rate limiter, cooldown, safe mode.

## Improvement plan — near term

The seven items below are the plan against the head of `main` as of
`2026-08-27`. They are recorded here (rather than only in a review issue) so
the plan is versioned with the code.

1. **Measure the OCR/QR lane.** Mirror `benchmarks/` for the risk-scan lane
   (defanged URLs, lookalike AI-company domains, credential-harvest wording,
   wallet-connect prompts, QR-payload phishing). Commit a
   `docs/eval/ocr-risk-report.{md,json}` and gate CI on a precision floor.
2. **Publish `uvx optimus` for real.** Add a tag-triggered release job
   (`.github/workflows/release.yml`) that runs `uv build` + trusted-publisher
   OIDC to PyPI, and pushes a GHCR image from the existing `Dockerfile`. The
   README already advertises `uvx optimus`; today it depends on a checkout.
3. **Re-enable Issues, add issue templates, add a `ROADMAP.md`.** Issues are
   currently disabled on the repo, which blocks bug reports from the mod-team
   audience the recent docs work is aimed at. Ship
   `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.yml` (mode, version,
   preset, minimal repro) at the same time.
4. **Add `SECURITY.md` at the root** and enable GitHub Security Advisories.
   `docs/security-audit.md` exists but is not discoverable from GitHub's
   security tab. For a bot that holds `Ban Members` / `Moderate Members`, this
   is table stakes.
5. **Detection telemetry**: export a Prometheus histogram of the *actual*
   per-verdict ensemble score, labelled by preset, from
   `src/optimus/services/detection/worker.py`. The synthetic eval proves the
   preset is zero-FP on the corpus; the histogram proves it on real traffic.
6. **`/scamhash cluster` view**: group blocked hashes by ensemble distance so
   mods can see and prune redundant entries. Purely additive; no policy change.
7. **Pick a hosted-tier stance and put it in the README.** Simple mode +
   Docker is a great self-host story; the dashboard (#36) and global trust lane
   (#34) point at a hosted control plane. Either commit to it (and prioritise
   #5–#6 as its telemetry / mod tooling) or say "community-first, no hosted"
   so contributors don't have to guess.

## Open questions

- Global trust lane (#34) is review-only + confirm-as-vote today. Does it
  eventually auto-block after N confirmations from N distinct guilds? If yes,
  it changes the fail-closed posture in `docs/architecture.md` and needs its
  own section here.
- Default preset on join is `strict`. Balanced gives 0.979 recall at zero FP on
  the eval corpus (see `docs/eval/detection-eval-report.md`); worth revisiting
  once the OCR/QR lane is measured (item 1).
- Is `simple` mode a permanent supported path or a demo? If permanent, it
  deserves its own capacity note in `docs/capacity.md` (currently focused on
  distributed).

## Where to look in the code

- Event contracts: [`src/optimus/contracts/events.py`](src/optimus/contracts/events.py)
- Bus (JetStream / in-process): [`src/optimus/bus/`](src/optimus/bus/)
- Hashing (ensemble, MIH, decoder sandbox, OCR/QR): [`src/optimus/hashing/`](src/optimus/hashing/)
- Services: [`src/optimus/services/`](src/optimus/services/)
- Core resilience (circuit, ratelimit, idempotency, readiness): [`src/optimus/core/`](src/optimus/core/)
- Detection quality harness: [`benchmarks/`](benchmarks/) → [`docs/detection-eval.md`](docs/detection-eval.md)
