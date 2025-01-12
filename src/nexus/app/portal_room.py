from typing import Annotated

import typer
from typer import Typer

import nexus.portal_room as portal_room
from nexus.spell import Spell
from nexus.utils.print_output import print_output

cli_app = Typer()
# api_app = FastAPI()

SpellArg = Annotated[Spell, typer.Argument(parser=Spell.parse)]


@cli_app.command()
@print_output
def list_portals():
    return portal_room.list_portals()


@cli_app.command()
@print_output
def get_portal(name: str):
    return portal_room.get_portal(name)


@cli_app.command()
@print_output
def prepare_portal(spell: SpellArg, name: str):
    return portal_room.prepare_portal(spell, name)


@cli_app.command()
@print_output
def create_portal(
    spell: SpellArg, name: str, start: Annotated[bool, typer.Option("--start")] = False
):
    return portal_room.create_portal(spell, name, start)


@cli_app.command()
@print_output
def cast_portal(spell: SpellArg, name: str):
    return portal_room.cast_portal(spell, name)


@cli_app.command()
@print_output
def open_portal(name: str):
    return portal_room.open_portal(name)


@cli_app.command()
@print_output
def reopen_portal(name: str):
    return portal_room.reopen_portal(name)


@cli_app.command()
@print_output
def close_portal(name: str):
    return portal_room.close_portal(name)


@cli_app.command()
@print_output
def remove_portal(name: str, force: Annotated[bool, typer.Option("--force")] = False):
    return portal_room.remove_portal(name, force=force)


@cli_app.command()
@print_output
def destroy_portal(name: str, force: Annotated[bool, typer.Option("--force")] = False):
    return portal_room.destroy_portal(name, force=force)
