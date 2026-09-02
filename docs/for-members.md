# What Optimus does to your posts

This page is for ordinary members of a server that runs Optimus. Moderators can
link or pin it. Nothing here needs a command or a login to understand.

Optimus is a bot that catches **scam images** — fake giveaways, fake Nitro or
Steam gifts, faked exchange screenshots, wallet-drainer QR codes — before they
work on somebody. Images are the only thing it acts on.

## What it looks at

- **Images you upload or link**, in messages and in edits, in any channel the
  bot can see. That is the whole scope.
- **Not your text.** Discord hands the bot the whole message so it can find the
  attachments and links in it, but no message content is stored, logged, or
  shown to a moderator. Nothing is kept of what you wrote.
- **Not your DMs.** Optimus only sees channels inside the server.
- When the bot first joins a server it also takes a pass over recent history,
  because scam waves usually start before somebody installs a bot.

## What happens to an image

Each image is reduced to a short **fingerprint** — a few numbers that survive
cropping, recoloring, and re-compression — and compared against the list of scam
images this server's moderators have blocked. The image itself is not kept; only
the fingerprint. (A server can optionally turn on evidence copies for
moderators; that setting is **off** by default.)

Three things can happen:

| Outcome | What you see |
| --- | --- |
| No match | Nothing. This is almost every image. |
| Close match to a known scam | Depending on how the server is configured, the message may be deleted and a moderator notified. |
| Suspicious but unknown | Nothing automatic. A moderator is asked to look at it. The bot will not delete, mute, or ban on a guess. |

That last row matters: an image the bot has never seen before is **never**
acted on by itself. It is sent to humans. The bot is deliberately biased toward
doing nothing when it is unsure.

## If you get caught by mistake

Contact the server's moderators directly. There is no self-service appeal
command: the bot cannot offer one that works, because the members most likely to
need it have already been removed from the server and can no longer run commands
in it.

A moderator can reverse any action the bot took, including an unban, and can add
the image to a whitelist so it is never flagged again.

## If you spot a scam

Two ways, both open to everyone:

- **Right-click the message → Apps → *Report scam to mods***.
- **`/report <message>`** — paste the message link, or just its ID if it is in
  the channel you are typing in. Use this one if your app hides the right-click
  *Apps* menu, which mobile often does.

Either way it goes to the moderators for a decision. Reporting something does
not delete it, mute anyone, or ban anyone — that is a human's call. Reporting
the same message twice does not pile up; you will also be told to slow down if
you report a lot in a short time.

Report the message, not the person. If an account you know starts posting
giveaway images out of nowhere, it has very likely been stolen rather than
turned evil — reporting it fast is how its owner gets it back.

## Your data

- **What is kept:** image fingerprints, and the ids needed to act on a detection
  (which user, which channel, which message). Records are deleted on the
  server's retention schedule — 30 days unless its moderators changed it.
- **What is never kept:** your message text, and the images themselves. Nothing
  at all is recorded for an image that doesn't match — not even a note that it
  was checked. If you have never been flagged, there is no record of you.
- **There is no opt-out from scanning.** You are posting publicly in a server
  whose owners chose to run this bot, and an opt-out would just be a way to
  grant yourself immunity before posting a scam. Only a server's own moderators
  can exempt someone, by marking them as trusted.
- **To ask what is held about you, or to request erasure**, email
  [optimus.privacy@proton.me](mailto:optimus.privacy@proton.me) with your
  Discord user ID. See the [privacy policy](privacy-policy.md) for the full
  detail. Note that records are deleted automatically within 30 days anyway.
- If a server has a privacy policy from its host, it will say more about how
  they run the bot.

## Commands you can use

| Command | What it does |
| --- | --- |
| `/report <message>` | Send a message's image to the moderators. |
| `/help` | Short version of all of this, inside Discord. |

Everything else is restricted to moderators.

The server's host can choose to expose only some of these — most often
`/report` alone. If a command is not in your picker, it is not available here;
ask the moderators.
