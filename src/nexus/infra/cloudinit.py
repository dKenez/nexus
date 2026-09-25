"""Cloud-init user data for game hosts."""

from dataclasses import dataclass

import asyncssh
from jinja2 import Environment, PackageLoader, StrictUndefined

GAMES_DIR = "/srv/nexus/games"
READY_MARKER = "/var/lib/nexus/ready"
ENV_DIR = "/var/lib/nexus/env"

_env = Environment(
    loader=PackageLoader("nexus.infra", "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


def public_key_of(private_key: str) -> str:
    """The OpenSSH one-line public key for an OpenSSH private key."""
    key = asyncssh.import_private_key(private_key)
    return key.export_public_key("openssh").decode().strip()


@dataclass(frozen=True)
class HostKeys:
    client_private: str
    host_private: str

    @property
    def client_public(self) -> str:
        return public_key_of(self.client_private)

    @property
    def host_public(self) -> str:
        return public_key_of(self.host_private)


def render_user_data(
    *,
    keys: HostKeys,
    ssh_user: str,
    registry_user: str | None = None,
    registry_token: str | None = None,
) -> str:
    user_data = _env.get_template("cloud-init.yaml.j2").render(
        ssh_user=ssh_user,
        client_public_key=keys.client_public,
        host_private_key=keys.host_private.strip(),
        host_public_key=keys.host_public,
        games_dir=GAMES_DIR,
        ready_marker=READY_MARKER,
        env_dir=ENV_DIR,
        registry_user=registry_user or "",
        registry_token=registry_token or "",
    )
    if len(user_data.encode()) > 32 * 1024:
        raise ValueError("cloud-init user data exceeds Hetzner's 32 KiB limit")
    return user_data
