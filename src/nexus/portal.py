from dataclasses import dataclass
from pathlib import Path

from docker.models.containers import Container


@dataclass
class Portal:
    name: str
    container: Container
    data: Path
