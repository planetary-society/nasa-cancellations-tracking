"""DOGE fetch: id extraction, the NASA filter, truncation failure, and the checked_date cache."""

from datetime import date
from decimal import Decimal

import pytest
import requests
import responses

from nasatrack import doge
from nasatrack.schema import DogeClaimRow

CONTRACTS = doge.DOGE_CONTRACTS_ENDPOINT
GRANTS = doge.DOGE_GRANTS_ENDPOINT

NASA = "National Aeronautics and Space Administration"


def _contract(piid="80NSSC25C0001", agency=NASA):
    return {
        "agency": agency,
        "vendor": "Acme Aerospace",
        "value": 1_000_000,
        "savings": "500,000",
        "deleted_date": "3/4/2025",
        "fpds_status": "Deleted",
        "fpds_link": f"https://www.fpds.gov/ezsearch/search.do?PIID={piid}&s=FPDS",
        "description": "whatever",
    }


def _grant(link="https://usaspending.gov/award/ASST_NON_80NSSC24K0913_8000", agency=NASA):
    return {
        "agency": agency,
        "recipient": "State University",
        "value": 250_000,
        "savings": 250_000,
        "date": "2025-05-06",
        "link": link,
    }


def _page(data_key, items, pages=1):
    return {"success": True, "meta": {"pages": pages}, "result": {data_key: items}}


def _register(contract_pages, grant_pages):
    """Register one JSON body per page, in order, for both endpoints."""
    for body in contract_pages:
        responses.add(responses.GET, CONTRACTS, json=body, status=200)
    for body in grant_pages:
        responses.add(responses.GET, GRANTS, json=body, status=200)


# ---------------------------------------------------------------------------
# Extraction and filtering
# ---------------------------------------------------------------------------


def test_contract_award_id_comes_from_the_fpds_link_query_string():
    url = "https://www.fpds.gov/ezsearch/search.do?s=FPDS&indexName=awardfull&PIID=80JSC022CA012"
    assert doge._extract_award_id_from_contract_url(url) == "80JSC022CA012"


def test_missing_or_unparseable_contract_link_yields_no_award_id():
    assert doge._extract_award_id_from_contract_url(None) == ""
    assert doge._extract_award_id_from_contract_url("https://www.fpds.gov/no/query") == ""


def test_grant_award_id_is_the_fain_inside_the_generated_award_id():
    url = "https://usaspending.gov/award/ASST_NON_80NSSC24K0913_8000"
    assert doge._extract_award_id_from_grant_url(url) == "80NSSC24K0913"


def test_grant_url_that_is_not_a_generated_id_is_returned_verbatim():
    assert doge._extract_award_id_from_grant_url(
        "https://usaspending.gov/award/80NSSC24K0913/"
    ) == ("80NSSC24K0913")


@responses.activate
def test_non_nasa_items_are_filtered_out():
    _register(
        [_page("contracts", [_contract(), _contract(piid="X", agency="Department of Energy")])],
        [_page("grants", [_grant(agency="NASA"), _grant(agency="Department of Education")])],
    )
    claims = doge.fetch_claims()
    assert [claim["claim_type"] for claim in claims] == ["contract", "grant"]
    assert claims[0]["doge_award_id"] == "80NSSC25C0001"
    assert claims[0]["doge_value"] == Decimal("1000000")
    assert claims[0]["doge_savings"] == Decimal("500000")
    assert claims[0]["doge_claim_date"] == date(2025, 3, 4)
    assert claims[0]["doge_status"] == "Deleted"
    assert claims[1]["doge_award_id"] == "80NSSC24K0913"
    assert claims[1]["doge_claim_date"] == date(2025, 5, 6)
    assert claims[1]["doge_status"] == ""


# ---------------------------------------------------------------------------
# A short read must never publish
# ---------------------------------------------------------------------------


@responses.activate
def test_fewer_pages_than_meta_advertises_raises():
    _register(
        [_page("contracts", [_contract()], pages=3), _page("contracts", [], pages=3)],
        [_page("grants", [_grant()])],
    )
    with pytest.raises(doge.DogeFetchError):
        doge.fetch_claims()


@responses.activate
def test_a_failing_page_raises_rather_than_returning_a_partial_list():
    responses.add(responses.GET, CONTRACTS, json=_page("contracts", [_contract()], pages=2))
    responses.add(responses.GET, CONTRACTS, json={"error": "boom"}, status=500)
    with pytest.raises(requests.RequestException):
        doge.fetch_claims()


@responses.activate
def test_missing_pagination_metadata_raises():
    _register([{"success": True, "result": {"contracts": [_contract()]}}], [])
    with pytest.raises(doge.DogeFetchError):
        doge.fetch_claims()


@responses.activate
def test_unsuccessful_payload_raises():
    _register([{"success": False, "meta": {"pages": 1}, "result": {}}], [])
    with pytest.raises(doge.DogeFetchError):
        doge.fetch_claims()


# ---------------------------------------------------------------------------
# Enrichment against a fake ORM
# ---------------------------------------------------------------------------


class FakeTransaction:
    """The ORM transaction shape `api.orm_txn` reads."""

    def __init__(self, action_date, action_type="", description="", mod="1"):
        self.action_date = action_date
        self.action_type = action_type
        self.transaction_description = description
        self.modification_number = mod
        self.recipient_name = "Acme Aerospace"
        self.federal_action_obligation = None


class FakeTransactions:
    def __init__(self, transactions):
        self._transactions = transactions

    def all(self):
        return list(self._transactions)


class FakePeriod:
    def __init__(self, end_date):
        self.end_date = end_date


class FakeAward:
    def __init__(self, category="contract", transactions=(), data=None):
        self.category = category
        self.generated_unique_award_id = "CONT_AWD_80NSSC25C0001_8000_-NONE-_-NONE-"
        self.total_obligation = Decimal("1234.00")
        self.period_of_performance = FakePeriod(date(2026, 9, 30))
        self.transactions = FakeTransactions(transactions)
        self._data = data or {}

    def get_value(self, keys, default=None):
        for key in keys:
            if self._data.get(key) is not None:
                return self._data[key]
        return default


class FakeAwards:
    def __init__(self, award):
        self.award = award
        self.calls = []

    def find_by_award_id(self, award_id):
        self.calls.append(award_id)
        return self.award


class FakeClient:
    def __init__(self, award=None):
        self.awards = FakeAwards(award)


def _claim(award_id="80NSSC25C0001", claim_date=date(2025, 3, 4)):
    return {
        "claim_type": "contract",
        "doge_award_id": award_id,
        "recipient": "Acme Aerospace",
        "doge_value": Decimal("1000000"),
        "doge_savings": Decimal("500000"),
        "doge_claim_date": claim_date,
        "doge_status": "Deleted",
        "source_url": "https://www.fpds.gov/ezsearch/search.do?PIID=80NSSC25C0001",
    }


def _row(**overrides):
    return DogeClaimRow(**{**_claim(), **overrides})


def test_a_fresh_existing_row_is_carried_over_without_an_orm_call():
    cached = _row(
        usaspending_found=True, latest_description="cached", checked_date=date(2026, 8, 10)
    )
    client = FakeClient(FakeAward())
    rows = doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[cached])
    assert rows == [cached]
    assert client.awards.calls == []


def test_a_stale_existing_row_is_re_enriched():
    stale = _row(usaspending_found=True, latest_description="stale", checked_date=date(2026, 7, 1))
    client = FakeClient(
        FakeAward(transactions=[FakeTransaction(date(2025, 4, 1), "C", "admin mod")])
    )
    rows = doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[stale])
    assert client.awards.calls == ["80NSSC25C0001"]
    assert rows[0].checked_date == date(2026, 8, 20)
    assert rows[0].latest_description == "admin mod"


def test_enrichment_carries_the_award_locations():
    # find_by_award_id is search-backed, so the locations ride on the same
    # fields the API door's award search reads - and a POP never has address
    # lines, so its address columns stay "".
    client = FakeClient(
        FakeAward(
            transactions=[FakeTransaction(date(2025, 4, 1), "C", "admin mod")],
            data={
                "Recipient Location": {
                    "address_line1": "1 ROCKET RD",
                    "city_name": "HAWTHORNE",
                    "state_code": "CA",
                    "zip5": "90250",
                },
                "Primary Place of Performance": {
                    "city_name": "PASADENA",
                    "state_code": "CA",
                    "zip5": "91109",
                },
            },
        )
    )
    (enriched,) = doge.enrich([_claim()], client, today=date(2026, 8, 20))
    assert enriched.recipient_address1 == "1 ROCKET RD"
    assert enriched.recipient_city == "HAWTHORNE"
    assert enriched.recipient_state == "CA"
    assert enriched.recipient_zip == "90250"
    assert enriched.pop_city == "PASADENA"
    assert enriched.pop_state == "CA"
    assert enriched.pop_zip == "91109"


def test_a_settled_terminated_row_is_never_re_enriched():
    # Explicitly terminated: nothing USASpending says can change the row, so
    # even a checked_date far past refresh_days carries over with no ORM call.
    settled = _row(
        usaspending_found=True, has_explicit_termination=True, checked_date=date(2025, 5, 1)
    )
    client = FakeClient(FakeAward())
    rows = doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[settled])
    assert rows == [settled]
    assert client.awards.calls == []


def test_a_settled_expired_row_is_never_re_enriched():
    # The award's period of performance ended; the record is done changing.
    settled = _row(
        usaspending_found=True, current_end_date=date(2026, 1, 31), checked_date=date(2026, 2, 1)
    )
    client = FakeClient(FakeAward())
    rows = doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[settled])
    assert rows == [settled]
    assert client.awards.calls == []


def test_a_live_unterminated_award_is_not_settled():
    # Found, not terminated, end date still ahead: stale rows re-enrich.
    live = _row(
        usaspending_found=True, current_end_date=date(2027, 9, 30), checked_date=date(2026, 7, 1)
    )
    client = FakeClient(FakeAward())
    doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[live])
    assert client.awards.calls == ["80NSSC25C0001"]


def test_a_not_found_row_is_never_settled():
    # The award may simply not have reached USASpending yet - keep checking.
    missing = _row(usaspending_found=False, checked_date=date(2026, 7, 1))
    client = FakeClient(None)
    doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[missing])
    assert client.awards.calls == ["80NSSC25C0001"]


def test_a_new_claim_is_enriched():
    award = FakeAward(transactions=[FakeTransaction(date(2025, 4, 1), "C", "base award")])
    client = FakeClient(award)
    (row,) = doge.enrich([_claim()], client, today=date(2026, 8, 20), existing=[])
    assert client.awards.calls == ["80NSSC25C0001"]
    assert row.usaspending_found is True
    assert row.generated_award_id == award.generated_unique_award_id
    assert row.award_type == "contract"
    assert row.latest_action_date == date(2025, 4, 1)
    assert row.latest_action_type == "C"
    assert row.current_obligation == Decimal("1234.00")
    assert row.current_end_date == date(2026, 9, 30)
    assert row.checked_date == date(2026, 8, 20)
    assert row.has_explicit_termination is False


def test_an_award_usaspending_does_not_have_leaves_the_right_hand_columns_empty():
    client = FakeClient(None)
    (row,) = doge.enrich([_claim()], client, today=date(2026, 8, 20))
    assert row.usaspending_found is False
    assert row.generated_award_id == ""
    assert row.award_type == ""
    assert row.latest_action_date is None
    assert row.current_obligation is None
    assert row.checked_date == date(2026, 8, 20)
    # The DOGE side survives regardless.
    assert row.doge_award_id == "80NSSC25C0001"
    assert row.doge_status == "Deleted"


def test_a_claim_with_no_extractable_award_id_is_never_looked_up():
    client = FakeClient(FakeAward())
    (row,) = doge.enrich([_claim(award_id="")], client, today=date(2026, 8, 20))
    assert client.awards.calls == []
    assert row.usaspending_found is False


def test_same_day_claims_without_award_ids_do_not_collide_in_the_cache():
    # The cache is keyed on (award id, claim date), and every claim whose id
    # extraction failed carries the SAME empty id - so two same-day ones would
    # come back as two copies of whichever was cached last, silently replacing
    # one claim's recipient and dollar figures with the other's. Id-less rows
    # are excluded from the cache instead; they cost no ORM call to rebuild.
    claims = [
        {**_claim(award_id=""), "recipient": "Acme Aerospace"},
        {**_claim(award_id=""), "recipient": "Beta Dynamics", "doge_value": Decimal("42")},
    ]
    client = FakeClient(FakeAward())
    first = doge.enrich(claims, client, today=date(2026, 8, 20))
    assert sorted(row.recipient for row in first) == ["Acme Aerospace", "Beta Dynamics"]

    # Second run, with the first run's own output as the cache.
    second = doge.enrich(claims, client, today=date(2026, 8, 20), existing=first)
    assert sorted(row.recipient for row in second) == ["Acme Aerospace", "Beta Dynamics"]
    assert sorted(row.doge_value for row in second) == [Decimal("42"), Decimal("1000000")]
    assert client.awards.calls == []


# ---------------------------------------------------------------------------
# has_explicit_termination is the shared brain's answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transaction", "expected"),
    [
        (FakeTransaction(date(2025, 6, 1), "F", "modification"), True),
        # An N code alone is not evidence - NASA applies it to routine admin
        # actions too; it only ever confirms termination language.
        (FakeTransaction(date(2025, 6, 1), "N", "modification"), False),
        (FakeTransaction(date(2025, 6, 1), "N", "terminate for convenience"), True),
        (
            FakeTransaction(date(2025, 6, 1), "C", "TERMINATION FOR CONVENIENCE OF THE GOVERNMENT"),
            True,
        ),
        (FakeTransaction(date(2025, 6, 1), "C", "terminate for convience"), True),
        (FakeTransaction(date(2025, 6, 1), "C", "TERMINATION FOR CAUSE"), False),
        (FakeTransaction(date(2025, 6, 1), "F", "TERMINATION FOR CAUSE"), False),
        (FakeTransaction(date(2025, 6, 1), "C", "routine administrative modification"), False),
    ],
)
def test_has_explicit_termination(transaction, expected):
    client = FakeClient(FakeAward(transactions=[transaction]))
    (row,) = doge.enrich([_claim()], client, today=date(2026, 8, 20))
    assert row.has_explicit_termination is expected


def test_grants_have_no_action_codes_so_language_is_the_only_evidence():
    award = FakeAward(
        category="grant",
        transactions=[FakeTransaction(date(2025, 6, 1), "F", "continuation increment")],
    )
    (row,) = doge.enrich([_claim()], FakeClient(award), today=date(2026, 8, 20))
    assert row.has_explicit_termination is False


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_rows_sort_by_claim_date_descending_then_award_id():
    claims = [
        _claim(award_id="B", claim_date=date(2025, 3, 4)),
        _claim(award_id="A", claim_date=date(2025, 3, 4)),
        _claim(award_id="C", claim_date=date(2025, 9, 1)),
    ]
    rows = doge.enrich(claims, FakeClient(None), today=date(2026, 8, 20))
    assert [(row.doge_award_id, row.doge_claim_date) for row in rows] == [
        ("C", date(2025, 9, 1)),
        ("A", date(2025, 3, 4)),
        ("B", date(2025, 3, 4)),
    ]
