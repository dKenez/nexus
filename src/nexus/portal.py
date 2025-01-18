from dataclasses import dataclass
from pathlib import Path

from docker.models.containers import Container


@dataclass
class Portal:
    name: str
    container: Container
    data: Path

    def jsonify(self):
        return {
            "name": self.name,
            "container": {
                "name": self.container.name,
                "short_id": self.container.short_id,
                "status": self.container.status,
                "image": {
                    "tags": self.container.image.tags,
                    "short_id": self.container.image.short_id,
                },
            },
        }
