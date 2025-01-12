from pathlib import Path

from typer import Typer

import nexus.recipe_book as recipe_book
from nexus.utils.print_output import print_output

cli_app = Typer()
# api_app = FastAPI()


@cli_app.command()
@print_output
def list_recipes():
    return list(recipe_book.list_recipes())


@cli_app.command()
@print_output
def get_recipe(name: str):
    return recipe_book.get_recipe(name)


@cli_app.command()
@print_output
def build_recipe(recipes: list[Path]):
    return recipe_book.build_recipe(*recipes)
