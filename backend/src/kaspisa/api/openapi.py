"""Выгрузка схемы OpenAPI: `uv run python -m kaspisa.api.openapi > openapi.json`."""

from __future__ import annotations

import json

from kaspisa.api.app import create_app


def main() -> None:
    print(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
