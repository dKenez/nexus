import re
import shutil

import tomllib
from docker.models.containers import Container

import nexus
import nexus.spell_book as spell_book
from nexus.portal import Portal
from nexus.spell import Spell
from nexus.utils.docker import client

portal_format = re.compile(r"nexus-([a-zA-Z0-9]+)")


def extract_portal_name(container: Container):
    m = portal_format.search(container.name)

    if m:
        return str(m.group(1))

    return ""


def list_portals():
    containers = client.containers.list(all=True)

    portals: list[Portal] = []

    for container in containers:
        portal_name = extract_portal_name(container)

        if portal_name:
            portal = Portal(
                portal_name,
                container,
                nexus.paths.portals_dir / portal_name,
            )
            portals.append(portal)

    return portals


def get_portal(name: str):
    portals = list_portals()
    hits = [portal for portal in portals if portal.name == name]

    match len(hits):
        case 1:
            return hits[0]
        case 0:
            raise KeyError(f'Portal "{name}" not found')
        case _:
            raise KeyError(
                f'Portal "{name}" found {len(hits)} times. Expected 1 match.'
            )


def prepare_portal(spell: Spell, name: str):
    portal_path = nexus.paths.portals_dir / name
    portal_path.mkdir()

    shutil.copytree(
        nexus.paths.recipe_dir / spell.name / "server_data_template",
        portal_path / "server_data",
    )

    return portal_path


def create_portal(spell: Spell, name: str, start=False) -> Container:
    portal_path = nexus.paths.portals_dir / name

    image = spell.image
    network = "host"

    with open(spell.recipe / "properties.toml", "rb") as f:
        properties = tomllib.load(f)

    volumes = [
        str(portal_path / "server_data" / volume) for volume in properties["volumes"]
    ]

    if start:
        return client.containers.run(
            image=spell_book.nexus_spell_name(image),
            name=f"nexus-{name}",
            network=network,
            volumes=volumes,
            detach=True,
        )
    else:
        return client.containers.create(
            image=spell_book.nexus_spell_name(image),
            name=f"nexus-{name}",
            network=network,
            volumes=volumes,
        )


def cast_portal(spell: Spell, name: str):
    prepare = prepare_portal(spell, name)
    create = create_portal(spell, name)
    return prepare, create


def open_portal(name: str):
    portal = get_portal(name=name)
    portal.container.start()


def reopen_portal(name: str):
    portal = get_portal(name=name)
    portal.container.restart()


def close_portal(name: str):
    portal = get_portal(name=name)
    portal.container.stop()


def remove_portal(name: str, force=False):
    portal = get_portal(name=name)
    if force:
        close_portal(name)

    portal.container.remove()


# TODO: Fix permissions mess. Docker runs as root user, so all files created by it is owned by root, so managing it from the host machine is finnicky
def destroy_portal(name: str, force=False):
    remove_portal(name, force)

    portal_path = nexus.paths.portals_dir / name
    shutil.rmtree(portal_path)
