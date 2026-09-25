"""Finna Library integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FinnaAuthError, FinnaClient, FinnaData, FinnaError
from .const import CONF_HOST, CONF_PIN, CONF_USERNAME, DEFAULT_HOST, DOMAIN, RETRY_DELAYS_MINUTES, UPDATE_INTERVAL_HOURS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button", "calendar", "todo"]

type FinnaConfigEntry = ConfigEntry[FinnaCoordinator]


class FinnaCoordinator(DataUpdateCoordinator[FinnaData]):
    """Fetches account data from Finna for one library card."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data[CONF_USERNAME]}",
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        # Own cookie jar per card so multiple accounts don't share a session.
        self.session = async_create_clientsession(hass)
        self.host: str = entry.data.get(CONF_HOST, DEFAULT_HOST)
        self.client = FinnaClient(
            self.session, entry.data[CONF_USERNAME], entry.data[CONF_PIN], self.host
        )
        self.username: str = entry.data[CONF_USERNAME]
        self._failures = 0

    async def _async_update_data(self) -> FinnaData:
        start = time.monotonic()
        try:
            data = await self._fetch()
        except UpdateFailed:
            _LOGGER.debug(
                "Poll for %s… failed after %.1f s",
                self.username[:5], time.monotonic() - start,
            )
            # The coordinator schedules the next poll from update_interval
            # after this returns, so set the retry delay before raising.
            if self._failures < len(RETRY_DELAYS_MINUTES):
                delay = timedelta(minutes=RETRY_DELAYS_MINUTES[self._failures])
            else:
                delay = timedelta(hours=UPDATE_INTERVAL_HOURS)
            self._failures += 1
            self.update_interval = delay
            # HA logs only the first failure; make a lasting outage visible.
            if self._failures == 2:
                _LOGGER.warning(
                    "Finna poll for %s… has failed %d times in a row; "
                    "sensors stay unavailable until it recovers",
                    self.username[:5], self._failures,
                )
            raise
        _LOGGER.debug(
            "Poll for %s… took %.1f s", self.username[:5], time.monotonic() - start
        )
        self._failures = 0
        self.update_interval = timedelta(hours=UPDATE_INTERVAL_HOURS)
        return data

    async def _fetch(self) -> FinnaData:
        try:
            data = await self.client.async_get_data(self.data)
            # Flag saved searches whose hit count grew since the last poll.
            # Skip when the searches were carried over unchanged (issue #4):
            # comparing them with themselves would zero the counts.
            if (
                self.data is not None
                and data.saved_searches is not self.data.saved_searches
            ):
                previous = {
                    (s.url or s.query): s.results for s in self.data.saved_searches
                }
                for search in data.saved_searches:
                    prev = previous.get(search.url or search.query)
                    if prev is not None and search.results is not None:
                        search.new_results = max(0, search.results - prev)
            return data
        except FinnaAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except FinnaError as err:
            raise UpdateFailed(err) from err
        except Exception as err:  # noqa: BLE001 - network errors from aiohttp
            raise UpdateFailed(err) from err


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 entries: unique IDs and device identifiers gain the host."""
    if entry.version > 2:
        return False  # downgrade from a future version
    if entry.version == 1:
        host = entry.data.get(CONF_HOST, DEFAULT_HOST)
        username = entry.data[CONF_USERNAME]
        account = f"{host}:{username.lower()}"
        old_prefix = f"{username}_"

        @callback
        def migrate_unique_id(entity_entry: er.RegistryEntry) -> dict | None:
            if entity_entry.unique_id.startswith(old_prefix):
                key = entity_entry.unique_id.removeprefix(old_prefix)
                return {"new_unique_id": f"{account}_{key}"}
            return None

        await er.async_migrate_entries(hass, entry.entry_id, migrate_unique_id)
        device_registry = dr.async_get(hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, username)})
        if device is not None:
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, account)}
            )
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FinnaConfigEntry) -> bool:
    coordinator = FinnaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FinnaConfigEntry) -> bool:
    # The client session is closed by HA itself on unload (issue #3).
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
