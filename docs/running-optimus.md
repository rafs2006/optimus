# Running Optimus

The operator's page: how a live deployment is actually shaped, what you can see
from the outside, how to tell whether the bot is keeping up, and which knobs
trade cost against coverage.

This describes the **default single-process deployment** — one container, simple
mode, no external services. If you are running the six-service topology instead,
[scaling.md](scaling.md) and [architecture.md](architecture.md) are your pages;
most of what follows still applies.

## The shape of it

One process. No NATS, no Redis, no Postgres — the whole pipeline is wired
in-process and durable state goes to a SQLite file. See
[simple-mode.md](simple-mode.md) for the substitution table.

What that means in practice:

- **The SQLite file must live on a mounted volume.** If your platform gives the
  container an ephemeral filesystem, every restart starts from an empty
  blocklist. The `/stats` boot counter exists specifically to catch this: if the
  boot number keeps resetting to 1, the database is not persisting.
- **Migrations run on startup** (`alembic upgrade head`). No separate deploy
  step, no migration job to forget.
- **A restart drops in-flight work.** Queued-but-unprocessed images live in
  memory. Durable records — hashes, config, detections — survive. Anything mid-
  pipeline at the moment of restart is simply lost, and the next scam repost
  catches it anyway.
- **The cross-guild swarm correlator is off.** It needs a real Redis, so the
  review card's *Swarm* field never appears in this mode. A campaign hitting
  many servers at once looks like unrelated single detections.

If your host restarts containers on deploy and on crash — most do — treat
restarts as routine rather than as incidents, and read every counter below as
"since the last restart".

## What you can look at

Three surfaces, in increasing order of effort:

| Surface | Who it is for | What it costs to run |
| --- | --- | --- |
| **`/stats` in Discord** | moderators, on demand | nothing; it is already on |
| **`/dash` web dashboard** | you and moderators, in a browser | nothing extra — same process, same port ([dashboard.md](dashboard.md)) |
| **`/metrics`** | your own scraper | whatever your collector costs ([scaling.md §7](scaling.md#7-metrics-and-alerting)) |

`/stats` is the one that needs no setup and no infrastructure. Reach for a
scraper only when you need history and alerting; for "is it keeping up right
now", `/stats` and `/dash` answer it.

### The health port

`/healthz`, `/readyz`, `/metrics`, and (when enabled) `/dash` all share one HTTP
port — `OPTIMUS_HEALTH_PORT`, default 8080.

The consequence is easy to miss: **`/metrics` is unauthenticated.** It assumes a
scraper on a private network. The moment you give the service a public domain —
which you must do to use `/dash`, since Discord OAuth needs a public redirect —
`/metrics` is public too. It leaks no message content and no user data, only
aggregate counters, but it does tell a stranger how much traffic you handle.
Restrict the path at your platform's edge if that bothers you. `/dash` itself is
session-gated on every page but the landing and login screens.

## Reading the load numbers

`/stats` reports a **pipeline load** block. Two caveats have to be held in mind
or the numbers mislead:

1. **They are deployment-wide, not per-server.** The underlying counters carry
   no `guild_id` label — labelling them would grow cardinality with every server
   the bot joins — so a moderator reading `/stats` in their server sees totals
   across every server the bot is in. The wording in Discord says so, but expect
   the question.
2. **They reset on restart.** Which, per above, is routine. The boot counter sits
   directly above the load block for exactly this reason.

| Line | Healthy | Worth looking into |
| --- | --- | --- |
| Images scanned | grows steadily with traffic | flat while the server is busy → the bot is not seeing messages; check Message Content intent and channel permissions |
| Waiting on moderation | near zero, spikes drain in seconds | a number that keeps climbing → decisions are queuing faster than they dispatch |
| Skipped: already seen | **large is good** — it is the same image caught twice, which is the whole point of hashing | a sudden collapse to zero → check that the database is persisting |
| Skipped: unreadable or oversized | small and steady | a spike → someone is posting malformed or huge files, possibly deliberately |
| Skipped: rate-limited | zero most of the time | anything sustained → the bot is being throttled and is running behind |
| Skipped: over per-message cap | occasional | sustained → members are posting large image dumps; the per-message cap is doing its job |

"Waiting on moderation" climbing and "rate-limited" nonzero at the same time is
the one combination that means you have a real capacity problem rather than a
noisy neighbour. Everything else is usually a permissions or configuration
answer.

### When enforcement silently does nothing

The most common "the bot is broken" report is not a capacity problem — it is a
channel the bot cannot act in. Have a moderator run **`/config permissions`**:
it lists every channel where enforcement is blocked and names the missing
permission. Grant it and the backlog in that channel is rescanned
automatically; the bot posts a note in the review channel saying what it
recovered.

## Trading cost against coverage

Roughly in order of how much you save per unit of coverage given up:

| Lever | Effect | What you lose |
| --- | --- | --- |
| `OPTIMUS_DETECTION_OCR_RISK_SCAN=false` | biggest single CPU saving — turns off Tesseract OCR and QR decoding on every unmatched image | the second-look lane. Only images matching a known hash get caught; novel scams pass until a moderator blocks one |
| `retention_days` (per server, default 30) | bounds database growth | older detections and their dashboard history |
| `OPTIMUS_GATEWAY_JOIN_SCAN_DAYS=0` | skips the history backfill when joining a server | scams posted before the bot arrived survive |
| `OPTIMUS_GATEWAY_JOIN_SCAN_MAX_CHANNELS` / `_MESSAGES_PER_CHANNEL` | cheaper backfill without disabling it | depth of the initial sweep |
| `OPTIMUS_DETECTION_MAX_INFLIGHT` | caps concurrent decode/hash work, which caps peak memory and CPU | throughput headroom during a burst; work queues instead of running in parallel |
| `OPTIMUS_INGEST_MAX_INLINE_BYTES` | rejects large images earlier | very large legitimate screenshots go unscanned |

The OCR lane is the meaningful decision here. It is the only thing that catches
a scam nobody has reported yet, and it is the most expensive part of the
pipeline. Turning it off makes the bot cheap and purely reactive.

Scaling up rather than down — more replicas, Redis, Postgres — is a different
document: [scaling.md](scaling.md), with a worked capacity study in
[capacity.md](capacity.md).

## Narrowing what members can run

`OPTIMUS_MEMBER_COMMANDS` is a comma-separated list of the member-facing
commands to expose. Unset — the default — exposes all four, so this changes
nothing until you set it.

```
OPTIMUS_MEMBER_COMMANDS=report
```

| Value | Members get |
| --- | --- |
| unset | `/report`, `/appeal`, `/forget_me`, `/help` |
| `report` | `/report` only |
| `report,help` | `/report` and `/help` |

Only those four names are accepted. Moderator and admin commands are outside
this setting's reach, so no value here can take `/scamhash` or `/config` away
from your moderators, and right-click → Apps → *Report scam to mods* is always
available. A name that is not a member command is a startup error rather than a
silent no-op — a typo that quietly left a command exposed is the one failure
mode you would never notice.

Two reasons an operator running a couple of their own servers on a ban policy
usually wants `report` alone:

- **`/appeal` cannot be reached by the person it is for.** It is a guild-only
  command, and a banned user is no longer in the guild, so the command is not
  in their picker. It stays useful on the milder action policies
  (`report_only`, `delete`, `delete_timeout`), where the member is still
  present — which is why it is a setting rather than a deletion.
- **`/forget_me` is a self-serve erasure button.** Run by an offender, it
  deletes their own detection and appeal history. It does not unblock anything:
  blocked image hashes are not tied to a user, so the images stay blocked.

Whether that trade is right depends on who your members are. On a public bot
serving strangers' servers, keep both: a maintained erasure path is part of
what [Discord's developer terms](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service)
expect of you regardless of size.

Both halves are enforced. A hidden command is left out of registration so it
never appears in the picker, and it is also refused server-side — a client
holding a cached command list can still send the interaction, and the invoker
gets "That command is not available on this server."

Hiding `/forget_me` does not disable the opt-out itself. An existing opt-out
still stops that user's images being scanned in every server, and you can still
honour a request out of band.

## Quick triage

| Symptom | First thing to check |
| --- | --- |
| Nothing is being scanned at all | Message Content intent enabled in the Discord developer portal |
| Nothing scanned in one channel | `/config permissions` |
| Blocklist keeps emptying | volume mount for the SQLite file; the `/stats` boot counter |
| Detections post but nothing is deleted | the server's `action_policy` (default is `report_only`), and whether `safe_mode` turned itself on |
| Cards appear with no image | expected once the message is deleted — the image is omitted rather than shown broken |
| `/dash` returns a login loop | the OAuth redirect URI must match `OPTIMUS_DASHBOARD_BASE_URL` exactly, including scheme |
| Bot refuses to start after enabling the dashboard | it names the missing variable on the first line of the log, by design |
