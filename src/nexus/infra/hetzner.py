"""Hetzner Cloud access.

Everything nexus does to Hetzner goes through ``HetznerGateway``. The real implementation is
scoped: it only ever sees servers labelled ``managed-by=nexus`` *and* ``nexus-env=<env>``, only
touches the one configured Primary IP, and only manages the one nexus firewall. Nothing else in
the project is listed, adopted or deleted.
"""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from hcloud import Client
from hcloud.firewalls import FirewallRule
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.primary_ips import BoundPrimaryIP
from hcloud.server_types import ServerType
from hcloud.servers import BoundServer, ServerCreatePublicNetwork

from nexus.core.recipes import Port

log = logging.getLogger(__name__)

MANAGED_BY = "managed-by"
MANAGED_BY_VALUE = "nexus"
ENV_LABEL = "nexus-env"


class HetznerError(Exception):
    pass


class UnsafeEnvironmentError(HetznerError):
    """The configured token can see resources it must never touch (e.g. prod from dev)."""


@dataclass(frozen=True)
class PrimaryIPInfo:
    id: int
    ip: str
    location: str
    assignee_id: int | None


@dataclass(frozen=True)
class ServerInfo:
    id: int
    name: str
    status: str
    ip: str | None
    server_type: str
    labels: dict[str, str]


def _must[T](value: T | None, what: str) -> T:
    """hcloud models every field as optional; the API always returns these ones."""
    if value is None:
        raise HetznerError(f"hetzner response is missing {what}")
    return value


class HetznerGateway(Protocol):
    async def check_environment(self) -> None: ...
    async def primary_ip(self) -> PrimaryIPInfo: ...
    async def list_servers(self) -> list[ServerInfo]: ...
    async def get_server(self, server_id: int) -> ServerInfo | None: ...
    async def create_server(self, name: str, user_data: str) -> ServerInfo: ...
    async def delete_server(self, server_id: int) -> None: ...
    async def server_memory_mb(self) -> int: ...
    async def hourly_price(self) -> str | None: ...
    async def ensure_ssh_key(self, public_key: str) -> None: ...
    async def set_firewall(self, ports: Iterable[Port]) -> None: ...


def firewall_rules(ports: Iterable[Port], ssh_cidrs: list[str]) -> list[FirewallRule]:
    rules = [
        FirewallRule(
            direction=FirewallRule.DIRECTION_IN,
            protocol=FirewallRule.PROTOCOL_TCP,
            port="22",
            source_ips=ssh_cidrs,
            description="nexus ssh",
        )
    ]
    for port in sorted(set(ports), key=lambda p: (p.port, p.protocol)):
        rules.append(
            FirewallRule(
                direction=FirewallRule.DIRECTION_IN,
                protocol=port.protocol,
                port=str(port.port),
                source_ips=["0.0.0.0/0", "::/0"],
                description=f"game {port}",
            )
        )
    return rules


class HcloudGateway:
    """``HetznerGateway`` over the synchronous ``hcloud`` client, run in worker threads."""

    def __init__(
        self,
        *,
        token: str,
        env: str,
        primary_ip: str,
        server_type: str,
        image: str,
        ssh_key_name: str,
        firewall_name: str,
        ssh_cidrs: list[str],
        forbidden_primary_ip: str | None = None,
    ) -> None:
        self._client = Client(token=token, application_name="nexus")
        self._env = env
        self._primary_ip = primary_ip
        self._server_type = server_type
        self._image = image
        self._ssh_key_name = ssh_key_name
        self._firewall_name = firewall_name
        self._ssh_cidrs = ssh_cidrs
        self._forbidden_primary_ip = forbidden_primary_ip

    @property
    def labels(self) -> dict[str, str]:
        return {MANAGED_BY: MANAGED_BY_VALUE, ENV_LABEL: self._env}

    @property
    def selector(self) -> str:
        return ",".join(f"{k}={v}" for k, v in self.labels.items())

    # --- helpers (sync, run in threads) ---

    def _get_primary_ip(self) -> BoundPrimaryIP:
        ref = self._primary_ip
        ip = (
            self._client.primary_ips.get_by_id(int(ref))
            if ref.isdigit()
            else self._client.primary_ips.get_by_name(ref)
        )
        if ip is None:
            raise HetznerError(f"primary IP {ref!r} not found in this project")
        return ip

    @staticmethod
    def _location_of(ip: BoundPrimaryIP) -> str:
        return _must(_must(ip.location, "primary ip location").name, "location name")

    def _is_ours(self, server: BoundServer) -> bool:
        labels = server.labels or {}
        return all(labels.get(k) == v for k, v in self.labels.items())

    @staticmethod
    def _info(server: BoundServer) -> ServerInfo:
        ipv4 = server.public_net.ipv4 if server.public_net else None
        return ServerInfo(
            id=_must(server.id, "server id"),
            name=_must(server.name, "server name"),
            status=_must(server.status, "server status"),
            ip=ipv4.ip if ipv4 else None,
            server_type=_must(_must(server.server_type, "server type").name, "server type name"),
            labels=dict(server.labels or {}),
        )

    # --- gateway API ---

    async def check_environment(self) -> None:
        def check() -> None:
            forbidden = self._forbidden_primary_ip
            if forbidden and self._client.primary_ips.get_by_name(forbidden) is not None:
                raise UnsafeEnvironmentError(
                    f"HCLOUD_TOKEN can see primary IP {forbidden!r}, which is forbidden in "
                    f"env {self._env!r}. Use the token of the Hetzner dev project."
                )
            ip = self._get_primary_ip()
            if ip.auto_delete:
                raise HetznerError(
                    f"primary IP {ip.name!r} has auto_delete enabled; deleting a host would "
                    "release it. Disable auto-delete in the Hetzner console."
                )

        await asyncio.to_thread(check)

    async def primary_ip(self) -> PrimaryIPInfo:
        def get() -> PrimaryIPInfo:
            ip = self._get_primary_ip()
            return PrimaryIPInfo(
                id=_must(ip.id, "primary ip id"),
                ip=_must(ip.ip, "primary ip address"),
                location=self._location_of(ip),
                assignee_id=ip.assignee_id,
            )

        return await asyncio.to_thread(get)

    async def list_servers(self) -> list[ServerInfo]:
        def get() -> list[ServerInfo]:
            servers = self._client.servers.get_all(label_selector=self.selector)
            # Double-check the labels client-side; never trust the filter alone.
            return [self._info(s) for s in servers if self._is_ours(s)]

        return await asyncio.to_thread(get)

    async def get_server(self, server_id: int) -> ServerInfo | None:
        def get() -> ServerInfo | None:
            server = self._client.servers.get_by_id(server_id)
            if server is None or not self._is_ours(server):
                return None
            return self._info(server)

        try:
            return await asyncio.to_thread(get)
        except Exception as exc:  # hcloud raises APIException(not_found)
            if getattr(exc, "code", None) == "not_found":
                return None
            raise

    async def create_server(self, name: str, user_data: str) -> ServerInfo:
        def create() -> ServerInfo:
            ip = self._get_primary_ip()
            if ip.assignee_id is not None:
                raise HetznerError(
                    f"primary IP {ip.name!r} is still assigned to server {ip.assignee_id}"
                )
            firewall = self._client.firewalls.get_by_name(self._firewall_name)
            ssh_key = self._client.ssh_keys.get_by_name(self._ssh_key_name)
            response = self._client.servers.create(
                name=name,
                server_type=ServerType(name=self._server_type),
                image=Image(name=self._image),
                location=Location(name=self._location_of(ip)),
                ssh_keys=[ssh_key] if ssh_key else None,
                firewalls=[firewall] if firewall else None,
                user_data=user_data,
                labels=self.labels,
                public_net=ServerCreatePublicNetwork(ipv4=ip, enable_ipv4=True, enable_ipv6=False),
            )
            response.action.wait_until_finished()
            for action in response.next_actions or []:
                action.wait_until_finished()
            server = self._client.servers.get_by_id(_must(response.server.id, "server id"))
            return self._info(server)

        log.info("creating server %s (%s)", name, self._server_type)
        return await asyncio.to_thread(create)

    async def delete_server(self, server_id: int) -> None:
        def delete() -> None:
            server = self._client.servers.get_by_id(server_id)
            if not self._is_ours(server):
                raise HetznerError(f"refusing to delete server {server_id}: not managed by nexus")
            self._client.servers.delete(server).wait_until_finished()

        log.info("deleting server %s", server_id)
        await asyncio.to_thread(delete)

    async def server_memory_mb(self) -> int:
        def get() -> int:
            server_type = self._client.server_types.get_by_name(self._server_type)
            if server_type is None or server_type.memory is None:
                raise HetznerError(f"unknown server type {self._server_type!r}")
            return int(float(server_type.memory) * 1024)

        return await asyncio.to_thread(get)

    async def hourly_price(self) -> str | None:
        def get() -> str | None:
            server_type = self._client.server_types.get_by_name(self._server_type)
            location = self._location_of(self._get_primary_ip())
            for price in (server_type.prices if server_type else None) or []:
                if price.get("location") == location:
                    return price["price_hourly"]["gross"]
            return None

        return await asyncio.to_thread(get)

    async def ensure_ssh_key(self, public_key: str) -> None:
        def ensure() -> None:
            existing = self._client.ssh_keys.get_by_name(self._ssh_key_name)
            if existing is not None:
                if (existing.public_key or "").split()[:2] != public_key.split()[:2]:
                    raise HetznerError(
                        f"hcloud ssh key {self._ssh_key_name!r} exists with a different key"
                    )
                return
            self._client.ssh_keys.create(
                name=self._ssh_key_name, public_key=public_key, labels=self.labels
            )

        await asyncio.to_thread(ensure)

    async def set_firewall(self, ports: Iterable[Port]) -> None:
        rules = firewall_rules(ports, self._ssh_cidrs)

        def apply() -> None:
            firewall = self._client.firewalls.get_by_name(self._firewall_name)
            if firewall is None:
                self._client.firewalls.create(
                    name=self._firewall_name,
                    rules=rules,
                    labels=self.labels,  # ty: ignore[invalid-argument-type]  (hcloud mistypes it)
                )
                return
            for action in firewall.set_rules(rules):
                action.wait_until_finished()

        await asyncio.to_thread(apply)


__all__ = [
    "HcloudGateway",
    "HetznerError",
    "HetznerGateway",
    "PrimaryIPInfo",
    "ServerInfo",
    "UnsafeEnvironmentError",
]
