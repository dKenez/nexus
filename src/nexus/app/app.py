import typer

import nexus.app.recipe_book as recipe_book
import nexus.app.spell_book as spell_book
import nexus.app.portal_room as portal_room

cli_app = typer.Typer()
cli_app.add_typer(recipe_book.cli_app, name="recipe-book")
cli_app.add_typer(spell_book.cli_app, name="spell-book")
cli_app.add_typer(portal_room.cli_app, name="portal-room")

if __name__ == "__main__":
    cli_app()
