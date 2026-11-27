"""HTTP API сервиса."""

from kaspisa.api.app import create_app
from kaspisa.api.deps import ApiDeps

__all__ = ["ApiDeps", "create_app"]
