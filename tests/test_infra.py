import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import pytest
import yaml

from nexus.core.recipes import Port
from nexus.infra.agent import AgentError, docker_run_command, parse_ps, render_env_file
from nexus.infra.backups import BackupError, BackupStore
from nexus.infra.cloudinit import ENV_DIR, READY_MARKER, HostKeys, render_user_data
from nexus.infra.hetzner import HcloudGateway, firewall_rules
from tests.conftest import recipe

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _key() -> str:
    return asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()


# --- agent helpers ---


def test_docker_run_command() -> None:
    args = shlex.split(docker_run_command(recipe("alpha")))
    assert args[:3] == ["docker", "run", "--detach"]
    assert "nexus-alpha" in args
    assert "2456:2456/udp" in args and "2457:2457/udp" in args
    assert "/srv/nexus/games/alpha/data:/data" in args
    assert f"{ENV_DIR}/alpha.env" in args
    assert args[-1] == "ghcr.io/test/alpha:1"


def test_env_file_rejects_newlines() -> None:
    assert render_env_file({"B": "2", "A": "x y"}) == "A=x y\nB=2\n"
    with pytest.raises(AgentError):
        render_env_file({"A": "line\nbreak"})


def test_parse_ps() -> None:
    lines = [
        {
            "Names": "nexus-alpha",
            "State": "running",
            "Status": "Up 3m",
            "Labels": "nexus.game=alpha,x=y",
        },
        {"Names": "nexus-beta", "State": "exited", "Status": "Exited", "Labels": "nexus.game=beta"},
        {"Names": "other", "State": "running", "Status": "Up", "Labels": ""},
    ]
    containers = parse_ps("\n".join(json.dumps(line) for line in lines))
    assert set(containers) == {"alpha", "beta"}
    assert containers["alpha"].running
    assert not containers["beta"].running


# --- cloud-init ---


def test_user_data_is_valid_cloud_config() -> None:
    keys = HostKeys(client_private=_key(), host_private=_key())
    user_data = render_user_data(keys=keys, ssh_user="nexus")
    assert user_data.startswith("#cloud-config\n")
    doc = yaml.safe_load(user_data)
    assert doc["users"][0]["ssh_authorized_keys"] == [keys.client_public]
    assert doc["ssh_keys"]["ed25519_public"] == keys.host_public
    assert asyncssh.import_private_key(doc["ssh_keys"]["ed25519_private"])
    assert ["touch", READY_MARKER] in doc["runcmd"]
    assert "docker login" not in user_data


def test_user_data_registry_login() -> None:
    keys = HostKeys(client_private=_key(), host_private=_key())
    user_data = render_user_data(
        keys=keys, ssh_user="nexus", registry_user="bot", registry_token="ghp_x"
    )
    assert "docker login ghcr.io -u 'bot'" in user_data
    yaml.safe_load(user_data)


# --- firewall ---


def test_firewall_rules() -> None:
    rules = firewall_rules(
        [
            Port(port=2457, protocol="udp"),
            Port(port=2456, protocol="udp"),
            Port(port=2456, protocol="udp"),
        ],
        ["198.51.100.0/24"],
    )
    assert [(r.protocol, r.port) for r in rules] == [
        ("tcp", "22"),
        ("udp", "2456"),
        ("udp", "2457"),
    ]
    assert rules[0].source_ips == ["198.51.100.0/24"]


# --- hetzner scoping ---


def _server(labels: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(labels=labels)


def test_gateway_only_claims_its_own_env() -> None:
    gateway = HcloudGateway(
        token="x",
        env="dev",
        primary_ip="nexus-dev",
        server_type="cx32",
        image="docker-ce",
        ssh_key_name="nexus",
        firewall_name="nexus-host",
        ssh_cidrs=["0.0.0.0/0"],
    )
    assert gateway.selector == "managed-by=nexus,nexus-env=dev"
    assert gateway._is_ours(_server({"managed-by": "nexus", "nexus-env": "dev"}))  # ty: ignore[invalid-argument-type]
    assert not gateway._is_ours(_server({"managed-by": "nexus", "nexus-env": "prod"}))  # ty: ignore[invalid-argument-type]
    assert not gateway._is_ours(_server({"managed-by": "nexus"}))  # ty: ignore[invalid-argument-type]
    assert not gateway._is_ours(_server({}))  # ty: ignore[invalid-argument-type]


async def test_dev_guard_refuses_prod_primary_ip() -> None:
    from nexus.infra.hetzner import UnsafeEnvironmentError

    gateway = HcloudGateway(
        token="x",
        env="dev",
        primary_ip="nexus-dev",
        server_type="cx32",
        image="docker-ce",
        ssh_key_name="nexus",
        firewall_name="nexus-host",
        ssh_cidrs=[],
        forbidden_primary_ip="nexus-prod",
    )
    visible = {"nexus-prod": SimpleNamespace(name="nexus-prod")}
    gateway._client = SimpleNamespace(  # ty: ignore[invalid-assignment]
        primary_ips=SimpleNamespace(get_by_name=visible.get)
    )
    with pytest.raises(UnsafeEnvironmentError):
        await gateway.check_environment()


# --- backups ---


async def test_backup_write_is_atomic(tmp_path: Path) -> None:
    store = BackupStore(tmp_path)

    async def produce(sink):
        await sink(b"abc")
        await sink(b"def")

    written = await store.write("alpha", "manual", produce, now=NOW)
    assert written.filename == "20260926T120000Z-manual.tar.zst"
    assert written.bytes == 6
    assert (tmp_path / "alpha" / written.filename).read_bytes() == b"abcdef"
    assert b"".join([c async for c in store.read("alpha", written.filename)]) == b"abcdef"


async def test_failed_backup_leaves_no_files(tmp_path: Path) -> None:
    store = BackupStore(tmp_path)

    async def produce(sink):
        await sink(b"partial")
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError):
        await store.write("alpha", "idle", produce, now=NOW)
    assert list((tmp_path / "alpha").iterdir()) == []


async def test_empty_backup_is_rejected(tmp_path: Path) -> None:
    async def produce(sink):
        return None

    with pytest.raises(BackupError):
        await BackupStore(tmp_path).write("alpha", "idle", produce, now=NOW)


def test_backup_paths_are_validated(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        BackupStore(tmp_path).path("alpha", "../../etc/passwd")
