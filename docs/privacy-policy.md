# Privacy Policy for Optimus

_Last updated: 2026-09-02_

Optimus ("the Bot", "we", "us") is a Discord bot that detects known scam and
phishing images. This instance is operated by the project maintainer
(GitHub: [`rafs2006`](https://github.com/rafs2006)).

**Contact for any privacy question or deletion request:
[optimus.privacy@proton.me](mailto:optimus.privacy@proton.me)**

This policy describes what this instance actually does. If you run your own copy
of Optimus, you are your own operator and need your own policy — start from
[`privacy-policy-template.md`](privacy-policy-template.md).

## What the Bot does

When an image is posted in a server that has added the Bot, the Bot fetches the
image, computes **perceptual fingerprints** (compact numeric hashes) from it in
memory, and compares those fingerprints against the server's blocklist and — if
the server has opted in — a shared global list of scam fingerprints.

If nothing matches, processing ends there and **nothing is written**. If
something matches, the Bot either posts a review card for the server's
moderators or applies the moderation action the server's administrators
configured, and records that a detection occurred.

The Bot uses Discord's **Message Content** privileged intent so it can see image
attachments. It only inspects messages in servers where a server administrator
has installed it. **It does not read, scan, or store direct messages or private
conversations.**

## What is never stored

- **Raw images.** Image bytes exist only transiently in memory while
  fingerprints are computed, then are discarded. They are never written to disk
  or to our database. The codebase contains an optional evidence-storage
  subsystem, but it is **not wired into the processing pipeline and stores
  nothing** in this or any current build.
- **Clean images.** If a posted image does not match anything, no record of it
  is created — not even a note that it was checked. Only flagged images produce
  records.
- **Message text, usernames, avatars, or DM content.** Message content is read
  only to locate image attachments and is not retained.

## What is stored

### If one of your posts is flagged

| Data | Detail |
| --- | --- |
| Discord IDs | Server, channel, message, attachment, and your user ID |
| Match data | The verdict, per-algorithm distances, and the fingerprints of the flagged image |
| Outcome | The moderation action taken, whether it succeeded, and a short diagnostic note |
| Timestamp | When the detection occurred |

### If you are a moderator

- **Audit records** of moderation actions you take: your user ID, the action,
  the target, and a timestamp.
- **Trusted-user grants** a moderator issues (the exempted user's ID).
- **Global-list contributions**, if your server is approved to contribute: your
  user ID recorded as a submitter or approver, alongside contribution counters
  (submitted / confirmed / rejected). These are shared with other servers using
  the global list.

### Server-level

Per-server configuration chosen by administrators — sensitivity, action policy,
locale, retention window, ignored channels and roles, opt-ins — plus aggregate
detection counts.

Image fingerprints on the blocklist identify an **image**, not a person, and are
not linked to whoever posted it. Fingerprints cannot be used to reconstruct the
original image.

We do not sell data, and we do not share it with third parties for advertising
or any purpose unrelated to scam detection.

## How long it is kept

Detection records, appeal records, and moderation audit records are
**automatically deleted after 30 days** by a scheduled job. A server
administrator may configure a different window for their own server.

Global-list contribution records (submitter and approver IDs, reputation
counters) are **retained indefinitely**, because they are what makes a
cross-server trust signal meaningful. Server configuration is retained until the
server's administrators change it or delete the server's data.

## Why we process this, and the absence of an opt-out

We process this data to protect servers from scam and phishing images, at the
direction of the administrators who installed the Bot. Preventing fraud is a
recognised legitimate interest under
[GDPR Recital 47](https://gdpr-info.eu/recitals/no-47/).

**There is deliberately no way for a member to opt out of being scanned.** Images
covered by this policy are posted publicly, in a server whose operator chose to
run the Bot, and are visible to everyone else in that channel. A member-triggered
exemption would not be a privacy control — it would let anyone grant themselves
immunity from scam detection and then post scam images freely. Only a server's
own moderators can exempt someone, by marking them as a trusted user.

Under [GDPR Article 21](https://gdpr-info.eu/art-21-gdpr/) an objection to
processing may be refused where the controller demonstrates compelling
legitimate grounds. We consider scam detection on public server posts to be such
a ground, and will say so if you object. We will still tell you what we hold and
answer your request.

## Requesting deletion or a copy of your data

Email **[optimus.privacy@proton.me](mailto:optimus.privacy@proton.me)** with your
Discord user ID.

Realistically, most requests have nothing to act on. If you have never had a post
flagged, **we hold no data about you at all**, and that is what we will tell you.
If you have, the records are limited to those listed above and are deleted
automatically within 30 days regardless.

We will respond within **one month**, as required by
[GDPR Article 12(3)](https://gdpr-info.eu/art-12-gdpr/), and will tell you if we
need to extend that period. Where we decline to erase something, we will explain
which exemption applies — for example
[Article 17(3)(e)](https://www.legislation.gov.uk/eur/2016/679/article/17), for
records needed to establish or defend legal claims, or an ongoing enforcement
record in a server where you were actioned.

Requests to delete the Bot's data for an entire server should come from that
server's administrators, who can also run `/delete_server_data` themselves for an
immediate and complete wipe, or remove the Bot at any time.

Content you posted on Discord itself is controlled by Discord and by the server's
moderators, not by us — deleting our records does not delete your Discord
messages, and we cannot delete them for you.

## Changes to this policy

We may update this policy; the "Last updated" date above reflects the current
version. Material changes will be noted in the
[project repository](https://github.com/rafs2006/optimus).
