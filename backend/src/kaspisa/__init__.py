"""Сервис автоматического управления ценами продавцов Kaspi."""

from importlib.metadata import version

# Единственный источник версии — pyproject.toml (его сверяет deploy/check-release-tag.sh).
__version__ = version("kaspisa")
