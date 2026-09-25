"""Recipes: the declarative description of a game server (``recipes/<name>/recipe.toml``)."""

import os
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class QueryType(StrEnum):
    A2S = "a2s"
    NONE = "none"


class Port(BaseModel):
    model_config = ConfigDict(frozen=True)

    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"

    def __str__(self) -> str:
        return f"{self.port}/{self.protocol}"


class Query(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: QueryType = QueryType.NONE
    port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def _port_required(self) -> "Query":
        if self.type is not QueryType.NONE and self.port is None:
            raise ValueError(f"query type {self.type} needs a port")
        return self


class Recipe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    display_name: str
    image: str
    version: str
    enabled: bool = True
    data_path: str = "/data"
    memory_mb: int = Field(ge=256)
    ports: tuple[Port, ...] = Field(min_length=1)
    query: Query = Query()
    idle_minutes: int = Field(default=20, ge=1)
    startup_grace_minutes: int = Field(default=10, ge=0)
    startup_timeout: int = Field(default=600, ge=10, description="seconds")
    stop_grace_seconds: int = Field(default=60, ge=1)
    env: dict[str, str] = {}
    secret_env: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError(f"invalid recipe name {value!r}")
        return value

    @field_validator("data_path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("data_path must be absolute")
        return value

    @model_validator(mode="after")
    def _query_port_is_published(self) -> "Recipe":
        if self.query.port is not None and self.query.port not in {p.port for p in self.ports}:
            raise ValueError(f"query port {self.query.port} is not one of the recipe's ports")
        overlap = set(self.env) & set(self.secret_env)
        if overlap:
            raise ValueError(f"keys in both env and secret_env: {sorted(overlap)}")
        return self

    @property
    def image_ref(self) -> str:
        return f"{self.image}:{self.version}"

    @property
    def container_name(self) -> str:
        return f"nexus-{self.name}"

    @property
    def idle_seconds(self) -> int:
        return self.idle_minutes * 60

    def secret_env_var(self, key: str) -> str:
        """The nexus environment variable a recipe secret is read from."""
        return f"NEXUS_GAME_{self.name.upper().replace('-', '_')}_{key}"

    def resolve_env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        """The full environment for the game container, with secrets resolved."""
        source = os.environ if environ is None else environ
        resolved = dict(self.env)
        missing = []
        for key in self.secret_env:
            var = self.secret_env_var(key)
            if var not in source:
                missing.append(var)
            else:
                resolved[key] = source[var]
        if missing:
            raise MissingSecretError(self.name, missing)
        return resolved


class MissingSecretError(Exception):
    def __init__(self, recipe: str, variables: list[str]) -> None:
        super().__init__(f"recipe {recipe!r} is missing secrets: {', '.join(variables)}")
        self.recipe = recipe
        self.variables = variables


class RecipeNotFoundError(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"no such game: {self.name!r}"


class RecipeBook:
    """All recipes, keyed by name."""

    def __init__(self, recipes: dict[str, Recipe]) -> None:
        self._recipes = recipes

    @classmethod
    def load(cls, directory: Path) -> "RecipeBook":
        recipes: dict[str, Recipe] = {}
        for path in sorted(directory.glob("*/recipe.toml")):
            with path.open("rb") as f:
                recipe = Recipe.model_validate(tomllib.load(f))
            if recipe.name != path.parent.name:
                raise ValueError(f"{path}: name {recipe.name!r} does not match its directory")
            recipes[recipe.name] = recipe
        return cls(recipes)

    def get(self, name: str) -> Recipe:
        try:
            return self._recipes[name]
        except KeyError:
            raise RecipeNotFoundError(name) from None

    def all(self) -> list[Recipe]:
        return list(self._recipes.values())

    def enabled(self) -> list[Recipe]:
        return [r for r in self._recipes.values() if r.enabled]

    def __contains__(self, name: object) -> bool:
        return name in self._recipes
