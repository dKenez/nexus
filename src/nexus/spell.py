from dataclasses import dataclass
from pathlib import Path

from docker.models.images import Image


@dataclass
class Spell:
    name: str
    image: Image
    recipe: Path
