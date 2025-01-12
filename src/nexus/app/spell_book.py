from typer import Typer

import nexus.spell_book as spell_book
from nexus.utils.print_output import print_output

cli_app = Typer()
# api_app = FastAPI()


@cli_app.command()
@print_output
def list_spells():
    return spell_book.list_spells()


@cli_app.command()
@print_output
def get_spell(name: str):
    return spell_book.get_spell(name)
