import os
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


def create_portal(spell: Spell, name: str, start=False):
    portal_path = nexus.paths.portals_dir / name

    image = spell.image
    network = "host"

    uid = os.getuid()
    gid = os.getgid()

    with open(spell.recipe / "properties.toml", "rb") as f:
        properties = tomllib.load(f)

    volumes = [
        str(portal_path / "server_data" / volume) for volume in properties["volumes"]
    ]

    if start:
        client.containers.run(
            image=spell_book.nexus_spell_name(image),
            name=f"nexus-{name}",
            network=network,
            volumes=volumes,
            user=f"{uid}:{gid}",
        )
    else:
        client.containers.create(
            image=spell_book.nexus_spell_name(image),
            name=f"nexus-{name}",
            network=network,
            volumes=volumes,
            user=f"{uid}:{gid}",
        )


def cast_portal(cls, spell: Spell, name: str):
    prepare_portal(spell, name)
    create_portal(spell, name)


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


def destroy_portal(name: str, force=False):
    remove_portal(name, force)

    portal_path = nexus.paths.portals_dir / name
    shutil.rmtree(portal_path)


if __name__ == "__main__":
    ppmc_spell = spell_book.get_spell("papermc")
    # prepare_portal(ppmc_spell, "testmc")

    # create_portal(ppmc_spell, "testmc", start=True)
    # close_portal("testmc")
    destroy_portal("testmc", True)
    # print(get_portal("testmc").container.status)
    # remove_portal("testmc")
    # shutil.rmtree(nexus.paths.portals_dir / "testmc")
