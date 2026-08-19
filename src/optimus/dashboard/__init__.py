"""Read-only web dashboard for moderators and the deployment owner.

Mounted onto the existing health/metrics aiohttp server (simple mode), behind
Discord OAuth2 login. Phase 1 is deliberately read-only: every moderation
*action* stays in Discord (review-channel buttons and slash commands); the
dashboard only makes the bot's existing records browsable.
"""
