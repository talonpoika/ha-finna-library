"""Renew-all button."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FinnaConfigEntry, FinnaCoordinator
from .api import FinnaError
from .entity import FinnaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FinnaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([RenewAllButton(entry.runtime_data)])


class RenewAllButton(FinnaEntity, ButtonEntity):
    _attr_translation_key = "renew_all"
    _attr_icon = "mdi:book-refresh"

    def __init__(self, coordinator: FinnaCoordinator) -> None:
        super().__init__(coordinator, "renew_all")

    async def async_press(self) -> None:
        try:
            result = await self.coordinator.client.async_renew_all()
        except FinnaError as err:
            raise HomeAssistantError(f"Renewing loans failed: {err}") from err
        _LOGGER.info(
            "Renew all for %s…: renewed %s; not renewed %s",
            self.coordinator.username[:5],
            result.renewed,
            result.failed,
        )
        await self.coordinator.async_request_refresh()
        if result.failed:
            # Raising is what surfaces the outcome in the UI (issue #6).
            raise HomeAssistantError(
                f"{len(result.renewed)} renewed, {len(result.failed)} not: "
                + "; ".join(result.failed)
            )
