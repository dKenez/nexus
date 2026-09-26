"""``python -m nexus``: run the API, the Discord bot and the reconciler in one process."""

import logging

import uvicorn

from nexus.app import create_app
from nexus.config import get_settings


class _RenameUvicornError(logging.Filter):
    """uvicorn logs its normal lifecycle ("Application startup complete") on a logger named
    ``uvicorn.error``, which reads like a failure in our output; show it as ``uvicorn``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RenameUvicornError())
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
