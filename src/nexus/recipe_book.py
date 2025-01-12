from pathlib import Path

import tomllib
from docker.models.images import Image

import nexus
from nexus.utils.docker import client


def list_recipes():
    return nexus.paths.recipe_dir.glob("*")


def get_recipe(name: str):
    recipes = list_recipes()
    hits = [recipe for recipe in recipes if recipe.stem == name]

    match len(hits):
        case 1:
            return hits[0]
        case 0:
            raise KeyError(f'Recipe "{name}" not found')
        case _:
            raise KeyError(
                f'Recipe "{name}" found {len(hits)} times. Expected 1 match.'
            )


def build_recipe(*recipes: Path):
    built_recipes: list[Image] = []
    for recipe in recipes:
        with open(recipe / "properties.toml", "rb") as f:
            properties = tomllib.load(f)

        image, _ = client.images.build(
            path=str(recipe.absolute()),
            tag=f"nexus-{properties['name']}:{properties['game_version']}",
        )

        built_recipes.append(image)

    return built_recipes
