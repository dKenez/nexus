"""The bot must say clearly when it can't post notifications, not fail silently later."""

import logging
from types import SimpleNamespace

import discord
import pytest

from nexus.bot.client import NexusBot
from nexus.bot.permissions import RoleTiers
from tests.conftest import World

CHANNEL_ID = 1320838170014781460


def bot(world: World) -> NexusBot:
    return NexusBot(
        orchestrator=world.orch,
        role_tiers=RoleTiers.of([], [], []),
        guild_id=None,
        notify_channel_id=CHANNEL_ID,
    )


async def test_missing_access_is_reported_with_the_fix(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bot(world)

    async def forbidden(_: int) -> None:
        response = SimpleNamespace(status=403, reason="Forbidden")
        raise discord.Forbidden(response, "Missing Access")  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(b, "get_channel", lambda _: None)
    monkeypatch.setattr(b, "fetch_channel", forbidden)
    with caplog.at_level(logging.ERROR):
        await b._check_notify_channel()
    assert "allow the bot View Channel, Send Messages" in caplog.text


async def test_non_text_channel_is_reported(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bot(world)
    category = SimpleNamespace(type="category")  # not Messageable
    monkeypatch.setattr(b, "get_channel", lambda _: category)
    with caplog.at_level(logging.ERROR):
        await b._check_notify_channel()
    assert "can't hold messages" in caplog.text


async def test_ok_channel(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bot(world)

    class Channel(discord.abc.Messageable):
        name = "nexus"
        guild = SimpleNamespace(me=object())

        async def _get_channel(self):  # pragma: no cover - required abstract method
            return self

        def permissions_for(self, _: object) -> SimpleNamespace:
            return SimpleNamespace(send_messages=True)

    monkeypatch.setattr(b, "get_channel", lambda _: Channel())
    with caplog.at_level(logging.INFO):
        await b._check_notify_channel()
    assert "notifications go to #nexus" in caplog.text
    assert "ERROR" not in caplog.text
