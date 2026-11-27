"""Журнал действий владельца (ADR 0024): последние события арендатора."""

from __future__ import annotations

from fastapi import APIRouter

from kaspisa.api.deps import CurrentPrincipal, Deps
from kaspisa.api.schemas import OwnerEventOut

router = APIRouter(prefix="/api/events")

EVENTS_LIMIT = 200  # без страниц (Н7)


@router.get("")
async def list_events(principal: CurrentPrincipal, deps: Deps) -> list[OwnerEventOut]:
    async with deps.database.tenant(principal.tenant_id) as scope:
        events = await scope.owner_events.list_recent(EVENTS_LIMIT)
    return [OwnerEventOut.model_validate(event, from_attributes=True) for event in events]
