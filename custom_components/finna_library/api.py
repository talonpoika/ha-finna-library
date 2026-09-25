"""HTTP client and HTML parsing for Finna library views (VuFind-based).

Finna has no user-data API, so this logs in like a browser (form POST with a
one-time CSRF token) and parses the account pages. All parsing functions are
pure so they can be unit-tested against saved HTML fixtures.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

import aiohttp
from bs4 import BeautifulSoup

from .const import DEFAULT_HOST, USER_AGENT

_LOGGER = logging.getLogger(__name__)

FINNISH_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


class FinnaError(Exception):
    """Base error."""


class FinnaAuthError(FinnaError):
    """Login rejected."""


class FinnaConnectionError(FinnaError):
    """Network or HTTP-level failure."""


@dataclass
class Loan:
    title: str | None
    author: str | None
    due_date: date | None
    renewable: bool
    details: dict
    record_id: str | None = None


@dataclass
class Hold:
    title: str
    available: bool
    in_transit: bool
    pickup_location: str | None
    queue_position: str | None
    expires: date | None


@dataclass
class HistoryEntry:
    title: str | None
    author: str | None
    checkout_date: date | None
    return_date: date | None


@dataclass
class SavedSearch:
    query: str
    url: str | None
    results: int | None
    new_results: int = 0


@dataclass
class RenewResult:
    renewed: list[str] = field(default_factory=list)  # titles
    failed: list[str] = field(default_factory=list)  # "title: reason"


@dataclass
class FinnaData:
    loans: list[Loan] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)
    fines_total: float | None = None
    renew_all_ids: list[str] = field(default_factory=list)
    renew_csrf: str | None = None
    loans_this_year: int | None = None
    history_total: int | None = None
    history_year: int | None = None  # year loans_this_year was counted for
    saved_searches: list[SavedSearch] = field(default_factory=list)

    @property
    def next_due_date(self) -> date | None:
        dates = [loan.due_date for loan in self.loans if loan.due_date]
        return min(dates) if dates else None


def parse_finnish_date(text: str | None) -> date | None:
    m = FINNISH_DATE_RE.search(text or "")
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return date(int(y), int(mo), int(d))
    except ValueError:
        return None


def _text_pairs(container) -> dict:
    """Extract '<strong>Label:</strong> value' pairs plus bare strong texts."""
    pairs: dict = {}
    for strong in container.find_all("strong"):
        text = strong.get_text(" ", strip=True)
        if ":" in text:
            label, _, value = text.partition(":")
            value = value.strip()
            if not value:
                # Walk siblings past whitespace-only text nodes; take the
                # first with content (a text node or an element like <span>).
                for nxt in strong.next_siblings:
                    text = (
                        nxt.get_text(" ", strip=True)
                        if hasattr(nxt, "get_text")
                        else str(nxt)
                    )
                    text = re.sub(r"\s+", " ", text).strip(" |")
                    if text:
                        value = text
                        break
                    if getattr(nxt, "name", None) == "br":
                        break
            pairs[label.strip()] = value
        else:
            pairs.setdefault("_flags", []).append(text)
    return pairs


def parse_login_form(html: str) -> dict:
    form = BeautifulSoup(html, "html.parser").find("form", attrs={"name": "loginForm"})
    if form is None:
        raise FinnaError("login form not found")
    return {
        inp["name"]: inp.get("value", "")
        for inp in form.find_all("input", attrs={"type": "hidden"})
    }


def is_logged_out(html: str) -> bool:
    return 'name="loginForm"' in html or 'id="loginForm"' in html


def parse_checked_out(html: str) -> tuple[list[Loan], list[str], str | None]:
    """Return (loans, renew_all_ids, csrf)."""
    doc = BeautifulSoup(html, "html.parser")
    loans = []
    for row in doc.select("tr.myresearch-row[id^=record]"):
        title_el = row.select_one("h3.record-title")
        author_el = row.select_one(".record-core-metadata .authority-label")
        status = row.select_one(".status-column")
        details = _text_pairs(status) if status else {}
        loans.append(
            Loan(
                title=title_el.get_text(" ", strip=True) if title_el else None,
                author=author_el.get_text(" ", strip=True) if author_el else None,
                due_date=parse_finnish_date(details.get("Eräpäivä")),
                renewable=bool(row.select_one("input.checkbox-select-item")),
                details=details,
                record_id=(row.get("id") or "").removeprefix("record") or None,
            )
        )
    form = doc.find("form", attrs={"name": "renewals"})
    renew_ids = [
        inp.get("value")
        for inp in (form.find_all("input", attrs={"name": "renewAllIDS[]"}) if form else [])
        if inp.get("value")
    ]
    csrf_el = form.find("input", attrs={"name": "csrf"}) if form else None
    return loans, renew_ids, csrf_el.get("value") if csrf_el else None


def parse_renew_result(html: str, attempted: set[str]) -> RenewResult:
    """Parse the CheckedOut page returned by a renew POST.

    Only rows in `attempted` (record IDs sent for renewal) count: an overdue
    loan shows the same alert-danger without any renewal attempt. Failures
    for loans hidden from the table are alerts outside the rows, already
    prefixed with the title by Finna.
    """
    doc = BeautifulSoup(html, "html.parser")
    result = RenewResult()
    rows = doc.select("tr.myresearch-row[id^=record]")
    for alert in doc.select(".alert-danger"):
        if alert.find_parent("tr") is None:
            result.failed.append(alert.get_text(" ", strip=True))
    for row in rows:
        if row.get("id", "").removeprefix("record") not in attempted:
            continue
        title_el = row.select_one("h3.record-title")
        title = title_el.get_text(" ", strip=True) if title_el else "?"
        if row.select_one(".status-column .alert-success"):
            result.renewed.append(title)
        elif danger := row.select_one(".status-column .alert-danger"):
            result.failed.append(f"{title}: {danger.get_text(' ', strip=True)}")
    return result


def parse_holds(html: str) -> list[Hold]:
    doc = BeautifulSoup(html, "html.parser")
    holds = []
    for row in doc.select("tr.myresearch-row"):
        title_el = row.select_one("h3.record-title")
        if title_el is None:
            continue
        info = row.select_one(".holds-status-information") or row
        details = _text_pairs(info)
        holds.append(
            Hold(
                title=title_el.get_text(" ", strip=True),
                available=info.select_one(".alert-success") is not None,
                in_transit=info.select_one(".text-success") is not None,
                pickup_location=details.get("Noutopaikka"),
                queue_position=details.get("Sijainti jonossa"),
                expires=parse_finnish_date(details.get("Vanhenee")),
            )
        )
    return holds


HISTORY_TOTAL_RE = re.compile(r"Lainaushistoria\s*\((\d+)\)")


def parse_history_page(html: str) -> tuple[list[HistoryEntry], int | None]:
    """Parse one /Checkouts/History page; returns (entries, total_count)."""
    doc = BeautifulSoup(html, "html.parser")
    m = HISTORY_TOTAL_RE.search(doc.get_text(" ", strip=True))
    total = int(m.group(1)) if m else None
    entries = []
    for row in doc.select("tr.myresearch-row.result, tr.myresearch-row[id^=record]"):
        title_el = row.select_one("h3.record-title")
        if title_el is None:
            continue
        author_el = row.select_one(".record-core-metadata a")
        status = row.select_one(".checkedout-status-information") or row
        details = _text_pairs(status)
        entries.append(
            HistoryEntry(
                title=title_el.get_text(" ", strip=True),
                author=author_el.get_text(" ", strip=True) if author_el else None,
                checkout_date=parse_finnish_date(details.get("Lainauspäivä")),
                return_date=parse_finnish_date(details.get("Palautuspäivä")),
            )
        )
    return entries, total


def parse_saved_searches(html: str) -> list[SavedSearch]:
    """Parse table#saved-searches on /Search/History."""
    doc = BeautifulSoup(html, "html.parser")
    searches = []
    table = doc.select_one("table#saved-searches")
    for row in table.select("tr") if table else []:
        link = row.select_one("td.history_search a")
        if link is None:
            continue
        results_el = row.select_one("td.history_results")
        results = None
        if results_el is not None:
            digits = re.sub(r"[^\d]", "", results_el.get_text(strip=True))
            results = int(digits) if digits else None
        searches.append(
            SavedSearch(
                query=link.get_text(" ", strip=True),
                url=link.get("href"),
                results=results,
            )
        )
    return searches


def parse_fines_total(html: str) -> float | None:
    doc = BeautifulSoup(html, "html.parser")
    total_el = doc.select_one(".js-payment-total-due[data-raw]")
    if total_el is not None:
        try:
            return int(total_el["data-raw"]) / 100
        except (ValueError, TypeError):
            return None
    # No online-payment block on the page: no fines (or not payable online).
    return 0.0 if not doc.select("table.fines-table tbody tr") else None


class FinnaClient:
    """Session-holding client for one library card."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        pin: str,
        host: str = DEFAULT_HOST,
    ) -> None:
        self.base_url = f"https://{host}"
        self._session = session
        self._username = username
        self._pin = pin
        self._headers = {"User-Agent": USER_AGENT}
        self._timeout = aiohttp.ClientTimeout(total=30)

    async def _request(self, method: str, path: str, data=None) -> str:
        start = time.monotonic()
        try:
            async with self._session.request(
                method,
                self.base_url + path,
                data=data,
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                body = await resp.text()
                _LOGGER.debug(
                    "%s %s -> %s in %d ms",
                    method, path, resp.status, (time.monotonic() - start) * 1000,
                )
                resp.raise_for_status()
                return body
        except FinnaError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "%s %s failed after %d ms: %r",
                method, path, (time.monotonic() - start) * 1000, err,
            )
            raise FinnaConnectionError(f"{method} {path}: {err}") from err

    async def _get(self, path: str) -> str:
        return await self._request("GET", path)

    async def _post(self, path: str, data) -> str:
        return await self._request("POST", path, data)

    async def async_login(self) -> None:
        # lng=fi pins the session language: parsing relies on Finnish labels,
        # and the user's own browser sessions are unaffected (issue #2).
        fields = parse_login_form(await self._get("/MyResearch/UserLogin?lng=fi"))
        fields.update(
            {
                "username": self._username,
                "password": self._pin,
                "processLogin": "Kirjaudu",
            }
        )
        body = await self._post("/MyResearch/Home", fields)
        if is_logged_out(body):
            raise FinnaAuthError("Finna rejected the library card number or PIN")

    async def _get_page(self, path: str) -> str:
        """Get a page, re-logging in once if the session has expired."""
        body = await self._get(path)
        if is_logged_out(body):
            await self.async_login()
            body = await self._get(path)
            if is_logged_out(body):
                raise FinnaAuthError("still logged out after re-login")
        return body

    async def async_count_loans_in_year(
        self, year: int, known: tuple[int, int] | None = None
    ) -> tuple[int | None, int | None]:
        """Count history entries checked out in `year`; returns (count, total).

        History is newest-first, so stop as soon as a page only has older
        entries. Capped at 50 pages as a runaway guard. `known` is a
        (count, total) already counted for `year`: if page 1 shows the same
        total, the history hasn't changed and it is returned as is (#5).
        """
        count = 0
        total = None
        prev_first: tuple | None = None
        for page in range(1, 51):
            entries, total = parse_history_page(
                await self._get_page(f"/Checkouts/History?page={page}")
            )
            if page == 1 and known is not None and total == known[1]:
                return known
            if not entries:
                break
            first = (entries[0].title, entries[0].checkout_date)
            dated = [e for e in entries if e.checkout_date]
            # Stop on a repeated page (some servers clamp page=N past the
            # end) or when no dates parse (layout/language changed) — both
            # would otherwise loop to the cap and inflate the count.
            if first == prev_first or not dated:
                break
            prev_first = first
            count += sum(1 for e in dated if e.checkout_date.year == year)
            if any(e.checkout_date.year < year for e in dated):
                break
        return (count if total is not None else None), total

    async def async_get_saved_searches(self) -> list[SavedSearch]:
        return parse_saved_searches(await self._get_page("/Search/History"))

    async def async_get_data(self, previous: FinnaData | None = None) -> FinnaData:
        """Fetch everything; loans and holds must succeed, the rest is optional.

        If an optional page (fines, history, saved searches) fails with a
        connection error, its previous value is kept and the remaining
        optional pages are skipped for this poll — the server is likely slow
        and loans/due dates matter more than a complete poll (issue #4).
        """
        previous = previous or FinnaData()
        year = date.today().year
        # A count carried over from last year would be wrong, not just stale.
        carried_count = (
            previous.loans_this_year if previous.history_year == year else None
        )
        known = (
            (carried_count, previous.history_total)
            if carried_count is not None and previous.history_total is not None
            else None
        )
        loans, renew_ids, csrf = parse_checked_out(
            await self._get_page("/MyResearch/CheckedOut")
        )
        holds = parse_holds(await self._get_page("/Holds/List"))
        data = FinnaData(
            loans=loans,
            holds=holds,
            renew_all_ids=renew_ids,
            renew_csrf=csrf,
            fines_total=previous.fines_total,
            loans_this_year=carried_count,
            history_total=previous.history_total,
            history_year=previous.history_year if carried_count is not None else None,
            saved_searches=previous.saved_searches,
        )
        try:
            data.fines_total = parse_fines_total(
                await self._get_page("/MyResearch/Fines")
            )
            data.loans_this_year, data.history_total = (
                await self.async_count_loans_in_year(year, known)
            )
            data.history_year = year
            data.saved_searches = await self.async_get_saved_searches()
        except FinnaConnectionError as err:
            _LOGGER.warning("Skipping rest of optional Finna pages: %s", err)
        return data

    async def async_renew_all(self) -> RenewResult:
        """Renew all renewable loans; returns which were and weren't renewed."""
        loans, renew_ids, csrf = parse_checked_out(
            await self._get_page("/MyResearch/CheckedOut")
        )
        if not renew_ids or not csrf:
            return RenewResult()
        data = [("renewAll", "1"), ("csrf", csrf)]
        data += [("renewAllIDS[]", i) for i in renew_ids]
        body = await self._post("/MyResearch/CheckedOut", data)
        if is_logged_out(body):
            raise FinnaError("session expired during renewal; nothing was renewed")
        attempted = {loan.record_id for loan in loans if loan.renewable}
        return parse_renew_result(body, attempted)
