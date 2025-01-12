import re

from docker.models.images import Image

import nexus.recipe_book as recipe_book
from nexus.spell import Spell
from nexus.utils.docker import client

spell_format = re.compile(r"nexus-([a-zA-Z0-9]+):")


def extract_spell_name(image: Image):
    for tag in image.tags:
        m = spell_format.search(tag)

        if m:
            return str(m.group(1))

    return ""


def nexus_spell_name(image: Image):
    for tag in image.tags:
        m = spell_format.search(tag)

        if m:
            return tag

    return ""


def list_spells():
    images = client.images.list()

    spells: list[Spell] = []

    for image in images:
        spell_name = extract_spell_name(image)

        if spell_name:
            spell = Spell(
                spell_name,
                image,
                recipe_book.get_recipe(spell_name),
            )
            spells.append(spell)

    return spells


def get_spell(name: str):
    spells = list_spells()
    hits = [spell for spell in spells if spell.name == name]

    match len(hits):
        case 1:
            return hits[0]
        case 0:
            raise KeyError(f'Spell "{name}" not found')
        case _:
            raise KeyError(f'Spell "{name}" found {len(hits)} times. Expected 1 match.')
