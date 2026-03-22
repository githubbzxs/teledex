from __future__ import annotations

import logging

from .app import TeledexApp
from .config import AppConfig


def main() -> None:
    config = AppConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = TeledexApp(config)
    app.run_forever()


if __name__ == "__main__":
    main()
