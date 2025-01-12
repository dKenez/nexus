from pathlib import Path

from rich import print
from typer import Typer

import nexus.recipe_book as recipe_book

cli_app = Typer()
# api_app = FastAPI()


@cli_app.command()
def list_recipes():
    recipes = recipe_book.list_recipes()
    print([str(recipe) for recipe in recipes])


@cli_app.command()
def get_recipe(name: str):
    recipe = recipe_book.get_recipe(name)
    print(recipe)


@cli_app.command()
def build_recipe(recipes: list[Path]):
    recipe = recipe_book.build_recipe(*recipes)
    print(recipe)
