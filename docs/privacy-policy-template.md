# Privacy policy template

> **Template, not legal advice.** This is a starting point for operators who run
> their own optimus instance and need a privacy policy — notably for Discord's
> bot verification, which requires a published privacy policy once a bot using
> the **Message Content** privileged intent grows past ~75–100 servers. Fill in
> the **`[BRACKETED]`** placeholders, review with counsel, and host it at a
> stable public URL you can point Discord at.
>
> The technical claims below are accurate for upstream optimus as shipped. If you
> fork or change configuration (e.g. enable the optional evidence store), update
> the wording to match what your deployment actually does.

---

# Privacy Policy for [BOT NAME]

_Last updated: [DATE]_

[BOT NAME] ("the Bot", "we", "us") is a Discord moderation bot operated by
[OPERATOR NAME / ORGANISATION]. This policy explains what data the Bot processes,
what it stores, how long it keeps it, and how to request deletion.

## What the Bot does

[BOT NAME] detects known scam / malicious images posted in your server by
comparing posted images against a database of image **fingerprints** (perceptual
hashes). When a posted image matches a known-bad fingerprint, the Bot may take a
configured moderation action (e.g. delete the message, time out the uploader)
and record that a detection occurred.

To do this the Bot uses Discord's **Message Content** privileged intent so it can
see image attachments in messages.

## What we process but do NOT store

- **Raw images / attachment bytes.** When a user posts an image, the Bot fetches
  the image, computes fingerprints from it **in memory**, and discards the bytes.
  **Raw image content is never written to disk or to our database.** Image bytes
  exist only transiently in memory and on the internal message bus during
  processing.
- **Message text.** The Bot reads message content only to find image
  attachments. Message text is not stored.

## What we store

The Bot stores only fingerprints and moderation metadata — never the underlying
images:

- **Image fingerprints (perceptual hashes).** Compact numeric hashes (pHash,
  dHash, wHash, aHash) of known-bad images, **including hashes of mirrored
  (horizontally-flipped) variants** so trivially mirrored re-uploads are still
  caught. Optionally, a numeric embedding vector for ambiguous-match
  confirmation. These are derived from images but cannot reconstruct them.
- **Detection metadata.** For each detection: the Discord guild, channel,
  message, attachment, and uploader IDs; the match verdict and per-algorithm
  distances; the moderation action taken; and a timestamp. **No image is stored
  with a detection.**
- **Appeals.** If a user appeals a moderation action, the appeal record and its
  status/resolution.
- **Per-guild configuration.** Settings a server admin chooses (sensitivity,
  action policy, locale, opt-ins).

We do **not** sell data or share it with third parties for advertising.

### Optional evidence storage (off by default)

The Bot supports an **optional, off-by-default** evidence feature
(`OPTIMUS_EVIDENCE_ENABLED`). When an operator explicitly enables it, a copy of a
flagged image may be stored on the operator's own S3-compatible object storage,
**server-side encrypted**, with a **short time-to-live (default 1 hour, hard cap
24 hours)**, retrievable only via a short-lived presigned link, after which it is
automatically deleted. This is the **only** circumstance in which raw image bytes
are persisted, it is never enabled by default, and the storage is the operator's
own.

> **Operators:** delete this section if you do not enable the evidence store. If
> you do enable it, state where the storage is hosted and its region.

## Data retention

[BOT NAME] [does / does not] enforce automatic retention. Stored detection and
appeal records are retained for **[N] days** and then automatically purged in
batches by a scheduled job, configured via `OPTIMUS_DETECTION_RETENTION_DAYS`.

> **Operators:** if `OPTIMUS_DETECTION_RETENTION_DAYS` is unset, retention is
> disabled and records are kept indefinitely — say so honestly here and state how
> a user can request deletion instead. If set, fill in **[N]** with the value.

## Legal basis and use

We process this data to provide moderation functionality to servers that have
added the Bot, at the direction of those servers' administrators. Fingerprints
and detection metadata are used solely to detect and act on known-bad images and
to handle appeals.

## Data deletion and contact

To request deletion of data associated with you or your server, or for any
privacy question, contact:

- **Contact:** [EMAIL ADDRESS or SUPPORT SERVER INVITE]

Server administrators can remove the Bot from a server at any time. On request we
will delete the detection metadata and appeal records associated with a given
guild or user, subject to the retention behaviour described above.

## Changes to this policy

We may update this policy; the "Last updated" date reflects the latest version.
Material changes will be communicated via [CHANNEL — e.g. the support server].
