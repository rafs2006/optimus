# Optimus

[![CI](https://github.com/la314sazuli/optimus/actions/workflows/ci.yml/badge.svg)](https://github.com/la314sazuli/optimus/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Optimus is a Discord bot that automatically detects and removes scam, phishing,
and fraud **images** — fake giveaways, fake Nitro/Steam gifts, fake exchange
screenshots, wallet-drainer QR codes — within seconds of them being posted. It
matches every uploaded image against a database of known scam images using
perceptual hashing, so it still catches a scam after the usual re-share tricks
(cropping, re-coloring, re-compression, resizing, watermarking) while keeping a
zero-false-positive bias so it never punishes legitimate users.

## Quickstart

Three steps: create a bot, run Optimus, tell it what to catch. No database, no
infrastructure — just a bot token.

### 1. Create your Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Give it a name (e.g. "Optimus") and create it.
2. Open the **Bot** tab. Click **Reset Token**, then **Copy** — this is your
   `OPTIMUS_DISCORD_TOKEN`. Keep it secret.
3. Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and turn on
   **Message Content Intent**. Optimus needs this to see the images people post.
   (Leave Presence and Server Members off.)
4. Open **OAuth2 → URL Generator**. Under **Scopes** tick `bot` and
   `applications.commands`. Under **Bot Permissions** tick:
   **View Channels**, **Read Message History**, **Manage Messages** (to delete
   scams), **Moderate Members** (timeouts), and **Kick Members** / **Ban Members**
   if you want those actions available.
5. Copy the generated URL at the bottom, open it in your browser, and invite the
   bot to your server.

### 2. Run it

Optimus runs as a single process with zero external services. Pick one:

**With [uv](https://docs.astral.sh/uv/):**

```bash
# from a checkout of this repo:
OPTIMUS_DISCORD_TOKEN=your-token-here uv run optimus

# or, once published to an index, with no checkout at all:
OPTIMUS_DISCORD_TOKEN=your-token-here uvx optimus
```

**With Docker (one container, nothing else):**

```bash
docker build -t optimus .
docker run --rm -e OPTIMUS_DISCORD_TOKEN=your-token-here \
  -v optimus-data:/data optimus
```

The image defaults to simple mode, so a single `docker run` is the whole bot.
The `optimus-data` volume is mounted at `/data`, where the image keeps the SQLite
database (registered scam hashes, per-server config) across restarts; drop the
`-v` flag for an ephemeral run.

On startup you'll see a line like `optimus_online bot=YourBot#1234` once it has
connected and registered its slash commands. If anything is wrong before that —
a missing or malformed token, a token Discord rejects, an unwritable database
path — Optimus prints a single clear line telling you exactly what to fix,
instead of a traceback.

### 3. Teach it which images are scams

Optimus only acts on images you've shown it. In your server, use the slash
commands (registered automatically on first run):

- **`/setup`** — one command to create the private **#optimus-review** channel
  (pass `mod_role:` to grant your moderator role access) where every detection
  posts a review card with Confirm / False positive / Ban buttons. The
  **[Moderator Guide](docs/moderator-guide.md)** covers the full review
  workflow, every button, and every setting — share it with your mod team.
- **`/scamhash add`** — attach a scam image to block it. From now on Optimus
  catches re-posts of that image and variants of it.
- **`/scamhash review`** — point at a message that was already posted (link or
  ID, or right-click → Apps → *Review as scam*): its images are blocked and the
  author is actioned per your `action_policy`.
- **`/scamhash list`** / **`/scamhash remove`** — review or drop blocked hashes.
- **`/scamhash export`** — download this server's hashes as a JSON file;
  **`/scamhash import`** loads that file on another server.
- **`/config set`** — choose what happens on a match (report / delete / timeout /
  ban), the detection **sensitivity** (`strict` / `balanced` / `permissive`), and
  the moderator review channel. **`/config view`** shows the current settings.
- **`/stats`** — see detection activity for your server, plus how hard the bot
  is working (images scanned, queue depth, skips) without needing `/metrics`.

Members can right-click any image message → Apps → **_Report scam to mods_** (or
run **`/report <link-or-id>`**, which does the same thing and works where the
right-click menu is hidden) to flag it for the moderator queue (nothing is
deleted or blocked until a mod confirms), run **`/appeal`** on a detection, and
**`/forget_me`** to erase their data. That's the whole product — register scam
images, pick an action, done.

### What gets scanned

Optimus scans images **live** in every server it's in — new messages and edits
(scam-swapped-in-by-edit included) — from the moment it joins. On joining a
server it also **backfills the last 3 days of message history** (scam waves
usually start *before* someone installs the bot), bounded to 50 channels and
the newest 200 messages per channel. Tune with `OPTIMUS_GATEWAY_JOIN_SCAN_DAYS`
(`0` disables the join scan), `OPTIMUS_GATEWAY_JOIN_SCAN_MAX_CHANNELS`, and
`OPTIMUS_GATEWAY_JOIN_SCAN_MESSAGES_PER_CHANNEL`.

## How detection works (the short version)

Every image is reduced to a four-hash **perceptual fingerprint** (pHash, dHash,
wHash, aHash) and matched against your registered scam hashes plus an optional
shared global database. Perceptual hashing is robust to the transforms scammers
use to dodge exact-match filters, and the ensemble vote plus a tunable
sensitivity preset is what gives the zero-false-positive bias. Detection quality
is measured against a fixture corpus — see
[docs/detection-eval.md](docs/detection-eval.md).

Hashes only catch images that have been seen before, so images with **no hash
match** get a second look: an **OCR + QR risk scan** reads the text out of the
image (Tesseract), decodes QR codes (decode only — payloads are never fetched),
repairs defanged URLs (`hxxps://`, `perplexity[.]com`), flags lookalikes of
official AI-company domains, and scores phishing signals (credential harvesting,
wallet connect prompts, crypto addresses, urgency language). High/critical
findings go to the **mod queue as ambiguous** with the evidence on the review
card — this lane never deletes, bans, or stores a hash on its own. Disable with
`OPTIMUS_DETECTION_OCR_RISK_SCAN=false` if you'd rather run hashes alone.

## More

**You don't need any of this to run the bot.** These cover the internals and
running at scale:

- [docs/moderator-guide.md](docs/moderator-guide.md) — hand this to your mod
  team: setup, the review workflow, every button, command, and setting.
- [docs/](docs/README.md) — full documentation index.
- [docs/architecture.md](docs/architecture.md) — system design and the
  six-service distributed topology (`OPTIMUS_MODE=distributed`) for very large
  fleets.
- [docs/scaling.md](docs/scaling.md) — operating at scale: sharding, detection
  replicas, distributed rate limiting, retention, pooling, monitoring, alerting.
- [docs/security-audit.md](docs/security-audit.md) — the security model and audit
  record (SSRF defense, decode sandboxing, signed global DB, multi-tenant RLS).
- [CONTRIBUTING.md](CONTRIBUTING.md) — developer workflow, tests, and PR
  expectations.
- [.env.example](.env.example) — every setting, with the simple-mode keys first.

## License

MIT — see [LICENSE](LICENSE).
