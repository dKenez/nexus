from importlib.metadata import version

import nexus
from nexus.utils.paths import Paths

name = "nexus"
__version__ = version(name)
paths = Paths(nexus)


def main() -> None:
    print("Hello from nexus!")
