# Optimus Moderator Guide

Everything a moderator needs to run Optimus day to day: getting the bot into
your server, wiring up the shared review channel, what each button on a review
card does, and every command and setting.

- **What Optimus does:** matches every uploaded image against the server's
  scam-image blocklist using perceptual hashing, so re-shared scams (cropped,
  re-colored, re-compressed, resized, watermarked, mirrored) are still caught.
  High-risk images that don't match a known hash (fake-Nitro/giveaway text, QR
  codes to suspicious hosts) are flagged for review instead of auto-actioned.
- **Zero-trust bias:** the bot only auto-punishes on high-confidence matches
  against hashes *your* moderators put in. Everything ambiguous lands in the
  review channel for a human decision.

## First-time setup (5 minutes)

1. **Invite the bot** with these permissions: View Channels, Read Message
   History, Manage Messages (delete scams), Ban Members (ban scammers),
   Manage Channels (create the review channel), Moderate Members (timeouts),
   plus the `applications.commands` scope. The README's
   [Quickstart](../README.md#quickstart) has the full OAuth walkthrough.
2. **Run `/setup mod_role:@YourModRole`.** This creates a private
   **#optimus-review** channel — hidden from `@everyone`, visible to the bot,
   the role you picked, and server admins (admins bypass channel overrides) —
   and links it as the review channel. All detections now post there for
   moderator sign-off.
   - Already have a mod channel? `/setup channel:#your-channel` links it
     instead (the bot never edits an existing channel's permissions — make
     sure only mods can see it).
   - Re-running `/setup` never creates duplicates; it tells you where reviews
     already go. Use the `channel` option to move them.
3. **Seed the blocklist.** `/scamhash add` with a screenshot of a known scam
   image, or `/scamhash import` with a JSON file exported from another server
   you moderate.
4. **Pick an enforcement level.** The default `action_policy` is
   `report_only` — detections are only reported to the review channel. When
   you trust the blocklist, switch to auto-enforcement:
   `/config set action_policy delete` (or `delete_timeout` / `delete_ban`).

## The review workflow

Every detection — an automatic hash/risk match, a member report, or a
moderator's `/scamhash review` — posts a **review card** into the review
channel with the evidence and five buttons.

The card's **Message** field is a jump link straight to the offending message,
and when the message still exists the image itself is shown on the card — so a
member report can be judged without leaving the review channel. Once the message
has been deleted the image is omitted rather than shown broken; recovering it
after the fact requires `optin_evidence_storage`.

| Button | What it actually does |
| --- | --- |
| **Confirm scam** | Adds the image's hash to this server's blocklist (future reposts are caught automatically), deletes the offending message, and marks the detection confirmed. Works even for member reports, which are filed without hashes: the bot re-fetches the image, hashes it, and stores it. |
| **False positive** | Whitelists the image so it is never flagged again, reverses the recorded action, and — if the uploader was banned — unbans them. |
| **Ban uploader** | Bans the uploader and purges their recent messages (`ban_purge_hours`, default 24h, Discord cap 7 days). If Discord refuses (role hierarchy, missing Ban Members), you get an explicit error — never a silent failure. |
| **Unban** | Lifts the uploader's ban. |
| **Whitelist image** | Whitelists the image without touching the detection or the uploader. |

On servers approved for global contribution (see below), **Confirm scam** and
**False positive** additionally vote in the shared global database — no extra
button or command needed.

Notes on cards:

- When a moderator handles a card, the card gets a visible
  `✅ <action> — handled by @moderator` line, so with several mods watching
  one channel nobody double-handles a report. Detailed results are shown
  only to the clicking moderator.
- Buttons require the **Manage Server** permission; ordinary members can see
  nothing in the private channel anyway.
- **False positives are cheap, misses are not.** When in doubt, Confirm — the
  affected user can `/appeal`, and appeals land back in the same channel with
  Approve/Deny buttons. Approving an appeal reverses the action and unbans
  the appellant.

## Deciding fast

A review card is built to be judged in a few seconds without leaving the
channel. Read it in this order and stop as soon as you have an answer.

1. **The image**, shown on the card itself. Most decisions end here.
2. **OCR/QR risk scan** — present only when the image did *not* match a known
   hash. This is the bot saying "I have never seen this, but the text or QR code
   in it looks like phishing", with the signals it found. Treat it as a prompt
   to look, not as a verdict.
3. **Matched hash** and **Confidence** — present when it *did* match. A match
   against a hash your own team added is the strongest signal on the card.
4. **Source** — if it says the global database, nothing has been done and
   nothing will be until you press a button, whatever your `action_policy` says.
5. **Uploader** and **Channel** — context for *who*, which is a different
   question from *is this a scam* (below).
6. **Action taken** / **Needs attention** — what already happened, and anything
   the bot could not finish.

### Tells on the image

What the OCR lane is scoring, and what to look for yourself:

- A **QR code** in a giveaway, gift, support, or "verify your wallet" image.
  QR codes are decoded but never opened, so nobody has visited the destination —
  and a QR code is the point of most drainer images.
- **Login or seed-phrase prompts** rendered inside a picture. No legitimate
  service asks for credentials via a screenshot.
- **Lookalike domains** — a character swapped, a bracket added, an extra
  hyphen. The scan flags impersonations of well-known AI and crypto brands, but
  read the URL in the image yourself; it is often the fastest tell.
- **Urgency plus a deadline** — "first 100 users", "expires today", a countdown
  baked into the picture.
- **Free Nitro / Steam gift / exchange-balance screenshots.** These are
  templates. Once you block one, reposts are caught automatically even after
  cropping and recoloring, so confirming the first one pays for itself.

### Scammer or stolen account?

The decision on the *image* and the decision on the *person* are separate, and
the card gives you signals for both:

| Looks like | Signals | Reasonable response |
| --- | --- | --- |
| A scammer | brand-new account, no history in the server, posted the same image into several channels, no other messages | **Confirm scam**, then **Ban uploader** — the ban purges their recent messages too |
| A stolen or hacked account | a known member with real history who suddenly posts a giveaway image, often across many channels at once, often at an odd hour | **Confirm scam** to kill the image, then a timeout rather than a ban — the owner is the victim and will want the account back |
| Someone sharing a warning | a member posting a screenshot *of* a scam to warn others | **Whitelist image** if it will keep re-tripping, or leave it; do not punish |

When you cannot tell, act on the image and be gentle with the account. Deleting
the post is the urgent half; a ban is the reversible-but-annoying half, and
**Unban** and `/appeal` both exist.

### The bar for pressing Confirm

**False positives are cheap, misses are not.** A wrong Confirm costs one member
one `/appeal` — which lands right back in this channel with Approve/Deny, and
approving it reverses the action and unbans them. A miss costs somebody their
account or their wallet. When in doubt, Confirm.

The one thing worth slowing down for is **Ban uploader** on an account with real
history in the server. That is the case where a timeout is the better first move.

## How members help

There is a plain-language page for your members at
[for-members.md](for-members.md) — what the bot looks at, what it keeps, and how
to appeal. Link or pin it; it needs no moderator context to read.

- **Report scam to mods** — right-click any message → Apps → *Report scam to
  mods*. Available to everyone, rate-limited, files a review card with the
  reporter attributed. It never blocks, deletes, or bans by itself.
- **/report `<message>`** — the same thing as a typed command, for members
  whose client hides the right-click *Apps* menu (mobile especially). Takes a
  message link, or a bare message ID when the message is in the current
  channel. Identical limits: same per-user rate limit, and reporting the same
  message twice does not create a second card.
- **/appeal** — a punished member's way to contest their most recent
  detection. Appears as an Approve/Deny card in the review channel.
- **/forget_me** — GDPR: erases the requesting user's data and opts them out
  of all processing.

## Commands

Moderator commands (require **Manage Server**):

| Command | What it does |
| --- | --- |
| `/setup [mod_role] [channel]` | Create (or link) the private review channel. |
| `/scamhash add <image>` | Block a scam image; future reposts are caught. |
| `/scamhash remove <hash_id>` | Unblock by hash id (from `/scamhash list`). |
| `/scamhash list` | Show blocked hashes. |
| `/scamhash export` | Download this server's hashes as JSON. |
| `/scamhash import <file>` | Load hashes from another server's export. |
| `/scamhash review <message>` | Mark a posted message as scam by link/ID: blocks its images and applies the action policy. Also available as right-click → Apps → *Review as scam*. |
| `/config view` | Show all settings. |
| `/config set <field> <value>` | Change one setting (fields below). |
| `/config permissions` | List every channel where the bot cannot enforce, and the exact permission it is missing. The first thing to run when "the bot ignored a scam". |
| `/stats` | Detection activity, pipeline load, and the database persistence canary. |
| `/help` | This guide's short version, right inside Discord. Available to everyone. |

Admin-only:

| Command | What it does |
| --- | --- |
| `/delete_server_data` | Permanently deletes ALL of this server's data (confirmation button; GDPR). |

Everyone:

| Command | What it does |
| --- | --- |
| `/report <message>` | Send a message's image to the mod-review queue by link/ID. Same as right-click → Apps → *Report scam to mods*. |
| `/appeal` | Contest your most recent detection. |
| `/forget_me` | Erase your data and opt out of processing. |
| `/help` | Explains the commands and the review workflow. |
| Apps → *Report scam to mods* | File a message into the mod-review queue. |

Bot owner only (anyone else gets an "owner only" refusal, even if they can
see the command):

| Command | What it does |
| --- | --- |
| `/global approve_server <server_id>` | Approve a server: its mods' **Confirm scam** clicks count as global votes. |
| `/global revoke_server <server_id>` | Remove a server from the approved contributors. |
| `/global servers` | List approved contributor servers. |

## The global scam database

The global database shares scam hashes across servers so a scam confirmed on
one server can be caught everywhere. It is designed so that **no other
community can ever cause action on your server**:

- **Consuming is opt-in and review-only.** With `optin_global_db: true`, a
  global match posts a review card marked *"Global scam database — needs your
  confirmation"* — it is **never** auto-deleted or auto-banned, regardless of
  your `action_policy`. Your moderator presses **Confirm scam** to act, which
  also adds the hash to your own local blocklist (local matches of it do use
  your action policy from then on).
- **Contributing is allowlisted.** Only servers the bot owner approved with
  `/global approve_server` can push toward the shared list. On those servers,
  **Confirm scam** doubles as a vote; a hash goes live globally only after
  moderators on **two different approved servers** independently confirm it.
  This is the anti-poisoning gate: throwaway servers and colluding accounts
  outside the allowlist have zero influence.
- **False positives self-heal.** If any server marks a globally-matched image
  as a false positive, the hash is revoked from the global list for everyone
  and the submitter's reputation is docked.
- Promoted hashes are cryptographically signed; rate limits and reputation
  scores throttle even approved contributors.

## Settings reference (`/config set`)

| Field | Values | Default | Meaning |
| --- | --- | --- | --- |
| `sensitivity` | `strict` / `balanced` / `permissive` | `balanced` | How close an image must be to a blocked hash to count as a match. `strict` catches more variants at a slightly higher false-positive risk. |
| `action_policy` | `report_only` / `delete` / `delete_timeout` / `delete_ban` | `report_only` | What happens automatically on a confident match. `report_only` never touches messages. |
| `mod_queue_threshold` | `0.0`–`1.0` | `0.5` | Minimum confidence for a detection to be posted for review. |
| `retention_days` | `1`–`365` | `30` | How long detection records are kept. |
| `ban_purge_hours` | `0`–`168` | `24` | How much of a banned user's message history is purged (Discord caps at 7 days; `0` disables). |
| `locale` | `en` / `sr` | `en` | Language for the bot's replies. |
| `review_channel` | `#channel` or `none` | unset | Where review cards post. `/setup` manages this for you. |
| `optin_global_db` | `true` / `false` | `false` | Also match against the shared cross-server scam database. Global matches only ever create review cards — they never auto-act. |
| `optin_scan_bots` | `true` / `false` | `false` | Also scan images posted by bots and webhooks. Off by default; turn on if scam posts arrive via webhooks. |
| `optin_evidence_storage` | `true` / `false` | `false` | Keep evidence copies of detected images. |
| `safe_mode` | `true` / `false` | `false` | Circuit breaker: detections still post for review, but nothing is auto-deleted or auto-banned. The bot may enable this itself after repeated failures; a button on the notice turns it off. |

## Good to know

- **Bots and webhooks are not scanned by default** (`optin_scan_bots`). If
  scams reach you through webhook mirrors, turn it on.
- **New-member backfill:** when scanning is enabled the bot also scans recent
  history, including active threads and forum posts, so scams posted just
  before the bot joined don't survive.
- **`/config permissions` answers "why did the bot ignore that?"** Nine times
  out of ten the bot could not act in that channel rather than chose not to.
  The command lists the blocked channels and the missing permission for each.
  Grant it and the bot notices on its own — no restart, no re-running anything:
  it rescans that channel's recent history for scams posted while it was locked
  out and posts a note in the review channel saying what it found.
- **The database line in `/stats`** shows a boot counter and first-boot date —
  if the boot count resets, your host is not persisting the database file.
- **The pipeline-load section of `/stats`** answers "is the bot keeping up?":
  images scanned, how many are waiting on moderation right now, and how many
  were skipped (with a breakdown). Two things to read it correctly: the
  numbers cover **every server the bot is in**, not just yours — the
  underlying counters carry no server label — and they **reset on restart**,
  which is why the boot number sits right above them. A large "already seen"
  count is normal and healthy (it is the same image caught twice); a growing
  "waiting on moderation" or a large "rate-limited" is the bot running behind.
- **Rate limits** protect against abuse of `/scamhash add`, member reports,
  and global votes; hitting one is an explicit "try later" reply.
- **`/config view` explains every setting inline** — each field shows its
  current value, what it does, and its default, so you rarely need this page.
- **Privacy:** no message text is stored; only perceptual hashes, ids needed
  for enforcement, and (opt-in) evidence. `/forget_me` and
  `/delete_server_data` are honored fully.
- **Web dashboard (optional):** if whoever hosts the bot has enabled it, you
  can browse scan activity, every detection (including clean scans), and the
  audit log in a browser with your Discord login — anyone with **Manage
  Server** in this server gets access automatically. It is read-only; actions
  still happen here in Discord. See [dashboard.md](dashboard.md).

Found a bug or want a feature? Open an issue on
[GitHub](https://github.com/rafs2006/optimus/issues).
