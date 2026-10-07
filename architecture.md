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
  rate limiter, cooldown, safe mode. Since #76 the rate-limit token is taken
  before the idempotency key, an empty bucket waits (bounded) rather than failing
  fast, and a `rate_limited` enforcement is retried with a fresh key before it
  falls to a moderator; see [`docs/architecture.md`](docs/architecture.md#resilience-controls-and-where-they-sit).

## Moderation audit contract

All enforcement and reversal reasons share the `Scam image` prefix and are built
in `services/moderation/reasons.py`. Reasons are bounded to 512 characters after
URL encoding; the timeout adapter carries the same reason contract as bans and
kicks. Automated actions identify available confidence, fingerprint, source, and
message evidence, not an invented detection ID: that row is created later.
Moderator actions can reference an existing detection. See
[PR #48](https://github.com/rafs2006/optimus/pull/48) and the
[moderator guide](docs/moderator-guide.md). Removed appeal commands are historical,
not a current member surface.

## Review cards, blocklist, and whitelist

Per-server blocklist entries are keyed by `(guild_id, hash_id)` since
[#62](https://github.com/rafs2006/optimus/pull/62) (migration `0011`). The
`hash_id` is derived from the image, so the same scam image can be listed on
several servers; adding an image a server already lists returns that server's
existing row and keeps its original attribution. Every read stays guild-scoped.

Since [#64](https://github.com/rafs2006/optimus/pull/64), a message gets one
review card, not one card per flagged image. Later images of the same message
update the open card, and the review buttons act on every image on it. A
successful decision folds the card to one handled line with no buttons; refusals
and failures leave it open. `/queue` counts cards, and
`/queue detection:<number>` reopens a card with fresh buttons. Migration `0012`
adds nullable `detections.review_message_id`, and `VerdictEvent` carries
optional `confirmed_by` and `review_card_id`. Grouping is held in memory per
process: exact in `simple` mode, but two images finishing at the same instant in
`distributed` mode can still produce two cards.

Since [#65](https://github.com/rafs2006/optimus/pull/65), a moderator's
**Confirm scam** or **Review as scam** settles the uploader's campaign whatever
the `action_policy`: it runs the existing campaign sweep once, closes that
uploader's other open cards in the sweep window (one `review.campaign_close`
audit row), and for the window afterwards deletes their reposts that match this
server's blocklist without posting new cards. Automatic `report_only` verdicts
still never sweep. The repost memory is in-process and resets on restart.

Since [#71](https://github.com/rafs2006/optimus/pull/71), the target resolver
tells an uploader who left the server (a 404 on the member lookup) apart from
one it cannot verify (403, 5xx, or any other failure). Under `delete_ban`, a
departed uploader is banned by user ID, on automatic verdicts and on Confirm;
a non-member holds no roles, so no hierarchy check is skipped. `delete_timeout`
and `delete_kick` fall back to delete-only for a departed uploader, and an
unverifiable one still gets delete-only. An already-banned result (40007)
counts as success; a ban limit (30035) or unknown user (10013) keeps the card
open with the reason. A card is posted already folded when the match came
from this server's own list with a hash, the decision was automatic, the
configured action ran without a downgrade, and every step succeeded; that
automatic action also settles the uploader's campaign the way Confirm does.
Global-only and near matches, safe mode, member reports, hashless matches,
hierarchy refusals, and downgraded or failed steps keep a full, open card.
`/queue detection:<number>` posts a folded card again for review or undo.

Since [#72](https://github.com/rafs2006/optimus/pull/72), the coordinator marks
an uploader settled as soon as enforcement runs (a fully auto-handled verdict or
a successful moderator confirm), before the card is posted. For
`SETTLED_WINDOW_SECONDS` (10 minutes) afterwards, any other hash match of that
uploader, including near matches and global-list matches that would otherwise
only ask a moderator, is deleted without a new card and counted on the settled
card. An open card that lands after the settlement (two posts checked at the
same moment) is re-checked once stamped: the post is deleted and the uploader's
open cards are closed under the system actor; a refused delete leaves the card
open. The ban still rests only on this server's own list, and member reports,
safe mode, hashless risk-scan matches, and other uploaders are unchanged. The
settled memory is in-process, bounded, and resets on restart. Each image logs a
`verdict_decided` line with its decision and reason.

Verdicts are serialised per uploader (`_uploader_locks`), so a burst across
channels settles once: the first image bans and posts the card, later images of
that post join it as items, and other posts take the follow-up path (delete
only, counted once per post). A detection the bot settled itself is stored as
`auto:<action>` (the mod-action audit keeps the plain action), and the campaign
closer skips those rows, so it never removes the bot's own cards. A card posted
again from the database reads `<action> (handled automatically)`, and so do
`/setup` replay cards and the dashboard. `detections.action_taken` can
therefore hold `auto:`-prefixed values (`String(32)`; the longest is
`auto:delete_timeout`, 19 characters): queries or exports that match exact
action strings should strip the prefix. Images that join the card store their
own plain `delete`, never the first image's cleanup notes, and a card line
drops `delete` when `delete_ban`/`delete_kick`/`delete_timeout` already says it.

Since [#69](https://github.com/rafs2006/optimus/pull/69), the whitelist is
visible and correctable through `/scamhash whitelist` and
`/scamhash unwhitelist`, and a scam call (Confirm scam, Review as scam,
`/scamhash add`) removes whitelist entries that cover the image, with a
`scamhash.unwhitelist` audit row per removal. `VerdictEvent.whitelist_removed`
carries the count onto the folded card. A whitelist match still wins over the
blocklist at scan time. Exports include a whitelist section for review only;
imports ignore it, so a shared file cannot exempt images on another server.

## Improvement plan — near term

### Current member-command boundary

`OPTIMUS_MEMBER_COMMANDS` narrows only permissionless commands, currently
`report` and `help`. Unset exposes both; unknown or moderator-command names
fail startup. Hidden commands are omitted from registration and rejected at
dispatch before entering a database scope, including interactions from cached
client command lists. The report context menu and moderator commands are not
controlled by this setting. See [#49](https://github.com/rafs2006/optimus/pull/49)
and the [operator guide](docs/running-optimus.md#narrowing-what-members-can-run).

The opt-out and appeal surfaces discussed in #49 are historical: #50 removed
appeals and #51 removed self-service scanning opt-out. This gate does not
restore either feature.

### Moderator review permissions

Since [#57](https://github.com/rafs2006/optimus/pull/57), each review action is
authorized by the Discord permission that matches its effect rather than by
Manage Server: **Ban uploader** and **Unban** require Ban Members; **Confirm
scam**, **False positive**, **Dismiss**, **Whitelist image**, and `/queue`
require Manage Messages. Administrator implies all of them; Manage Server
implies none of them. Confirm stays on Manage Messages because it applies the
server's standing `action_policy`, the same decision the automatic pipeline
enforces. Setup and settings commands remain on Manage Server and
`/delete_server_data` on Administrator. `mod_role` grants visibility of cards,
not permission to act. The permission mask is derived from the enum, and an
action missing from the permission map falls back to Manage Server, never to no
check. Since [#73](https://github.com/rafs2006/optimus/pull/73), every
`/scamhash` subcommand and the right-click **Review as scam** also require
Manage Messages, the same bar as Confirm scam, because moderators keep the
blocklist. As the one exception to "Manage Server implies none of them", those
two commands still accept Manage Server through `ALSO_ACCEPTED`; that fallback
is checked server-side only, since Discord's `default_member_permissions` hint
holds one permission set. `/config`, `/setup`, `/stats`, the per-button
permissions, and `/delete_server_data` are unchanged. See the
[moderator guide](docs/moderator-guide.md) and
[security audit](docs/security-audit.md).

### Proposed improvements

The seven items below are the plan against the head of `main` as of
`2026-08-27`. They are recorded here (rather than only in a review issue) so
the plan is versioned with the code.

1. **Measure the OCR/QR lane.** Mirror `benchmarks/` for the risk-scan lane
   (defanged URLs, lookalike AI-company domains, credential-harvest wording,
   wallet-connect prompts, QR-payload phishing). Commit a
   `docs/eval/ocr-risk-report.{md,json}` and gate CI on a precision floor.

   Now more urgent, and better specified, after the upscale/budget fix.
   `benchmarks/` is today entirely a hash-lane harness, and the OCR budget
   default (8s) was set from *one* measured worst case — a 1148px multi-panel
   collage at 6.4s — not from a distribution. The lane emits
   `optimus_ocr_duration_seconds`, `optimus_ocr_variants_completed`, and
   `optimus_ocr_outcome_total`, which covers production; this item is the
   offline half, so a preprocessing change can be judged before it ships.
   Concretely: a multi-panel corpus generator beside `corpus.py` that tiles
   3-6 synthetic UI panels at geometries spanning the range where the upscale
   gate matters (including 1148px), scored through `riskscan.scan` for
   payload-domain recovery and risk-level distribution, with per-case timing so
   the budget can be re-derived from a distribution.

   Two constraints worth settling first. The corpus should be **generated, not
   a real screenshot** — every fixture in the repo is synthetic, and a real
   sample means committing third-party content with real handles to a public
   repo; a generated collage at the same geometry is the same forcing function.
   And unlike the current benchmarks this one needs the Tesseract binary, so it
   stays out of CI, as `benchmarks/` already does. When it lands, replace the
   OCR entry in Known Limits in [docs/capacity.md](docs/capacity.md) with the
   measured result.
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

- Global trust lane (#34) is review-only + confirm-as-vote today, except that
  since #72 a global match from an uploader settled on this server in the last
  10 minutes is deleted (never banned) without a card. Does it
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
<!-- decision:rafs2006/optimus#55 --> Applied: review lifecycle grows a `dismissed` no-op terminal state alongside confirm/false-positive (no hash/whitelist/ban side-effects; audits `review.dismiss`), and a new MANAGE_GUILD-gated `/queue` command surface lists open cards via `DetectionRepository.list_open` (excludes unposted and already-actioned rows, oldest-first, capped at 25) so a neglected server can drain the backlog — from #55 _(Since #57, `/queue` requires MANAGE_MESSAGES, the same bar as the review buttons.)_

<!-- decision:rafs2006/optimus#48 --> Applied: centralize evidence-bearing, encoded-length-bounded audit reasons across moderation actions - from #48.

<!-- decision:rafs2006/optimus#49 --> Applied: gate permissionless command registration and dispatch while retaining subsequent removal of appeals and self-service opt-out - from #49.

<!-- decision:rafs2006/optimus#57 --> Applied: review actions are authorized per action by Ban Members or Manage Messages instead of Manage Server, with fail-closed handling for unmapped actions - from #57.

<!-- decision:rafs2006/optimus#62 --> Applied: per-server blocklist rows are keyed by `(guild_id, hash_id)` so one image can be listed on several servers - from #62.

<!-- decision:rafs2006/optimus#64 --> Applied: review cards are per message, decided cards fold in place, and card identity is stored on detections and carried on verdict events - from #64.

<!-- decision:rafs2006/optimus#65 --> Applied: a moderator scam confirmation runs the campaign sweep and closes the uploader's other open cards regardless of action policy, while automatic report-only verdicts never sweep - from #65.

<!-- decision:rafs2006/optimus#69 --> Applied: whitelist entries are listable and removable, a moderator scam call lifts covering entries, and exported whitelists are never imported - from #69.

<!-- decision:rafs2006/optimus#71 --> Applied: an uploader who already left is banned by user ID under `delete_ban`, and a match the bot fully handled from this server's own list is posted as a folded card and settles the uploader's campaign like Confirm - from #71.

<!-- decision:rafs2006/optimus#72 --> Applied: an uploader settled by auto-handling or a confirm has their other hash matches, including near and global-list matches, deleted without a moderator for 10 minutes, with late open cards closed, while bans still rest only on this server's list - from #72.

<!-- decision:rafs2006/optimus#73 --> Applied: `/scamhash` and Review as scam are gated on Manage Messages like the review buttons, with Manage Server kept as a server-side fallback for those two commands - from #73.

<!-- decision:rafs2006/optimus#75 --> Applied: verdicts are serialised per uploader so a burst settles to one card and one ban, and detections the bot settled itself are stored as `auto:<action>` so the campaign closer never removes the bot's own cards - from #75.

<!-- decision:rafs2006/optimus#76 --> Applied: enforcement takes the per-server rate token before the idempotency key, waits a bounded time for it, honours Discord's `retry_after`, and retries a `rate_limited` enforcement with a fresh key before leaving it for a moderator - from #76.
