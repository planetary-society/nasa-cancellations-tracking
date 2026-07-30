"""The two-pass USAspending termination source.

The keyword pass and the action-code pass each find awards the other cannot:
stop-work language usually carries no distinguishing code, and formal
terminate-for-convenience mods usually carry no termination text. These tests
pin that neither pass can be dropped, and that the seek fails loudly if the
API ever stops honouring the sort it depends on.

No network: page fetches are stubbed.
"""

import pytest
from usaspending import Transaction, USASpendingClient

import usaspending_terminations_query as utq
from usaspending_terminations_query import USASpendingTerminationsQuery as Q


def txn_data(aid, mod, action_type="", desc="", date="2025-06-01", amount=-100.0):
    return {
        "Award ID": aid,
        "Mod": mod,
        "Action Type": action_type,
        "Action Date": date,
        "Recipient Name": f"Recip {aid}",
        "Transaction Amount": amount,
        "Transaction Description": desc,
        "Awarding Agency": "NASA",
        "generated_internal_id": f"CONT_AWD_{aid}",
    }


def txn(aid, mod, action_type="", desc="", date="2025-06-01", amount=-100.0):
    return Transaction(txn_data(aid, mod, action_type, desc, date, amount))


# --- ORM integration -------------------------------------------------------


def test_keyword_search_uses_global_transaction_query(monkeypatch):
    calls = []
    client = USASpendingClient()

    def make_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {
            "results": [txn_data("A-1", "P00001", "M", "Stop work order")],
            "page_metadata": {"hasNext": False},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    rows = Q(client=client)._fetch_keyword("stop work", "2025-01-20", "2026-07-30")

    assert len(rows) == 1
    assert isinstance(rows[0], Transaction)
    assert len(calls) == 1

    method, endpoint, kwargs = calls[0]
    assert method == "POST"
    assert endpoint == "/search/spending_by_transaction/"
    payload = kwargs["json"]
    assert set(payload["filters"]["award_type_codes"]) == {"A", "B", "C", "D"}
    assert payload["filters"]["agencies"] == utq.NASA_AGENCY_FILTER
    assert payload["filters"]["time_period"] == [
        {"start_date": "2025-01-20", "end_date": "2026-07-30"}
    ]
    assert payload["filters"]["keywords"] == ["stop work"]
    assert payload["fields"] == Transaction.SEARCH_FIELDS
    assert payload["limit"] == 100
    assert payload["page"] == 1
    assert payload["sort"] == "Action Date"
    assert payload["order"] == "desc"


def test_action_code_search_uses_orm_count_and_sort(monkeypatch):
    calls = []
    client = USASpendingClient()

    def make_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if endpoint == "/search/spending_by_transaction_count/":
            return {"results": {"contracts": 3}}
        return {
            "results": [
                txn_data("A-1", "P00001", "E"),
                txn_data("A-2", "P00002", "F"),
                txn_data("A-3", "P00003", "G"),
            ],
            "page_metadata": {"hasNext": False},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    rows = Q(client=client)._fetch_termination_codes("2025-01-20", "2026-07-30")

    assert [row.award_identifier for row in rows] == ["A-2"]
    endpoints = [endpoint for _, endpoint, _ in calls]
    assert "/search/spending_by_transaction_count/" in endpoints
    assert "/search/spending_by_transaction/" in endpoints

    search_payload = next(
        kwargs["json"]
        for _, endpoint, kwargs in calls
        if endpoint == "/search/spending_by_transaction/"
    )
    assert search_payload["sort"] == "Action Type"
    assert search_payload["order"] == "asc"
    assert set(search_payload["filters"]["award_type_codes"]) == {
        "A",
        "B",
        "C",
        "D",
    }
    assert search_payload["filters"]["agencies"] == utq.NASA_AGENCY_FILTER
    assert search_payload["filters"]["time_period"] == [
        {"start_date": "2025-01-20", "end_date": "2026-07-30"}
    ]


def test_action_code_seek_treats_blank_tail_as_sorting_after_codes(monkeypatch):
    client = USASpendingClient()
    page_codes = {1: "D", 2: "E", 3: "F"}

    def make_request(method, endpoint, **kwargs):
        if endpoint == "/search/spending_by_transaction_count/":
            return {"results": {"contracts": 800}}
        page = kwargs["json"]["page"]
        code = page_codes.get(page, "")
        return {
            "results": [
                txn_data(f"A-{page}-{index}", f"P{index:05d}", code)
                for index in range(100)
            ],
            "page_metadata": {"hasNext": page < 8},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    rows = Q(client=client)._fetch_termination_codes("2025-01-20", "2026-07-30")

    assert len(rows) == 100
    assert {row.action_type for row in rows} == {"F"}


# --- the sort guard --------------------------------------------------------


def test_assert_sorted_accepts_ordered_codes():
    Q._assert_sorted(
        [txn("A-1", "P1", "D"), txn("A-2", "P1", "F"), txn("A-3", "P1", "G")], page=1
    )


def test_assert_sorted_accepts_trailing_blank_codes():
    """Blank action types sort last and are valid at the end of a page."""
    Q._assert_sorted(
        [txn("A-1", "P1", "F"), txn("A-2", "P1", ""), txn("A-3", "P1", "")], page=1
    )


def test_assert_sorted_raises_when_the_api_stops_sorting():
    """The seek reads only a slice of the data, so an unhonoured sort would
    silently return the wrong slice and look like 'no terminations today'."""
    with pytest.raises(RuntimeError, match="unsorted"):
        Q._assert_sorted([txn("A-1", "P1", "M"), txn("A-2", "P1", "B")], page=7)


def test_binary_search_validates_probe_page_sorting():
    class ProbeQuery:
        def __getitem__(self, key):
            page = key.start // utq.PAGE_SIZE + 1
            if page == 2:
                return [txn("A-1", "P1", "M"), txn("A-2", "P1", "B")]
            return [txn("A-3", "P1", "D")]

    with pytest.raises(RuntimeError, match="page 2 unsorted"):
        Q(client=object())._lower_bound(ProbeQuery(), "F", total_pages=4)


# --- client lifecycle ------------------------------------------------------


class CloseTrackingClient:
    def __init__(self):
        self._closed = False

    def close(self):
        self._closed = True


def stub_empty_search(monkeypatch):
    monkeypatch.setattr(Q, "_fetch_keyword", lambda self, kw, s, e: [])
    monkeypatch.setattr(Q, "_fetch_termination_codes", lambda self, s, e: [])
    monkeypatch.setattr(Q, "export_to_csv", lambda self, df, name: None)


def test_search_closes_client_it_created(monkeypatch):
    client = CloseTrackingClient()
    monkeypatch.setattr(utq, "USASpendingClient", lambda: client)
    stub_empty_search(monkeypatch)

    Q().search()

    assert client._closed


def test_search_leaves_injected_client_open(monkeypatch):
    client = CloseTrackingClient()
    stub_empty_search(monkeypatch)

    Q(client=client).search()

    assert not client._closed


# --- the union -------------------------------------------------------------


@pytest.fixture
def stub(monkeypatch):
    """Stub both passes so search() can be exercised offline."""

    def apply(keyword_rows, coded_rows):
        monkeypatch.setattr(
            Q, "_fetch_keyword", lambda self, kw, s, e: list(keyword_rows.get(kw, []))
        )
        monkeypatch.setattr(
            Q, "_fetch_termination_codes", lambda self, s, e: list(coded_rows)
        )
        monkeypatch.setattr(Q, "export_to_csv", lambda self, df, name: None)
        return Q()

    return apply


def test_code_only_award_is_recovered(stub):
    """80MSFC22CA005 (MAVIS, $103M): action code F, description is the project
    name, no termination language anywhere. Keywords alone never find it."""
    mavis = txn(
        "80MSFC22CA005",
        "P00032",
        "F",
        "MARS ASCENT VEHICLE INTEGRATED SYSTEM (MAVIS)",
        "2025-09-30",
    )
    df = stub({}, [mavis]).search()
    assert list(df["Award ID"]) == ["80MSFC22CA005"]
    assert df.iloc[0]["status"].startswith("Terminate-for-convenience action")
    assert df.iloc[0]["description"] == "MARS ASCENT VEHICLE INTEGRATED SYSTEM (MAVIS)"
    assert isinstance(df.iloc[0]["value"], float)


def test_keyword_only_award_is_kept(stub):
    """Stop-work language often carries no distinguishing action code, so the
    code pass alone would lose it."""
    sw = txn("A-1", "P00002", "M", "Stop work order issued")
    df = stub({"stop work": [sw]}, []).search()
    assert list(df["Award ID"]) == ["A-1"]
    assert df.iloc[0]["status"].startswith("Termination-language transaction")


def test_both_passes_union_without_duplicating(stub):
    shared = txn("A-1", "P00003", "F", "terminate for convenience", "2025-05-01")
    df = stub({"terminate for convenience": [shared]}, [shared]).search()
    assert list(df["Award ID"]) == ["A-1"]


def test_most_recent_transaction_wins(stub):
    old = txn("A-1", "P00001", "M", "Stop work order", "2025-01-01")
    new = txn("A-1", "P00009", "F", "MARS THING", "2026-01-01")
    df = stub({"stop work": [old]}, [new]).search()
    assert len(df) == 1
    assert "P00009" in df.iloc[0]["status"]


def test_termination_for_cause_still_excluded(stub):
    cause = txn("A-1", "P00002", "F", "terminated for cause of contractor")
    df = stub({}, [cause]).search()
    assert df.empty


def test_rows_without_an_award_id_are_skipped(stub):
    df = stub({}, [txn("", "P00001", "F", "MARS THING")]).search()
    assert df.empty


# --- keyword list --------------------------------------------------------


def test_dead_and_duplicate_keywords_stay_removed():
    """Verified against the live API 2026-07-30: 'terminated for convenience'
    matched nothing, and 'stop-work' returned a byte-identical set to
    'stop work'. Both were dropped; this pins them out."""
    assert "terminated for convenience" not in utq.SEARCH_KEYWORDS
    assert "stop-work" not in utq.SEARCH_KEYWORDS
    assert utq.SEARCH_KEYWORDS == [
        "terminate for convenience",
        "termination for convenience",
        "stop work",
    ]
