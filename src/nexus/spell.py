from dataclasses import dataclass
from pathlib import Path

from docker.models.images import Image

from nexus import spell_book


@dataclass
class Spell:
    name: str
    image: Image
    recipe: Path

    @staticmethod
    def parse(name: str):
        return spell_book.get_spell(name)
