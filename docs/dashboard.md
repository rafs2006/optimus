# Web dashboard

Optimus ships an optional **read-only** web dashboard: a browser view of what
the bot has been doing, behind a "Log in with Discord" button. It is off by
default and adds no new port — when enabled it is served by the same built-in
web server that already answers health checks, under `/dash`.

Read-only means exactly that: the dashboard has no button that deletes,
bans, votes, or changes settings. All moderation actions stay in Discord
(review-channel buttons and slash commands), where they are permission-checked
and audit-logged by the existing paths.

## Who sees what

| Role | How it is determined | What they can open |
|---|---|---|
| Guild moderator | **Manage Server** (or Administrator) permission in a server the bot is installed in, per Discord OAuth | That server's pages: scan activity, detections, detection details, audit log |
| Deployment owner | Owner (or team member) of the Discord application, fetched from Discord | Everything: any guild page plus the global sections |

Logging in never grants the bot any power over the visitor's account — the
OAuth scopes are `identify guilds` (who you are, which servers you are in).
The access token is used once at login and discarded; the session is a signed
cookie that expires after `OPTIMUS_DASHBOARD_SESSION_TTL_SECONDS` (default
24 h).

## Pages

- `/dash` — your servers, and (for the owner) links to the global sections.
- `/dash/guild/<id>` — 30-day scan chart (clean vs flagged), verdict totals,
  and the detection log, filterable by verdict and uploader id, with paging.
- `/dash/guild/<id>/detection/<n>` — one detection in full: verdict, action
  taken, uploader/channel/message ids, match distances, perceptual hashes.
  Images themselves are never stored, so they are never displayed.
- `/dash/guild/<id>/audit` — the guild's audit log (config changes, review
  decisions, purges…).
- `/dash/global` — owner only: per-server activity rollup and global hash
  totals.
- `/dash/global/hashes?status=candidate|promoted|revoked` — owner only: the
  global hash database with approval progress per hash.
- `/dash/global/servers` — owner only: servers trusted to vote on the global
  database.

## Setup

1. **Discord developer portal** (
   [discord.com/developers/applications](https://discord.com/developers/applications)
   → your application → **OAuth2**):
   - Copy the **Client Secret**.
   - Under **Redirects**, add exactly:
     `https://<your-public-domain>/dash/callback`
2. **Environment** (Railway → your service → Variables, or `.env`):

   ```bash
   OPTIMUS_DASHBOARD_ENABLED=true
   OPTIMUS_DASHBOARD_BASE_URL=https://<your-public-domain>
   OPTIMUS_DASHBOARD_SESSION_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
   OPTIMUS_DISCORD_CLIENT_SECRET=<client secret from step 1>
   ```

   On Railway, `<your-public-domain>` is the service's public domain
   (Settings → Networking → Generate Domain if you have not already); the
   dashboard listens on the same port the health checks use, so no extra
   service or port configuration is needed.
3. Restart the bot and open `https://<your-public-domain>/dash`.

If any of the settings are missing the bot refuses to start with a one-line
message saying which variable to fix (simple mode), so a half-configured
dashboard can't silently ship a broken login.

## Notes

- The dashboard shows records; retention still applies — detections older
  than the guild's `retention_days` (default 30) are purged as before, so
  the dashboard cannot resurrect them.
- Session cookies are HMAC-signed with your `OPTIMUS_DASHBOARD_SESSION_SECRET`;
  rotating that value logs everyone out immediately.
- `/healthz`, `/readyz`, and `/metrics` remain unauthenticated exactly as
  before; every `/dash` page except the landing and login pages requires a
  valid session.
