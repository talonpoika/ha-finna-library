"""Coordinator lifecycle and failure handling (issues #3, #4)."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.finna_library.api import (
    FinnaClient,
    FinnaConnectionError,
    FinnaData,
    SavedSearch,
)
from custom_components.finna_library.const import (
    CONF_HOST,
    CONF_PIN,
    CONF_USERNAME,
    DOMAIN,
    UPDATE_INTERVAL_HOURS,
)

NORMAL = timedelta(hours=UPDATE_INTERVAL_HOURS)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HEILI0123",
        version=2,
        data={CONF_HOST: "heili.finna.fi", CONF_USERNAME: "HEILI0123", CONF_PIN: "0000"},
        unique_id="heili.finna.fi:heili0123",
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, get_data):
    entry = _entry(hass)
    with patch(
        "custom_components.finna_library.FinnaClient.async_get_data", get_data
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_unload_does_not_close_session_itself(hass, caplog):
    entry = await _setup(hass, AsyncMock(return_value=FinnaData()))
    session = entry.runtime_data.session

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert "closes the Home Assistant aiohttp session" not in caplog.text
    assert session.closed  # HA's own on-unload cleanup still closes it


async def test_failed_update_retries_in_minutes_then_restores_interval(hass):
    get_data = AsyncMock(return_value=FinnaData())
    entry = await _setup(hass, get_data)
    coordinator = entry.runtime_data
    coordinator.client.async_get_data = get_data
    assert coordinator.update_interval == NORMAL

    intervals = []
    get_data.side_effect = FinnaConnectionError("GET /MyResearch/CheckedOut: timeout")
    for _ in range(5):
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        intervals.append(coordinator.update_interval)

    # Short, growing retries; then back to the polite normal cadence.
    assert intervals[0] <= timedelta(minutes=5)
    assert intervals[0] < intervals[1] < intervals[2] < NORMAL
    assert intervals[3:] == [NORMAL, NORMAL]

    get_data.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.update_interval == NORMAL

    # A later outage starts the retry ladder from the beginning again.
    get_data.side_effect = FinnaConnectionError("boom")
    await coordinator.async_refresh()
    assert coordinator.update_interval == intervals[0]


def _client_with_pages(pages: dict[str, str | Exception]) -> FinnaClient:
    client = FinnaClient(session=None, username="u", pin="p")
    calls = []

    async def fake_get_page(path):
        calls.append(path)
        for prefix, body in pages.items():
            if path.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected {path}")

    client._get_page = fake_get_page  # noqa: SLF001
    client.calls = calls
    return client


EMPTY = "<html><body></body></html>"


async def test_optional_page_failure_keeps_loans_and_previous_values():
    client = _client_with_pages(
        {
            "/MyResearch/CheckedOut": EMPTY,
            "/Holds/List": EMPTY,
            "/MyResearch/Fines": FinnaConnectionError("GET /MyResearch/Fines: timeout"),
            "/Checkouts/History": EMPTY,
            "/Search/History": EMPTY,
        }
    )
    previous = FinnaData(
        fines_total=1.5,
        loans_this_year=7,
        history_year=date.today().year,
        history_total=40,
        saved_searches=[SavedSearch(query="q", url="/s", results=3)],
    )

    data = await client.async_get_data(previous)

    assert data.fines_total == 1.5
    assert data.loans_this_year == 7
    assert data.history_total == 40
    assert data.saved_searches == previous.saved_searches
    # After one optional page fails, don't keep hammering a slow server.
    assert not any(p.startswith(("/Checkouts/History", "/Search/History")) for p in client.calls)


async def test_core_page_failure_still_fails_update():
    client = _client_with_pages(
        {"/MyResearch/CheckedOut": FinnaConnectionError("timeout")}
    )
    try:
        await client.async_get_data(FinnaData())
    except FinnaConnectionError:
        return
    raise AssertionError("core page failure must propagate")


async def test_last_years_count_is_not_carried_into_new_year():
    client = _client_with_pages(
        {
            "/MyResearch/CheckedOut": EMPTY,
            "/Holds/List": EMPTY,
            "/MyResearch/Fines": FinnaConnectionError("timeout"),
        }
    )
    previous = FinnaData(
        loans_this_year=42, history_total=90, history_year=date.today().year - 1
    )

    data = await client.async_get_data(previous)

    assert data.loans_this_year is None


async def test_carried_saved_searches_keep_new_result_counts(hass):
    search = SavedSearch(query="q", url="/s", results=5, new_results=2)
    get_data = AsyncMock(return_value=FinnaData(saved_searches=[search]))
    entry = await _setup(hass, get_data)
    coordinator = entry.runtime_data
    coordinator.data.saved_searches[0].new_results = 2

    # Simulate a poll where the searches page was skipped and carried over.
    coordinator.client.async_get_data = AsyncMock(
        side_effect=lambda prev: FinnaData(saved_searches=prev.saved_searches)
    )
    await coordinator.async_refresh()

    assert coordinator.data.saved_searches[0].new_results == 2
