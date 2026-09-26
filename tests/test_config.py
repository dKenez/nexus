import pytest
from pydantic import ValidationError

from nexus.config import Environment, Settings

BASE = {
    "NEXUS_API_TOKEN": "x" * 32,
    "HCLOUD_TOKEN": "token",
    "HCLOUD_PRIMARY_IP": "nexus-dev",
    "SSH_CLIENT_KEY": "k",
    "SSH_HOST_KEY": "k",
}


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {**BASE, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch)
    assert s.nexus_env is Environment.DEV
    assert s.discord_token is None
    assert s.ssh_allowed_cidrs == ["0.0.0.0/0", "::/0"]


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(
        monkeypatch,
        DISCORD_ROLES_ADMIN="1, 2",
        DISCORD_ROLES_OPERATOR="3",
        SSH_ALLOWED_CIDRS="198.51.100.7/32",
    )
    assert s.discord_roles_admin == [1, 2]
    assert s.discord_roles_operator == [3]
    assert s.discord_roles_viewer == []
    assert s.ssh_allowed_cidrs == ["198.51.100.7/32"]


def test_short_api_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        settings(monkeypatch, NEXUS_API_TOKEN="short")


def test_api_token_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in BASE.items():
        if key != "NEXUS_API_TOKEN":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_blank_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, DISCORD_GUILD_ID="", DISCORD_TOKEN="", DISCORD_ROLES_ADMIN="")
    assert s.discord_guild_id is None
    assert s.discord_token is None
    assert s.discord_roles_admin == []


def test_tests_do_not_see_the_real_environment() -> None:
    import os

    assert "DISCORD_TOKEN" not in os.environ
    assert "HCLOUD_TOKEN" not in os.environ
