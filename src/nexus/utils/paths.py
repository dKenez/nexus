import types
from pathlib import Path


class Paths:
    def __init__(self, module: types.ModuleType):
        self.project_root = Path(module.__path__[0]).parents[1]
        self.recipe_dir = self.project_root / "recipes"
        self.portals_dir = self.project_root / "portals"

    def __repr__(self):
        return f"Paths(project_root={self.project_root})"

    def __str__(self):
        return f"Paths(project_root={self.project_root})"

    def print_paths(self):
        print(f"{self.project_root = }")
        print(f"{self.recipe_dir = }")
        print(f"{self.portals_dir = }")


if __name__ == "__main__":
    # Check paths
    import nexus

    Paths(nexus).print_paths()
