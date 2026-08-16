"""Access control for the Slack bot (PLAN.md 17.4).

app_mention is open to everyone in the workspace, external guests included, so
these checks are the gate in front of the knowledge base.
"""

from __future__ import annotations

import bot
import config


class FakeClient:
    def __init__(self, user: dict | None = None, raises: bool = False):
        self._user = user or {}
        self._raises = raises

    def users_info(self, user: str):  # noqa: ARG002 - matches the Slack SDK signature
        if self._raises:
            raise RuntimeError("network down")
        return {"user": self._user}


def test_all_channels_allowed_when_list_empty(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_SLACK_CHANNELS", [])
    assert bot.is_allowed_channel("C_ANYTHING") is True


def test_channel_allowlist_blocks_others(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_SLACK_CHANNELS", ["C_OK"])
    assert bot.is_allowed_channel("C_OK") is True
    assert bot.is_allowed_channel("C_OTHER") is False


def test_regular_member_allowed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_GUEST_USERS", False)
    client = FakeClient({"is_restricted": False, "is_ultra_restricted": False})
    assert bot.is_allowed_user(client, "U1") is True


def test_single_channel_guest_refused(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_GUEST_USERS", False)
    client = FakeClient({"is_ultra_restricted": True})
    assert bot.is_allowed_user(client, "U2") is False


def test_multi_channel_guest_refused(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_GUEST_USERS", False)
    client = FakeClient({"is_restricted": True})
    assert bot.is_allowed_user(client, "U3") is False


def test_lookup_failure_fails_closed(monkeypatch):
    """A users.info error must deny, not allow."""
    monkeypatch.setattr(config, "ALLOW_GUEST_USERS", False)
    assert bot.is_allowed_user(FakeClient(raises=True), "U4") is False


def test_guests_allowed_when_opted_in(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_GUEST_USERS", True)
    client = FakeClient({"is_ultra_restricted": True})
    assert bot.is_allowed_user(client, "U5") is True


def test_mention_is_stripped_from_question():
    assert bot._MENTION_RE.sub("", "<@U012AB3CD> 会場はどこ").strip() == "会場はどこ"
