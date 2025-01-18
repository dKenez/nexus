from pathlib import Path

from fastapi import APIRouter
from typer import Typer

import nexus.recipe_book as recipe_book
from nexus.utils.print_output import print_output

cli_app = Typer()
api_router = APIRouter()


@cli_app.command(name="list")
@api_router.get(path="/list")
@print_output
def list_recipes():
    return list(recipe_book.list_recipes())


@cli_app.command(name="get")
@api_router.get(path="/get")
@print_output
def get_recipe(name: str):
    return recipe_book.get_recipe(name)


@cli_app.command(name="build")
@api_router.post(path="/build")
@print_output
def build_recipe(recipes: list[Path]):
    built_images = recipe_book.build_recipe(*recipes)
    formatted_images = [
        {"tags": built_image.tags, "id": built_image.short_id}
        for built_image in built_images
    ]
    return formatted_images
