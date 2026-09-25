"""Renew-all button feedback (issue #6)."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.finna_library.api import FinnaData, RenewResult
from custom_components.finna_library.const import CONF_HOST, CONF_PIN, CONF_USERNAME, DOMAIN


async def _press(hass, result: RenewResult):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_HOST: "heili.finna.fi", CONF_USERNAME: "HEILI0123", CONF_PIN: "0000"},
        unique_id="heili.finna.fi:heili0123",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.finna_library.FinnaClient.async_get_data",
        AsyncMock(return_value=FinnaData()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.async_renew_all = AsyncMock(return_value=result)
        entity_id = next(
            s.entity_id for s in hass.states.async_all("button")
        )
        try:
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )
        finally:
            # Unload before the test ends so no refresh or executor work
            # outlives it (HA 2024.6's test harness fails on lingering threads).
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_press_names_loans_that_were_not_renewed(hass):
    with pytest.raises(HomeAssistantError) as err:
        await _press(
            hass,
            RenewResult(
                renewed=["Kiikissä"],
                failed=["Varattu kirja: Uusinta epäonnistui: Niteellä on varauksia"],
            ),
        )
    assert "Varattu kirja" in str(err.value)
    assert "1 renewed" in str(err.value)


async def test_press_all_renewed_does_not_raise(hass):
    await _press(hass, RenewResult(renewed=["Kiikissä"], failed=[]))


async def test_renew_with_no_result_shown_is_not_silent_success():
    from pathlib import Path

    from custom_components.finna_library.api import FinnaClient

    before = (Path(__file__).parent / "fixtures" / "checkedout.html").read_text()
    client = FinnaClient(session=None, username="u", pin="p")
    client._get_page = AsyncMock(return_value=before)  # noqa: SLF001
    # Finna answers with the plain list: no renewal result for the loan.
    client._post = AsyncMock(return_value=before)  # noqa: SLF001

    result = await client.async_renew_all()

    assert result.renewed == []
    assert result.failed and "Kiikissä" in result.failed[0]
