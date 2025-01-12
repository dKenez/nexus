import typer

import nexus.app.recipe_book as recipe_book

cli_app = typer.Typer()
cli_app.add_typer(recipe_book.cli_app, name="recipe_book")

if __name__ == "__main__":
    cli_app()
