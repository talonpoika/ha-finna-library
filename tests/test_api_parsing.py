"""Parser tests against saved/hand-built Finna HTML fixtures."""

from datetime import date
from pathlib import Path

import pytest

from custom_components.finna_library.api import (
    FinnaData,
    parse_checked_out,
    parse_fines_total,
    parse_finnish_date,
    parse_holds,
    parse_login_form,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_finnish_date():
    assert parse_finnish_date("Eräpäivä: 31.8.2026") == date(2026, 8, 31)
    assert parse_finnish_date("9.12.2025") == date(2025, 12, 9)
    assert parse_finnish_date(None) is None
    assert parse_finnish_date("ei päivämäärää") is None


def test_parse_checked_out():
    loans, renew_ids, csrf = parse_checked_out(fixture("checkedout.html"))
    assert len(loans) == 2  # header row skipped

    loan = loans[0]
    assert loan.title == "Kiikissä"
    assert loan.author == "Amores, Eva"
    assert loan.due_date == date(2026, 8, 31)
    assert loan.renewable is True
    assert loan.record_id == "demo.aaaa-1111"
    assert loans[1].record_id == "demo.bbbb-2222"
    assert loan.details["Lainauspaikka"] == "Pontuksen kirjasto"
    assert loan.details["Uusintakertoja jäljellä"] == "5"

    assert loans[1].title == "Ei-uusittava kirja"
    assert loans[1].renewable is False
    assert loans[1].due_date == date(2026, 7, 15)

    assert renew_ids == ["renewid-1"]
    assert csrf == "testcsrf-123"


def test_parse_checked_out_no_renewals_form():
    # Live Finna views omit the whole renewals form when nothing is renewable.
    html = fixture("checkedout.html").replace('name="renewals"', 'name="other"')
    loans, renew_ids, csrf = parse_checked_out(html)
    assert len(loans) == 2
    assert renew_ids == []
    assert csrf is None


def test_parse_holds():
    holds = parse_holds(fixture("holds.html"))
    assert len(holds) == 3

    transit = holds[0]
    assert transit.title == "Korppien kehä"
    assert transit.in_transit is True
    assert transit.available is False
    assert transit.pickup_location == "Pontuksen kirjasto"
    assert transit.expires == date(2029, 7, 11)

    queued = holds[1]
    assert queued.queue_position == "4 (10 kappaletta)"
    assert queued.in_transit is False

    ready = holds[2]
    assert ready.available is True


def test_parse_fines():
    assert parse_fines_total(fixture("fines.html")) == 2.5


def test_parse_fines_empty():
    assert parse_fines_total("<html><body></body></html>") == 0.0


def test_parse_login_form():
    html = """
    <form method="post" action="/MyResearch/Home" name="loginForm" id="loginForm">
      <input type="hidden" name="target" value="demo">
      <input id="u" type="text" name="username" value="">
      <input id="p" type="password" name="password">
      <input type="hidden" name="auth_method" value="MultiILS">
      <input type="hidden" name="csrf" value="abc-def">
    </form>
    """
    fields = parse_login_form(html)
    assert fields == {"target": "demo", "auth_method": "MultiILS", "csrf": "abc-def"}


def test_next_due_date():
    loans, _, _ = parse_checked_out(fixture("checkedout.html"))
    data = FinnaData(loans=loans)
    assert data.next_due_date == date(2026, 7, 15)
    assert FinnaData().next_due_date is None


def test_parse_history_page():
    from custom_components.finna_library.api import parse_history_page

    entries, total = parse_history_page(fixture("history.html"))
    assert total == 5
    assert len(entries) == 2
    assert entries[0].title == "Uusi kirja"
    assert entries[0].author == "Meikäläinen, Matti"
    assert entries[0].checkout_date == date(2026, 7, 15)
    assert entries[0].return_date == date(2026, 7, 30)
    assert entries[1].title == "Vanha kirja ilman tietuetta"
    assert entries[1].checkout_date == date(2025, 11, 20)


def test_parse_history_page_empty():
    from custom_components.finna_library.api import parse_history_page

    # A live Finna view shows "Lainaushistoria (0)" with no table when empty.
    entries, total = parse_history_page(
        "<html><body><h2>Lainaushistoria (0)</h2>"
        "Ei tietoja lainaushistoriassa.</body></html>"
    )
    assert entries == []
    assert total == 0


def test_parse_saved_searches():
    from custom_components.finna_library.api import parse_saved_searches

    searches = parse_saved_searches(fixture("searchhistory.html"))
    assert len(searches) == 2  # recent-searches table is ignored
    assert searches[0].query == "sienet"
    assert searches[0].url == "/Search/Results?lookfor=sienet&type=AllFields"
    assert searches[0].results == 1234  # thousands separator stripped
    assert searches[1].results == 7


def test_parse_saved_searches_none():
    from custom_components.finna_library.api import parse_saved_searches

    assert parse_saved_searches("<html><body></body></html>") == []


def test_parse_finnish_date_invalid_calendar_date():
    assert parse_finnish_date("31.6.2026") is None
    assert parse_finnish_date("99.9.2026") is None


def test_text_pairs_value_in_span():
    from custom_components.finna_library.api import parse_holds

    html = """
    <table><tr class="myresearch-row" id="recordx.1"><td>
      <h3 class="record-title">Kirja</h3>
      <div class="holds-status-information">
        <strong>Noutopaikka:</strong>
        <span>Keskuskirjasto</span><br>
      </div>
    </td></tr></table>
    """
    holds = parse_holds(html)
    assert holds[0].pickup_location == "Keskuskirjasto"


def test_parse_renew_result():
    from custom_components.finna_library.api import parse_renew_result

    result = parse_renew_result(
        fixture("renewresult.html"),
        attempted={"demo.aaaa-1111", "demo.cccc-3333"},
    )
    assert result.renewed == ["Kiikissä"]
    # The overdue, never-attempted loan is not a failed renewal.
    assert result.failed == [
        "Piilotettu laina: Uusinta epäonnistui: Varattu toiselle",
        "Varattu kirja: Uusinta epäonnistui: Niteellä on varauksia",
    ]
