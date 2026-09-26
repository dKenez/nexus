"""Runtime configuration, read from the environment."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    PROD = "prod"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    # Blank values (e.g. `DISCORD_GUILD_ID=` in .env) mean "unset", not "invalid".
    model_config = SettingsConfigDict(env_file=None, extra="ignore", env_ignore_empty=True)

    # --- app ---
    nexus_env: Environment = Environment.DEV
    nexus_api_token: SecretStr
    database_url: str = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    recipes_dir: Path = Path("/app/recipes")
    backup_dir: Path = Path("/backups")
    backup_retention: int = Field(default=10, ge=1)
    reconcile_interval: int = Field(default=60, ge=5, description="seconds")
    # Hetzner bills each server per started hour of its life, so an empty host is kept until
    # this many seconds before its paid hour runs out (a restart meanwhile reuses it for free).
    host_billing_margin: int = Field(default=300, ge=60, le=1800)

    # --- hetzner ---
    hcloud_token: SecretStr
    hcloud_primary_ip: str = Field(description="name, id or address of the static Primary IP")
    # Dev-only guard: refuse to run if this Primary IP (name, id or address) is visible to the
    # token, i.e. if the token belongs to the production project.
    hcloud_forbidden_primary_ip: str | None = None
    hcloud_server_type: str = "cx32"
    hcloud_image: str = "docker-ce"
    hcloud_ssh_key_name: str = "nexus"
    hcloud_firewall_name: str = "nexus-host"
    host_memory_reserve_mb: int = 1024
    ssh_allowed_cidrs: Annotated[list[str], NoDecode] = ["0.0.0.0/0", "::/0"]

    # --- ssh ---
    ssh_client_key: SecretStr = Field(description="OpenSSH ed25519 private key nexus logs in with")
    ssh_host_key: SecretStr = Field(description="OpenSSH ed25519 host key installed on every VM")
    ssh_user: str = "nexus"
    ssh_connect_timeout: int = 10

    # --- public addressing ---
    public_hostname: str | None = None

    # --- recipe image registry (optional; images are public by default) ---
    ghcr_pull_user: str | None = None
    ghcr_pull_token: SecretStr | None = None

    # --- discord (bot is disabled when no token is set) ---
    discord_token: SecretStr | None = None
    discord_guild_id: int | None = None
    discord_notify_channel_id: int | None = None
    discord_roles_viewer: Annotated[list[int], NoDecode] = []
    discord_roles_operator: Annotated[list[int], NoDecode] = []
    discord_roles_admin: Annotated[list[int], NoDecode] = []

    @field_validator(
        "ssh_allowed_cidrs",
        "discord_roles_viewer",
        "discord_roles_operator",
        "discord_roles_admin",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("nexus_api_token")
    @classmethod
    def _token_not_empty(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("NEXUS_API_TOKEN must be at least 16 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
