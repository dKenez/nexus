from typing import Annotated

import typer
from fastapi import APIRouter, Query
from typer import Typer

import nexus.portal_room as portal_room
from nexus.spell import Spell
from nexus.utils.print_output import print_output

cli_app = Typer()
api_router = APIRouter()

SpellArg = Annotated[Spell, typer.Argument(parser=Spell.parse)]


@cli_app.command(name="list")
@api_router.get(path="/list")
@print_output
def list_portals():
    portals = portal_room.list_portals()
    formatted_portals = [portal.jsonify() for portal in portals]
    return formatted_portals


@cli_app.command(name="get")
@api_router.get(path="/get")
@print_output
def get_portal(name: str):
    portal = portal_room.get_portal(name)
    formatted_portal = portal.jsonify()
    return formatted_portal


@cli_app.command(name="prepare")
@api_router.post(path="/prepare")
@print_output
def prepare_portal(spell_arg: Annotated[str, Query(alias="spell")], name: str):
    spell = Spell.parse(spell_arg)
    return portal_room.prepare_portal(spell, name)


@cli_app.command(name="create")
@api_router.post(path="/create")
@print_output
def create_portal(
    spell_arg: Annotated[str, Query(alias="spell")],
    name: str,
    start: Annotated[bool, typer.Option("--start")] = False,
):
    spell = Spell.parse(spell_arg)
    created_container = portal_room.create_portal(spell, name, start)
    return {
        "name": created_container.name,
        "short_id": created_container.short_id,
        "status": created_container.status,
        "image": {
            "tags": created_container.image.tags,
            "short_id": created_container.image.short_id,
        },
    }


@cli_app.command(name="cast")
@api_router.post(path="/cast")
@print_output
def cast_portal(spell_arg: Annotated[str, Query(alias="spell")], name: str):
    spell = Spell.parse(spell_arg)
    p, created_container = portal_room.cast_portal(spell, name)
    return {
        "path": p,
        "container": {
            "name": created_container.name,
            "short_id": created_container.short_id,
            "status": created_container.status,
            "image": {
                "tags": created_container.image.tags,
                "short_id": created_container.image.short_id,
            },
        },
    }


@cli_app.command(name="open")
@api_router.post(path="/open")
@print_output
def open_portal(name: str):
    return portal_room.open_portal(name)


@cli_app.command(name="reopen")
@api_router.post(path="/reopen")
@print_output
def reopen_portal(name: str):
    return portal_room.reopen_portal(name)


@cli_app.command(name="close")
@api_router.post(path="/close")
@print_output
def close_portal(name: str):
    return portal_room.close_portal(name)


@cli_app.command(name="remove")
@api_router.post(path="/remove")
@print_output
def remove_portal(name: str, force: Annotated[bool, typer.Option("--force")] = False):
    return portal_room.remove_portal(name, force=force)


@cli_app.command(name="destroy")
@api_router.post(path="/destroy")
@print_output
def destroy_portal(name: str, force: Annotated[bool, typer.Option("--force")] = False):
    return portal_room.destroy_portal(name, force=force)
