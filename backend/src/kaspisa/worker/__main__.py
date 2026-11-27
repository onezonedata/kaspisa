"""Точка входа: `python -m kaspisa.worker`."""

import asyncio
import logging

from kaspisa.config import Settings
from kaspisa.worker.runner import run_worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_worker(Settings()))


if __name__ == "__main__":
    main()
