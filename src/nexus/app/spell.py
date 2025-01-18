from fastapi import APIRouter
from typer import Typer

import nexus.spell_book as spell_book
from nexus.utils.print_output import print_output

cli_app = Typer()
api_router = APIRouter()


@cli_app.command(name="list")
@api_router.get(path="/list")
@print_output
def list_spells():
    spells = spell_book.list_spells()
    formatted_spells = [
        {"name": spell.name, "images": spell.image.tags, "id": spell.image.short_id}
        for spell in spells
    ]
    return formatted_spells


@cli_app.command(name="get")
@api_router.get(path="/get")
@print_output
def get_spell(name: str):
    spell = spell_book.get_spell(name)
    formatted_spell = {
        "name": spell.name,
        "images": spell.image.tags,
        "id": spell.image.short_id,
    }
    return formatted_spell
