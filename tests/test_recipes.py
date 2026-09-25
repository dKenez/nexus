from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus.core.recipes import MissingSecretError, RecipeBook, RecipeNotFoundError
from tests.conftest import recipe

REPO_RECIPES = Path(__file__).parent.parent / "recipes"


def test_repo_recipes_load() -> None:
    book = RecipeBook.load(REPO_RECIPES)
    valheim = book.get("valheim")
    assert valheim.query.port == 2457
    assert valheim.image_ref == "ghcr.io/dkenez/nexus-valheim:the-bog-witch"
    assert valheim.secret_env_var("SERVER_PASSWORD") == "NEXUS_GAME_VALHEIM_SERVER_PASSWORD"


def test_unknown_recipe() -> None:
    with pytest.raises(RecipeNotFoundError):
        RecipeBook({}).get("nope")


def test_query_port_must_be_published() -> None:
    with pytest.raises(ValidationError, match="query port"):
        recipe("xy", port=2456, query={"type": "a2s", "port": 9999})


def test_query_needs_port() -> None:
    with pytest.raises(ValidationError, match="needs a port"):
        recipe("xy", query={"type": "a2s"})


def test_invalid_name() -> None:
    with pytest.raises(ValidationError):
        recipe("Bad Name")


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        recipe("xy", volumes=["a:b"])


def test_resolve_env() -> None:
    r = recipe("xy", env={"A": "1"})
    assert r.resolve_env({"NEXUS_GAME_XY_SERVER_PASSWORD": "pw"}) == {
        "A": "1",
        "SERVER_PASSWORD": "pw",
    }
    with pytest.raises(MissingSecretError, match="NEXUS_GAME_XY_SERVER_PASSWORD"):
        r.resolve_env({})


def test_directory_must_match_name(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "recipe.toml").write_text(
        (REPO_RECIPES / "valheim" / "recipe.toml").read_text()
    )
    with pytest.raises(ValueError, match="does not match"):
        RecipeBook.load(tmp_path)
