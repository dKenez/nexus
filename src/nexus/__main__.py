"""``python -m nexus``: run the API, the Discord bot and the reconciler in one process."""

import logging

import uvicorn

from nexus.app import create_app
from nexus.config import get_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
