import fastapi
import typer

import nexus.app.portal as portal
import nexus.app.recipe as recipe
import nexus.app.spell as spell

cli_app = typer.Typer()
api_app = fastapi.FastAPI()

cli_app.add_typer(recipe.cli_app, name="recipe")
cli_app.add_typer(spell.cli_app, name="spell")
cli_app.add_typer(portal.cli_app, name="portal")

api_app.include_router(
    recipe.api_router,
    prefix="/recipe",
    tags=["recipe"],
)
api_app.include_router(
    spell.api_router,
    prefix="/spell",
    tags=["spell"],
)
api_app.include_router(
    portal.api_router,
    prefix="/portal",
    tags=["portal"],
)


if __name__ == "__main__":
    # import uvicorn
    # uvicorn.run(api_app, host="0.0.0.0", port=8000)
    cli_app()
