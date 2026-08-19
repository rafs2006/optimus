"""Unit tests for dashboard cookie signing and session parsing."""

from __future__ import annotations

import time

from optimus.dashboard.auth import (
    PERM_ADMINISTRATOR,
    PERM_MANAGE_GUILD,
    DashboardSession,
    can_moderate,
    sign_payload,
    verify_payload,
)

SECRET = "unit-test-secret-key-0123456789abcdef-0123456789"


class TestSignedPayloads:
    def test_roundtrip(self) -> None:
        payload = {"uid": 42, "exp": time.time() + 60}
        token = sign_payload(payload, secret=SECRET)
        out = verify_payload(token, secret=SECRET)
        assert out is not None
        assert out["uid"] == 42

    def test_tampered_body_rejected(self) -> None:
        token = sign_payload({"uid": 1, "exp": time.time() + 60}, secret=SECRET)
        body, _, sig = token.partition(".")
        # Flip a character in the body while keeping the old signature.
        forged_body = ("A" if body[0] != "A" else "B") + body[1:]
        assert verify_payload(f"{forged_body}.{sig}", secret=SECRET) is None

    def test_wrong_secret_rejected(self) -> None:
        token = sign_payload({"uid": 1, "exp": time.time() + 60}, secret=SECRET)
        assert verify_payload(token, secret=SECRET + "x") is None

    def test_expired_rejected(self) -> None:
        token = sign_payload({"uid": 1, "exp": time.time() - 1}, secret=SECRET)
        assert verify_payload(token, secret=SECRET) is None

    def test_missing_exp_rejected(self) -> None:
        token = sign_payload({"uid": 1}, secret=SECRET)
        assert verify_payload(token, secret=SECRET) is None

    def test_garbage_tokens_rejected(self) -> None:
        for garbage in ("", "no-dot", "a.b", "!!!.???", "a." + "b" * 100):
            assert verify_payload(garbage, secret=SECRET) is None


class TestDashboardSession:
    def _session(self, **overrides: object) -> DashboardSession:
        defaults: dict[str, object] = {
            "user_id": 7,
            "username": "mod",
            "guilds": {101: "Alpha", 202: "Beta"},
            "is_owner": False,
            "expires_at": time.time() + 3600,
        }
        defaults.update(overrides)
        return DashboardSession(**defaults)  # type: ignore[arg-type]

    def test_cookie_roundtrip(self) -> None:
        session = self._session()
        token = session.to_token(secret=SECRET)
        out = DashboardSession.from_token(token, secret=SECRET)
        assert out is not None
        assert out.user_id == 7
        assert out.guilds == {101: "Alpha", 202: "Beta"}
        assert out.is_owner is False

    def test_expired_cookie_rejected(self) -> None:
        token = self._session(expires_at=time.time() - 5).to_token(secret=SECRET)
        assert DashboardSession.from_token(token, secret=SECRET) is None

    def test_guild_visibility(self) -> None:
        session = self._session()
        assert session.can_view_guild(101)
        assert not session.can_view_guild(999)

    def test_owner_sees_everything(self) -> None:
        session = self._session(is_owner=True, guilds={})
        assert session.can_view_guild(101)
        assert session.can_view_guild(999)


class TestPermissionMask:
    def test_manage_guild_grants(self) -> None:
        assert can_moderate(PERM_MANAGE_GUILD)

    def test_administrator_grants(self) -> None:
        assert can_moderate(PERM_ADMINISTRATOR)

    def test_plain_member_denied(self) -> None:
        # SEND_MESSAGES | VIEW_CHANNEL style bits only.
        assert not can_moderate((1 << 10) | (1 << 11))
        assert not can_moderate(0)
