"""api.doge.gov contract and grant claims, enriched with factual USASpending award status.

DOGE asserts a cancellation; this module records the assertion and, beside it,
what USASpending currently says about the same award. No verdict is reached
here - the right-hand columns are facts (found / not found, latest transaction,
current obligation and end date), and `has_explicit_termination` is the shared
brain's own predicate rather than a second opinion grown locally.
"""

import contextlib
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from usaspending import USASpendingClient
from usaspending.models import get_award_group

from . import api
from .criteria import as_date, is_explicit_termination
from .schema import DogeClaimRow

DOGE_CONTRACTS_ENDPOINT = "https://api.doge.gov/savings/contracts"
DOGE_GRANTS_ENDPOINT = "https://api.doge.gov/savings/grants"
DOGE_PER_PAGE = 500
DOGE_TIMEOUT = 30

# Non-ISO date forms this API has been observed to return for `deleted_date`
# and `date`. ISO is what it sends today; these are the fallbacks.
_DOGE_FALLBACK_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y")

# DOGE reports the agency in one free-text field, in one of two spellings.
_NASA_AGENCY_NAMES = frozenset({"national aeronautics and space administration", "nasa"})

# USAspending's own award URLs carry a composite id - `ASST_NON_80NSSC24K0913_8000`,
# `CONT_AWD_<piid>_<agency>_<parent>_<pa>` - and DOGE's grant links quote it
# verbatim. Award lookups take the PIID or FAIN, so passing the composite
# through matches nothing.
_GENERATED_AWARD_ID_RE = re.compile(r"^(?:ASST_NON|ASST_AGG|CONT_AWD|CONT_IDV)_(.+?)_\d+(?:_|$)")


class DogeFetchError(RuntimeError):
    """The fetch could not be shown to be complete, so it must not be published."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _session() -> requests.Session:
    """A session that retries transient failures and gives up loudly."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _is_nasa_agency(item: dict) -> bool:
    """True when this claim's `agency` field names NASA."""
    agency = item.get("agency")
    return isinstance(agency, str) and agency.strip().lower() in _NASA_AGENCY_NAMES


def _extract_award_id_from_contract_url(url) -> str:
    """The PIID query parameter of an FPDS link, or ""."""
    if not isinstance(url, str) or not url:
        return ""
    piid = parse_qs(urlparse(url).query).get("PIID")
    return piid[0] if piid else ""


def _extract_award_id_from_grant_url(url) -> str:
    """The FAIN behind a usaspending.gov award URL's last path segment, or "".

    The segment is USAspending's *generated* award id, not the FAIN. Returning
    it verbatim made every DOGE grant unmatchable against the award lookup -
    26 claims dropped per run - and gave the same award two identities when
    another source reported it by FAIN.
    """
    if not isinstance(url, str) or not url:
        return ""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    match = _GENERATED_AWARD_ID_RE.match(parts[-1])
    return match.group(1) if match else parts[-1]


def _parse_date(value) -> date | None:
    """A claim date, through the ISO parser first and the legacy forms after."""
    parsed = as_date(value)
    if parsed is not None:
        return parsed
    text = str(value or "").strip()
    for fmt in _DOGE_FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_decimal(value) -> Decimal | None:
    """A DOGE money field as a plain Decimal, or None when it is unreadable."""
    text = "" if value is None else str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _contract_claim(item: dict) -> dict:
    return {
        "claim_type": "contract",
        "doge_award_id": _extract_award_id_from_contract_url(item.get("fpds_link")),
        "recipient": str(item.get("vendor") or "").strip(),
        "doge_value": _to_decimal(item.get("value")),
        "doge_savings": _to_decimal(item.get("savings")),
        "doge_claim_date": _parse_date(item.get("deleted_date")),
        "doge_status": str(item.get("fpds_status") or "").strip(),
        "source_url": str(item.get("fpds_link") or "").strip(),
    }


def _grant_claim(item: dict) -> dict:
    return {
        "claim_type": "grant",
        "doge_award_id": _extract_award_id_from_grant_url(item.get("link")),
        "recipient": str(item.get("recipient") or "").strip(),
        "doge_value": _to_decimal(item.get("value")),
        "doge_savings": _to_decimal(item.get("savings")),
        "doge_claim_date": _parse_date(item.get("date")),
        # Only the contracts endpoint reports a status.
        "doge_status": "",
        "source_url": str(item.get("link") or "").strip(),
    }


def _fetch_endpoint(session, url: str, data_key: str, normalise) -> list[dict]:
    """Every page of one endpoint, filtered to NASA and normalised.

    api.doge.gov exposes no server-side agency filter, so every agency's claims
    come down the wire and `_is_nasa_agency` does the narrowing here.

    The corpus this feeds is committed, so a short read is worse than no read:
    a page that quietly goes missing would delete live claims from the output.
    Every page is therefore accounted for against the count the API itself
    advertises, and anything unexplained raises instead of returning a partial.
    """
    claims: list[dict] = []
    page = 1
    total_pages: int | None = None
    while total_pages is None or page <= total_pages:
        response = session.get(
            url, params={"page": page, "per_page": DOGE_PER_PAGE}, timeout=DOGE_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise DogeFetchError(f"{url}: page {page} reported an unsuccessful status")
        if total_pages is None:
            total_pages = payload.get("meta", {}).get("pages")
            if not isinstance(total_pages, int) or total_pages < 1:
                raise DogeFetchError(f"{url}: unusable meta.pages {total_pages!r}")
        items = payload.get("result", {}).get(data_key) or []
        if not items:
            raise DogeFetchError(f"{url}: page {page} of {total_pages} came back empty")
        claims.extend(normalise(item) for item in items if _is_nasa_agency(item))
        page += 1
    return claims


def fetch_claims() -> list[dict]:
    """Every NASA contract and grant claim on DOGE's wall of receipts."""
    with _session() as session:
        return _fetch_endpoint(
            session, DOGE_CONTRACTS_ENDPOINT, "contracts", _contract_claim
        ) + _fetch_endpoint(session, DOGE_GRANTS_ENDPOINT, "grants", _grant_claim)


# ---------------------------------------------------------------------------
# Factual enrichment
# ---------------------------------------------------------------------------


def _enrich_claim(claim: dict, client, today: date) -> DogeClaimRow:
    """One claim plus what USASpending says about the award it names.

    Lookup and transaction failures are left to propagate: the ORM has already
    retried, and a row that silently reports "not found" because the API was
    down would be published as fact.
    """
    award = (
        client.awards.find_by_award_id(claim["doge_award_id"]) if claim["doge_award_id"] else None
    )
    if award is None:
        return DogeClaimRow(**claim, checked_date=today)

    # Normalised through the ORM's own mapping so "loan", "other" and anything
    # unrecognised become "", rather than reaching the published column - or the
    # FPDS action-code gate in `is_explicit_termination` - as a raw category.
    award_type = get_award_group(award.category or "") or ""
    transactions = award.transactions.all()
    # Only the latest transaction needs an ordering; the termination test below
    # is an any() over all of them, which no sort can change.
    latest = (
        max(transactions, key=lambda t: (t.action_date or date.min, t.modification_number or ""))
        if transactions
        else None
    )
    period = award.period_of_performance
    # The lookup is search-backed, so the row already carries the same location
    # fields the API door's award search reads - no further request, and the
    # same raw-payload mapping via the shared helper.
    recipient_location = api.location_from_payload(award.get_value(["Recipient Location"]))
    pop_location = api.location_from_payload(award.get_value(["Primary Place of Performance"]))
    return DogeClaimRow(
        **claim,
        usaspending_found=True,
        generated_award_id=award.generated_unique_award_id or "",
        award_type=award_type,
        has_explicit_termination=any(
            is_explicit_termination(
                api.orm_txn(
                    txn,
                    award_key="",
                    award_id="",
                    generated_award_id="",
                    award_type=award_type,
                )
            )
            for txn in transactions
        ),
        latest_action_date=latest.action_date if latest else None,
        latest_action_type=(latest.action_type or "") if latest else "",
        latest_description=(latest.transaction_description or "") if latest else "",
        current_obligation=award.total_obligation,
        current_end_date=period.end_date if period else None,
        recipient_address1=recipient_location.address1,
        recipient_address2=recipient_location.address2,
        recipient_city=recipient_location.city,
        recipient_state=recipient_location.state,
        recipient_zip=recipient_location.zip,
        pop_city=pop_location.city,
        pop_state=pop_location.state,
        pop_zip=pop_location.zip,
        checked_date=today,
    )


def is_settled(row: DogeClaimRow, today: date) -> bool:
    """True when USASpending can no longer say anything new about this claim.

    An award that is explicitly terminated, or whose current period of
    performance has already ended, is done changing - so its row never needs
    re-enrichment. (A settled termination that later gets reinstated would go
    unnoticed; accepting that is what keeps the daily run off USASpending for
    the bulk of the corpus.) A not-found row is never settled: the award may
    simply not have reached USASpending yet.
    """
    if not row.usaspending_found:
        return False
    if row.has_explicit_termination:
        return True
    return row.current_end_date is not None and row.current_end_date < today


def enrich(
    claims,
    client=None,
    *,
    refresh_days: int = 14,
    today: date | None = None,
    existing=(),
) -> list[DogeClaimRow]:
    """Claims as output rows, reusing settled and recently checked ones.

    A published claim is immutable: DOGE never edits a receipt once it is on
    the wall, so a row's DOGE-side columns are fixed by (award id, claim date)
    and only the USASpending side can go stale. Settled rows (see `is_settled`)
    are carried over verbatim forever; the rest are re-enriched once older than
    `refresh_days`, which holds a daily run to a small slice of the corpus.

    The client is built lazily and only if some claim actually needs enriching -
    a run that carries the whole corpus over never opens one. A client this
    function opens, it closes; a caller-supplied one is left alone.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=refresh_days)
    # Claims whose id extraction failed all share the empty award id, so on a
    # (id, date) key they collide: two same-day unidentifiable claims would come
    # back from the cache as two copies of whichever one was stored last, and
    # the other claim's recipient and dollar figures would vanish from the
    # output. They are excluded rather than disambiguated - with no award id
    # there is no lookup to skip, so re-enriching one costs nothing.
    fresh = {
        (row.doge_award_id, row.doge_claim_date): row
        for row in existing
        if row.doge_award_id
        and (is_settled(row, today) or (row.checked_date is not None and row.checked_date > cutoff))
    }

    rows = []
    with contextlib.ExitStack() as stack:
        for claim in claims:
            cached = fresh.get((claim["doge_award_id"], claim["doge_claim_date"]))
            if cached is not None:
                rows.append(cached)
                continue
            # An id-less claim is never cached (above) but never looked up
            # either, so it must not be what opens the client.
            if client is None and claim["doge_award_id"]:
                client = stack.enter_context(USASpendingClient())
            rows.append(_enrich_claim(claim, client, today))

    rows.sort(key=lambda row: row.doge_award_id)
    rows.sort(key=lambda row: row.doge_claim_date or date.min, reverse=True)
    return rows
