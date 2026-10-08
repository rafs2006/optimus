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
   Manage Channels (only so `/setup` can create the review channel; remove it
   afterwards if you like), Moderate Members (timeouts),
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
   image, a link to a message carrying scam images, or a Discord image link —
   or `/scamhash import` with a JSON file exported from another server
   you moderate.
4. **Pick an enforcement level.** The default `action_policy` is
   `report_only` — detections are only reported to the review channel. When
   you trust the blocklist, switch to auto-enforcement:
   `/config set action_policy delete` (or `delete_timeout` / `delete_ban`).

## The review workflow

Every detection — an automatic hash/risk match, a member report, or a
moderator's `/scamhash review` — posts a **review card** into the review
channel with the evidence and the buttons below.

There is **one card per message**, and when the bot handles a scam account on
its own, one card per uploader (see "One uploader, one card" below). A post
with several flagged images gets a single card that shows up to four of them together and says how many there
are, and every button on it acts on all of them: Confirm blocklists every
image, False positive whitelists every image, Dismiss closes them all, and the
message is deleted or its uploader banned once.

The card's **Message** field is a jump link straight to the offending message,
and when the message still exists the image itself is shown on the card — so a
member report can be judged without leaving the review channel. Once the message
has been deleted the image is omitted rather than shown broken. The bot does
not keep its own copy of flagged images, so once Discord deletes the original
the image is gone — screenshot it first if you need to keep it.

| Button | What it actually does |
| --- | --- |
| **Confirm scam** | Adds the image's hash to this server's blocklist (future reposts are caught automatically), deletes the offending message, and marks the detection confirmed. Works even for member reports, which are filed without hashes: the bot re-fetches the image, hashes it, and stores it. Any whitelist entry that covers the image is removed, and the folded card says how many. |
| **False positive** | Whitelists the image so it is never flagged again (the folded card names the new entry, e.g. `#40`), reverses the recorded action, and — if the uploader was banned and you have **Ban Members** — unbans them. Without Ban Members the ban stays, and the card says so, so someone who has it can press **Unban**. |
| **Dismiss** | Closes the card and teaches the detector nothing: no hash blocked, no image whitelisted, no action taken or reversed. Use it for a mistaken member report — the one case where you want the report gone but do not want the image made permanently exempt. |
| **Ban uploader** | Bans the uploader and purges their recent messages (`ban_purge_hours`, default 24h, Discord cap 7 days). If Discord refuses (role hierarchy, missing Ban Members), you get an explicit error — never a silent failure. |
| **Unban** | Lifts the uploader's ban. |
| **Whitelist image** | Whitelists the image without touching the detection or the uploader. The folded card names the new entry. |

On servers that opted in (`optin_global_db`) **and** are approved for global
contribution (see below), **Confirm scam** and **False positive** also act on
the shared global database — no extra button or command needed. Everywhere
else both stay local to your server.

Notes on cards:

- **A decided card folds.** As soon as a moderator presses a decision, the
  card collapses to one grey line — its title plus
  `✅ <action> — handled by @moderator` — and its buttons disappear, so with
  several mods watching one channel nobody double-handles a report and the
  channel stays short. There is no separate "done" message: the folded card is
  the confirmation. Only a refusal or failure (missing permission, Discord
  said no) is answered privately, and then the card stays open.
- **A post the bot fully handled is posted already folded.** When an image
  matches *this server's* blocklist and your `action_policy` ran in full (the
  post is deleted and, for `delete_ban`, the uploader is banned), the card is
  one short report: `✅ Handled automatically by Optimus`, what was done, and
  an **Original message** link with the message ID and the uploader, kept as
  evidence of what was removed.

  The folded card has no buttons, so nothing on it can be misclicked. To
  review it or undo it, click the `/queue` mention on the card and add the
  `detection:` number it shows: the report is posted again as a full card,
  where **Unban** only lifts the ban (for example for a hacked account that
  was recovered) and **False positive** also whitelists the image. The bot's
  own action also settles the campaign the way a Confirm does: the uploader's
  other open cards are closed and their reposts of blocklisted images are
  deleted without a card.

  **One uploader, one card.** However many posts a scam account spreads
  across the server at once, the review channel keeps a single card for it:
  the first image bans the uploader and posts the card, the other images of
  that post are added to it ("· 4 images"), and every other post is deleted
  and counted on it once ("Removed N later post(s) from this uploader."). The
  uploader is banned once. The bot never removes its own cards; the logs keep
  every image. To see one of these cards again, use `/queue detection:<number>`:
  it reads "delete_ban (handled automatically)".

  **Near matches spread across channels.** A near match below
  `auto_act_threshold` on this server's own list normally waits for a
  moderator. When one uploader posts a near match of the same blocklist entry
  in 3 different channels within 10 minutes (the defaults), the bot applies your `action_policy` to them as if the
  match were strong: it bans once, sweeps the campaign and closes the earlier
  cards, the same as **Confirm scam**. The card keeps the real match score and
  says `near match posted in N channels within M min`. Global matches, member
  reports and risk-scan cards never count; a channel where a moderator pressed
  **False positive**, **Whitelist image** or **Dismiss** on that uploader's card
  stops counting; safe mode and `report_only` turn it off; moderators and
  admins are never banned by it. The host sets the numbers
  with `OPTIMUS_MOD_SPREAD_CHANNELS` (0 turns it off) and
  `OPTIMUS_MOD_SPREAD_WINDOW_SECONDS`.

  Everything else that needs a person keeps a full, open card: a global-only
  match, a near match below `auto_act_threshold`, safe mode, a member report,
  a punishment refused by the role hierarchy, or a missing permission. The
  one exception is an uploader the bot or a moderator already settled in the
  last 10 minutes: their global and near matches are deleted without a card
  (see "One confirmation settles the whole campaign" below).
- **Uploaders who already left are still banned.** Scam accounts often post
  and leave within seconds. Discord bans by user ID, member or not, so
  `delete_ban` (and **Confirm scam** under it) still bans them, and the card
  says `banned by user ID: the uploader had already left`. A timeout or kick
  cannot apply to someone who left, so those policies only delete the post
  and the card says why. If the bot cannot read the uploader's roles at all,
  it never punishes blind: it deletes the post and the card says the
  punishment was skipped.
- After **Confirm scam**, the folded card also shows what enforcement did
  (`Action: …`). Cards from `/scamhash review` are posted already folded,
  since a moderator already made the call.
- **One confirmation settles the whole campaign.** A scammer usually pastes
  the same picture into many channels; cards that wait for a moderator come
  one per message.
  **Confirm scam** (or *Review as scam*) on any one of them, whatever your
  `action_policy`:
  - deletes that uploader's other image posts from the last 24 hours in every
    channel and adds their images to the blocklist;
  - closes that uploader's other open cards and deletes them from the review
    channel (`Action: … — cleared N other report(s) from this uploader`);
  - for the next 24 hours, deletes their reposts of blocklisted images
    without a new card; the confirmed card counts them instead
    ("Removed N later post(s) from this uploader."). A repost Discord refuses
    to delete, a new image that only looks risky, or anything in safe mode
    still gets a normal card. This memory resets when the bot restarts;
  - for the first 10 minutes, does the same for their weaker matches too: a
    near match, or a match from the shared global list, which on its own
    only asks a moderator. Posts that arrive at the same moment are handled
    one after another, so they join that one card instead of getting their
    own. The ban itself always rests on this server's own list.

  Cards from before this change don't record which card they are, so they
  are closed but stay in the channel for you to delete.
- **Fixing a misclick:** `/queue detection:<number>` posts that report again
  as a full card with fresh buttons, covering every image of the message. Press
  the right decision there; the old folded card stays as the record of the
  first one. If the misclick was **False positive** or **Whitelist image**,
  pressing **Confirm scam** on the reopened card also removes the whitelist
  entry. To remove it without acting, use `/scamhash unwhitelist entry:<number>`
  with the number from the folded card or `/scamhash whitelist`.
- Each button needs the Discord permission that matches what it does, so
  the moderators you already trust with that power can use it — no Manage
  Server needed:

  | Button | Needs |
  | --- | --- |
  | Confirm scam · False positive · Dismiss · Whitelist image | **Manage Messages** |
  | Ban uploader · Unban | **Ban Members** |

  Administrators can press everything. *False positive* can also lift a ban,
  so it only does that for someone who holds Ban Members too. *Confirm scam*
  and *False positive* can also touch the shared list, which is fine on
  Manage Messages: only opted-in, owner-approved servers take part, and a
  hash needs two such servers to go live. *Confirm scam* then applies your
  `action_policy`, which may ban: that is the standing decision your admins
  set, so it rides on Manage Messages. *Ban uploader* is the discretionary
  ban, and that is the one reserved for Ban Members. Ordinary members see
  nothing in the private channel anyway.
- If a moderator gets "you don't have permission" on a button, give their
  role the permission above — adding them to `mod_role` only lets them *see*
  the channel.
- **False positives are cheap, misses are not.** When in doubt, Confirm — a
  wrong call is undone by reopening the card with `/queue detection:<number>`
  and pressing **Unban**. Members who need to contest a call reach you directly.
- **Do not use False positive to clear junk reports.** It permanently
  whitelists the image, so a member who reports a real scam image incorrectly
  would get that image exempted from detection forever. **Dismiss** is the
  no-op exit for that case; the reporter is not told either way, so nobody can
  probe the bot to learn what gets through.
- **Every card is either acted on or dismissed.** Anything left untouched stays
  in `/queue` indefinitely — that is deliberate, so a neglected backlog stays
  visible instead of quietly ageing out.

## Deciding fast

A review card is built to be judged in a few seconds without leaving the
channel. Read it in this order and stop as soon as you have an answer.

1. **The image**, shown on the card itself. Most decisions end here.
2. **OCR/QR risk scan** — present only when the image did *not* match a known
   hash. This is the bot saying "I have never seen this, but the text or QR code
   in it looks like phishing", with the signals it found. Treat it as a prompt
   to look, not as a verdict. Common words ("team", "support", "free") next
   to a link are not enough on their own, and links to the official AI sites
   or discord.com don't count against an image, so ordinary screenshots stay
   out of the queue. Discord invite and app-authorization links, subdomains,
   and pages anyone can publish (Google Forms/Sites, Hugging Face) still do. What does raise a card is a claim/redeem prompt, a login
   or wallet request, a known scam phrase or a crypto address, especially
   beside an unfamiliar link, or any lookalike domain.
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
| A scammer | brand-new account, no history in the server, posted the same image into several channels, no other messages | **Confirm scam**. With `action_policy delete_ban` that bans too; otherwise reopen the card (`/queue detection:<number>`) and press **Ban uploader** — the ban purges their recent messages too |
| A stolen or hacked account | a known member with real history who suddenly posts a giveaway image, often across many channels at once, often at an odd hour | **Confirm scam** to kill the image, then a timeout rather than a ban — the owner is the victim and will want the account back |
| Someone sharing a warning | a member posting a screenshot *of* a scam to warn others | **Whitelist image** if it will keep re-tripping, or leave it; do not punish |

When you cannot tell, act on the image and be gentle with the account. Deleting
the post is the urgent half; a ban is the reversible-but-annoying half, and
**Unban** is one `/queue detection:<number>` away.

### The bar for pressing Confirm

**False positives are cheap, misses are not.** A wrong Confirm costs one member
one message to you, and one press of **Unban** to put right. A miss costs
somebody their account or their wallet. When in doubt, Confirm.

The one thing worth slowing down for is **Ban uploader** on an account with real
history in the server. That is the case where a timeout is the better first move.

## How members help

There is a plain-language page for your members at
[for-members.md](for-members.md) — what the bot looks at, what it keeps, and how
to reach you. Link or pin it; it needs no moderator context to read.

- **Report scam to mods** — right-click any message → Apps → *Report scam to
  mods*. Available to everyone, rate-limited, files a review card with the
  reporter attributed. It never blocks, deletes, or bans by itself.
- **/report `<message>`** — the same thing as a typed command, for members
  whose client hides the right-click *Apps* menu (mobile especially). Takes a
  message link, or a bare message ID when the message is in the current
  channel. Identical limits: same per-user rate limit, and reporting the same
  message twice does not create a second card.

There is no member-facing opt-out from scanning. A member who wants to know
what is held about them, or wants it erased, emails the address in the
[privacy policy](privacy-policy.md); requests are handled by the bot's operator,
not by you. Detections, appeals, and audit rows are deleted automatically after
30 days (or your server's configured window). If you want to exempt a specific
person from scanning, mark them as a trusted user — that is a moderator
decision, deliberately not a member one.

## Commands

Moderator commands. `/scamhash` (every subcommand), the right-click
**Review as scam** and `/queue` need **Manage Messages**, the same bar as the
review buttons, so the moderators who keep the blocklist can add, fix and
move it without Manage Server (Manage Server still works for them too). The
rest need **Manage Server**:

| Command | What it does |
| --- | --- |
| `/setup [mod_role] [channel]` | Create (or link) the private review channel. |
| `/scamhash add [image] [message] [url]` | Block scam images without acting on anyone: an upload, every image on a message (`message:` link, or a bare ID in the message's own channel), and/or one Discord image link (`url:`, right-click the image → Copy Link). Up to 10 per command; future reposts are caught. An image the list already has, or a copy Optimus already catches, is not added again: the reply names the entry that covers it. Whitelist entries that cover the image are removed first (the whitelist wins over the blocklist), and the reply names them. |
| `/scamhash remove <hash_id>` | Unblock by hash id (from `/scamhash list`). |
| `/scamhash list` | Show the 10 newest blocked hashes: id, how it was added (Confirm scam, Review as scam, campaign cleanup, `/scamhash add`, import), by whom ("Optimus" for its own campaign cleanup) and when. `/scamhash export` has all of them. |
| `/scamhash whitelist [page] [by] [since]` | Show the images this server exempts from detection, 10 per page, newest first: entry number, the button and detection that created it, by whom and when, and the blocked hashes it overrides. `by:` and `since:` (`30m`, `2h`, `3d`, `1w`) narrow the list. |
| `/scamhash unwhitelist [entry] [by] [since] [confirm]` | Remove whitelist entries so Optimus flags those images again. `entry:` takes entry numbers or hash ids, comma-separated, and removes them at once. `by:` and/or `since:` select a batch (for example a run of misclicks): the first run only lists it, and the same command with `confirm:True` removes it. |
| `/scamhash export` | Download this server's hashes as JSON. The file also lists the whitelist for review; `/scamhash import` skips that part. |
| `/scamhash import <file>` | Load hashes from another server's export. |
| `/scamhash review <message>` | Mark a posted message as scam by link/ID: blocks its images and applies the action policy. Also available as right-click → Apps → *Review as scam*. |
| `/config view` | Show all settings. |
| `/config set <field> <value>` | Change one setting (fields below). |
| `/config permissions` | Check the review channel first, then list the channels the bot watches but can't enforce in, with the exact permission each is missing. Channels hidden from the bot are counted as private, not flagged. The first thing to run when "the bot ignored a scam". |
| `/stats` | Detection activity, pipeline load, and the database persistence canary. |
| `/queue [detection]` | List the cards still waiting on a decision, oldest first, each with a jump link and its image count. Answers "what did we miss while nobody was online?" — the review channel cannot, once the backlog has scrolled past a screen. `detection:<number>` reopens that report as a full card with buttons, to correct a decision. |
| `/help` | This guide's short version, right inside Discord. Available to everyone. |

Admin-only:

| Command | What it does |
| --- | --- |
| `/delete_server_data` | Permanently deletes ALL of this server's data (confirmation button; GDPR). |

Everyone:

| Command | What it does |
| --- | --- |
| `/report <message>` | Send a message's image to the mod-review queue by link/ID. Same as right-click → Apps → *Report scam to mods*. |
| `/help` | Explains the commands and the review workflow. |
| Apps → *Report scam to mods* | File a message into the mod-review queue. |

Whoever hosts the bot can narrow that last table with
`OPTIMUS_MEMBER_COMMANDS` — see
[Narrowing what members can run](running-optimus.md#narrowing-what-members-can-run).
The right-click *Report scam to mods* entry is always available.

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
  confirmation"* — it never bans anyone, and it is never auto-deleted,
  regardless of your `action_policy`. Your moderator presses **Confirm scam**
  to act, which also adds the hash to your own local blocklist (local matches
  of it do use your action policy from then on). One exception to the
  deleting: if this server banned or confirmed the same uploader on its own
  list in the last 10 minutes, their global match is deleted quietly and
  counted on that card. It still bans nobody.
- **Contributing is allowlisted.** Only servers the bot owner approved with
  `/global approve_server` can push toward the shared list. On those servers,
  **Confirm scam** doubles as a vote; a hash goes live globally only after
  moderators on **two different approved servers** independently confirm it.
  This is the anti-poisoning gate: throwaway servers and colluding accounts
  outside the allowlist have zero influence.
- **False positives self-heal.** If an opted-in, approved server marks a
  globally-matched image as a false positive, the hash is revoked from the
  global list for everyone and the submitter's reputation is docked. Any other
  server's False positive only whitelists the image locally — otherwise a
  scammer could set up a server, report their own image, and pull it off the
  list for everyone.
- Promoted hashes are cryptographically signed; rate limits and reputation
  scores throttle even approved contributors.

## Settings reference (`/config set`)

| Field | Values | Default | Meaning |
| --- | --- | --- | --- |
| `sensitivity` | `strict` / `balanced` / `permissive` | `balanced` | How close an image must be to a blocked hash to count as a match. `strict` catches more variants at a slightly higher false-positive risk. |
| `action_policy` | `report_only` / `delete` / `delete_timeout` / `delete_ban` | `report_only` | What happens automatically on a confident match. `report_only` never touches messages. |
| `mod_queue_threshold` | `0.0`–the deployment's auto-action threshold (`0.85` by default) | `0.5` | Minimum confidence for a detection to be posted for review; anything below is ignored. It cannot be set above the confidence at which automatic action starts — `/config set` refuses and names the limit. |
| `retention_days` | `1`–`365` | `30` | How long detection records are kept. |
| `ban_purge_hours` | `0`–`168` | `24` | How much of a banned user's message history is purged (Discord caps at 7 days; `0` disables). |
| `locale` | `en` / `sr` | `en` | Language for the bot's replies. |
| `review_channel` | `#channel` or `none` | unset | Where review cards post. `/setup` manages this for you. |
| `optin_global_db` | `true` / `false` | `false` | Also match against the shared cross-server scam database. Global matches only ever create review cards — they never auto-act, except that an uploader this server already banned or confirmed in the last 10 minutes has them deleted quietly. |
| `optin_scan_bots` | `true` / `false` | `false` | Also scan images posted by bots and webhooks. Off by default; turn on if scam posts arrive via webhooks. |
| `safe_mode` | `true` / `false` | `false` | Circuit breaker: detections still post for review, but nothing is auto-deleted or auto-banned. The bot may enable this itself after repeated failures; a button on the notice turns it off. |

## Good to know

- **Bots and webhooks are not scanned by default** (`optin_scan_bots`). If
  scams reach you through webhook mirrors, turn it on.
- **New-member backfill:** when scanning is enabled the bot also scans recent
  history, including active threads and forum posts, so scams posted just
  before the bot joined don't survive.
- **`/config permissions` answers "why did the bot ignore that?"** Nine times
  out of ten the bot could not act in that channel rather than chose not to.
  It starts with the review channel — if the bot can't post cards there, no
  moderator sees any detection — then lists the channels it watches but can't
  enforce in, with the missing permission for each. Channels hidden from the
  bot (staff, beta, archive rooms) are only counted: keeping them private is
  fine. On `report_only`, a missing Manage Messages is shown as what deleting
  would need, not as a fault.
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
- **Every action is written to Discord's own audit log**, with the cause and
  the evidence, under **Server Settings → Audit Log**. Optimus only ever acts
  for one reason — a posted image matched a known scam fingerprint — so every
  entry starts with the same words, `Scam image`, and you can filter on that to
  see the bot's whole history in the server:

  | Entry begins | What happened |
  | --- | --- |
  | `Scam image — optimus auto-enforced` | the bot acted on its own; the entry carries the match confidence, the fingerprint, and the message id |
  | `Scam image — confirmed by moderator` | a moderator pressed **Confirm scam** or **Ban uploader**; carries the detection number |
  | `Scam image — false positive, action reversed` | a moderator pressed **False positive** |
  | `Scam image — unbanned by moderator` | a moderator pressed **Unban** |
  | `Scam image — appeal approved` | a member's appeal was granted |

  Two things worth knowing. The audit-log reason is visible only to people with
  **View Audit Log**, never to the person who was removed. And Discord keeps
  audit entries for a limited window, so your review channel — not the audit
  log — is the durable record.
- **Rate limits** protect against abuse of `/scamhash add`, member reports,
  and global votes; hitting one is an explicit "try later" reply.
- **`/config view` explains every setting inline** — each field shows its
  current value, what it does, and its default, so you rarely need this page.
- **Privacy:** no message text and no images are stored — only perceptual
  hashes and the ids needed to act on a detection, and only for images that
  were actually flagged. `/delete_server_data` wipes this server's data in
  full. Members ask about their own data by email; see the
  [privacy policy](privacy-policy.md).
- **Web dashboard (optional):** if whoever hosts the bot has enabled it, you
  can browse scan activity, every detection (including clean scans), and the
  audit log in a browser with your Discord login — anyone with **Manage
  Server** in this server gets access automatically. It is read-only; actions
  still happen here in Discord. See [dashboard.md](dashboard.md).

Found a bug or want a feature? Open an issue on
[GitHub](https://github.com/rafs2006/optimus/issues).
