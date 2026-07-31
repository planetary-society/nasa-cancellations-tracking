"""The three-pass USAspending termination/clawback source.

The keyword, action-code, and assistance-clawback passes each find awards the
others cannot. These tests pin that no pass can be dropped and that both
sorted seeks fail loudly if the API stops honoring their ordering.

No network: page fetches are stubbed.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from usaspending import Transaction, USASpendingClient

import usaspending_terminations_query as utq
from termination_vocabulary import is_termination
from usaspending_terminations_query import USASpendingTerminationsQuery as Q


def txn_data(
    aid,
    mod,
    action_type="",
    desc="",
    date="2025-06-01",
    amount=-100.0,
    generated_id=None,
):
    return {
        "Award ID": aid,
        "Mod": mod,
        "Action Type": action_type,
        "Action Date": date,
        "Recipient Name": f"Recip {aid}",
        "Transaction Amount": amount,
        "Transaction Description": desc,
        "Awarding Agency": "NASA",
        "generated_internal_id": generated_id or f"CONT_AWD_{aid}",
    }


def txn(
    aid,
    mod,
    action_type="",
    desc="",
    date="2025-06-01",
    amount=-100.0,
    generated_id=None,
):
    return Transaction(
        txn_data(aid, mod, action_type, desc, date, amount, generated_id)
    )


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
    assert set(payload["filters"]["award_type_codes"]) == {
        "A",
        "B",
        "C",
        "D",
        "IDV_A",
        "IDV_B",
        "IDV_B_A",
        "IDV_B_B",
        "IDV_B_C",
        "IDV_C",
        "IDV_D",
        "IDV_E",
    }
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
        "IDV_A",
        "IDV_B",
        "IDV_B_A",
        "IDV_B_B",
        "IDV_B_C",
        "IDV_C",
        "IDV_D",
        "IDV_E",
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


def test_action_code_seek_unions_f_and_n_blocks(monkeypatch):
    client = USASpendingClient()
    page_codes = {1: "E", 2: "F", 3: "G", 4: "N", 5: "X"}

    def make_request(method, endpoint, **kwargs):
        if endpoint == "/search/spending_by_transaction_count/":
            return {"results": {"contracts": 500, "idvs": 0}}
        page = kwargs["json"]["page"]
        code = page_codes[page]
        return {
            "results": [
                txn_data(f"A-{page}-{index}", f"P{index:05d}", code)
                for index in range(100)
            ],
            "page_metadata": {"hasNext": page < 5},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    rows = Q(client=client)._fetch_termination_codes("2025-01-20", "2026-07-30")

    assert len(rows) == 200
    assert {row.action_type for row in rows} == {"F", "N"}


# --- assistance clawback pass ---------------------------------------------


def test_clawback_query_payload_and_threshold_stop(monkeypatch):
    calls = []
    client = USASpendingClient()

    def make_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        page = kwargs["json"]["page"]
        if page > 1:
            pytest.fail("the amount seek paged past the -$10,000 cutoff")
        return {
            "results": [
                *[
                    txn_data(
                        f"G-{index}",
                        f"P{index:05d}",
                        "D",
                        amount=-20_000,
                    )
                    for index in range(98)
                ],
                txn_data("G-2", "P00002", "D", amount=-10_000),
                txn_data("G-3", "P00003", "D", amount=-9_999.99),
            ],
            "page_metadata": {"hasNext": True},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    rows = Q(client=client)._fetch_clawback_candidates("2025-01-20", "2026-07-30")

    assert len(rows) == 99
    assert rows[-1].award_identifier == "G-2"
    assert len(calls) == 1
    method, endpoint, kwargs = calls[0]
    assert method == "POST"
    assert endpoint == "/search/spending_by_transaction/"
    payload = kwargs["json"]
    assert set(payload["filters"]["award_type_codes"]) == {
        "02",
        "03",
        "04",
        "05",
        "F001",
        "F002",
    }
    assert payload["filters"]["agencies"] == utq.NASA_AGENCY_FILTER
    assert payload["filters"]["time_period"] == [
        {"start_date": "2025-01-20", "end_date": "2026-07-30"}
    ]
    assert payload["sort"] == "Transaction Amount"
    assert payload["order"] == "asc"


def test_clawback_amount_seek_rejects_unsorted_rows(monkeypatch):
    client = USASpendingClient()

    def make_request(method, endpoint, **kwargs):
        return {
            "results": [
                txn_data("G-1", "P00001", "D", amount=-20_000),
                txn_data("G-2", "P00002", "D", amount=-30_000),
            ],
            "page_metadata": {"hasNext": False},
        }

    monkeypatch.setattr(client, "_make_request", make_request)

    with pytest.raises(RuntimeError, match="unsorted.*Transaction Amount"):
        Q(client=client)._fetch_clawback_candidates("2025-01-20", "2026-07-30")


@pytest.mark.parametrize(
    ("deob", "current_total", "action_date", "end_date", "expected"),
    [
        (-25_000, 75_000, "2025-06-01", date(2026, 6, 1), True),
        (-24_999, 75_001, "2025-06-01", date(2026, 6, 1), False),
        (-25_000, 75_000, "2026-06-01", date(2026, 6, 1), False),
        (-25_000, 75_000, "2026-06-02", date(2026, 6, 1), False),
    ],
)
def test_clawback_fraction_is_inclusive_and_prematurity_is_strict(
    deob, current_total, action_date, end_date, expected
):
    row = txn("G-1", "P00001", "D", date=action_date, amount=deob)
    award = SimpleNamespace(
        award_amount=Decimal(current_total),
        end_date=end_date,
    )

    hit = Q(client=object())._qualify_clawback(row, award)

    assert (hit is not None) is expected
    if expected:
        assert hit.fraction == Decimal("0.25")
        assert hit.pre_clawback_total == Decimal("100000")


def test_clawback_without_current_end_date_is_unclassifiable():
    """Real live shape: 80NSSC25M7006 has a valid detail record but no end."""
    row = txn("80NSSC25M7006", "P00001", "D", amount=-1_188_655)
    award = SimpleNamespace(
        award_amount=Decimal("0"),
        end_date=None,
    )

    assert Q(client=object())._qualify_clawback(row, award) is None


def test_brown_same_action_end_rewrite_does_not_hide_full_clawback():
    """Live API regression: Brown's clawback rewrote 2028-06-30 to the
    preceding day, so the post-action current end cannot be treated as the
    pre-clawback expiry."""
    row = txn(
        "80NSSC25K0030",
        "P00001",
        "D",
        date="2026-01-20",
        amount=-448_257,
        generated_id="ASST_NON_80NSSC25K0030_080",
    )
    award = SimpleNamespace(
        award_amount=Decimal("0"),
        end_date=date(2026, 1, 19),
    )

    hit = Q(client=object())._qualify_clawback(row, award)

    assert hit is not None
    assert hit.fraction == Decimal("1")


def test_clawback_award_lookup_failure_propagates(monkeypatch):
    row = txn(
        "G-1",
        "P00001",
        "D",
        amount=-25_000,
        generated_id="ASST_NON_G-1_080",
    )

    def fail_lookup():
        raise OSError("batch lookup failed")

    client = SimpleNamespace(awards=SimpleNamespace(search=fail_lookup))
    monkeypatch.setattr(
        Q,
        "_fetch_clawback_candidates",
        lambda self, start, end: [row],
    )

    with pytest.raises(OSError, match="batch lookup failed"):
        Q(client=client)._fetch_clawbacks("2025-01-20", "2026-07-30")


def test_clawback_batch_lookup_is_limited_to_threshold_candidates(monkeypatch):
    rows = [
        txn(
            "G-1",
            "P00001",
            "D",
            amount=-25_000,
            generated_id="ASST_NON_G-1_080",
        ),
        txn(
            "G-2",
            "P00001",
            "D",
            amount=-50_000,
            generated_id="ASST_NON_G-2_080",
        ),
    ]
    seen = []

    class AwardQuery:
        def award_ids(self, *award_ids):
            seen.extend(award_ids)
            return self

        def grants(self):
            return self

        def page_size(self, size):
            assert size == utq.PAGE_SIZE
            return self

        def all(self):
            return [
                SimpleNamespace(
                    award_identifier=aid,
                    award_amount=Decimal("75000"),
                    end_date=date(2026, 12, 31),
                )
                for aid in seen
            ]

    client = SimpleNamespace(awards=SimpleNamespace(search=AwardQuery))
    monkeypatch.setattr(
        Q,
        "_fetch_clawback_candidates",
        lambda self, start, end: rows,
    )

    hits = Q(client=client)._fetch_clawbacks("2025-01-20", "2026-07-30")

    assert seen == ["G-1", "G-2"]
    assert len(hits) == 2


def test_clawback_missing_from_batch_award_lookup_raises(monkeypatch):
    row = txn(
        "G-1",
        "P00001",
        "D",
        amount=-25_000,
        generated_id="ASST_NON_G-1_080",
    )

    class EmptyAwardQuery:
        def award_ids(self, *award_ids):
            return self

        def grants(self):
            return self

        def page_size(self, size):
            return self

        def all(self):
            return []

    client = SimpleNamespace(awards=SimpleNamespace(search=EmptyAwardQuery))
    monkeypatch.setattr(
        Q,
        "_fetch_clawback_candidates",
        lambda self, start, end: [row],
    )

    with pytest.raises(RuntimeError, match="missing.*G-1"):
        Q(client=client)._fetch_clawbacks("2025-01-20", "2026-07-30")


def test_empty_clawback_candidate_sweep_fails_loudly(monkeypatch):
    monkeypatch.setattr(
        Q,
        "_fetch_clawback_candidates",
        lambda self, start, end: [],
    )

    with pytest.raises(RuntimeError, match="clawback.*zero"):
        Q(client=object())._fetch_clawbacks("2025-01-20", "2026-07-30")


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


def test_binary_search_validates_sorting_across_page_boundaries():
    class ProbeQuery:
        def __getitem__(self, key):
            page = key.start // utq.PAGE_SIZE + 1
            code = {1: "N", 2: "F"}[page]
            return [
                txn(f"A-{page}-{index}", f"P{index:05d}", code)
                for index in range(utq.PAGE_SIZE)
            ]

    with pytest.raises(RuntimeError, match="pages 1-2.*unsorted"):
        Q(client=object())._first_code(ProbeQuery(), page=2)


def test_code_block_stop_validates_the_next_page_boundary():
    class ProbeQuery:
        def __getitem__(self, key):
            page = key.start // utq.PAGE_SIZE + 1
            code = {1: "D", 2: "N", 3: "F", 4: "X"}[page]
            return [
                txn(f"A-{page}-{index}", f"P{index:05d}", code)
                for index in range(utq.PAGE_SIZE)
            ]

    query = Q(client=object())
    with pytest.raises(RuntimeError, match="pages 2-3.*unsorted"):
        query._fetch_code_block(ProbeQuery(), "F", total_pages=4)


# --- client lifecycle ------------------------------------------------------


class CloseTrackingClient:
    def __init__(self):
        self._closed = False

    def close(self):
        self._closed = True


def stub_empty_search(monkeypatch):
    monkeypatch.setattr(Q, "_fetch_keyword", lambda self, kw, s, e: [])
    monkeypatch.setattr(Q, "_fetch_termination_codes", lambda self, s, e: [])
    monkeypatch.setattr(Q, "_fetch_clawbacks", lambda self, s, e: [])
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

    def apply(keyword_rows, coded_rows, clawback_hits=()):
        monkeypatch.setattr(
            Q, "_fetch_keyword", lambda self, kw, s, e: list(keyword_rows.get(kw, []))
        )
        monkeypatch.setattr(
            Q, "_fetch_termination_codes", lambda self, s, e: list(coded_rows)
        )
        monkeypatch.setattr(
            Q, "_fetch_clawbacks", lambda self, s, e: list(clawback_hits)
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


def test_n_coded_idv_vehicle_is_recovered_with_vehicle_url(stub):
    """Real CSDA regression: this N-coded IDV has no configured keyword."""
    capella = Transaction(
        txn_data(
            "80HQTR23AA002",
            "P00005",
            "N",
            "LEGAL CONTRACT CANCELLATION - NO LONGER REQUIRED COMMERCIAL "
            "SMALLEST DATA ACQUISITION (CSDA) PROGRAM",
            "2025-03-11",
            generated_id="CONT_IDV_80HQTR23AA002_8000",
        )
    )

    df = stub({}, [capella]).search()

    assert list(df["Award ID"]) == ["80HQTR23AA002"]
    assert (
        df.iloc[0]["source_url"]
        == "https://www.usaspending.gov/award/CONT_IDV_80HQTR23AA002_8000/"
    )
    assert df.iloc[0]["status"].startswith("Legal-contract-cancellation action")


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


def test_default_and_cause_action_codes_are_excluded(stub):
    default = txn("A-E", "P00001", "E", "PROJECT WORK")
    cause = txn("A-X", "P00002", "X", "PROJECT WORK")
    convenience = txn("A-F", "P00003", "F", "PROJECT WORK")
    legal = txn("A-N", "P00004", "N", "PROJECT WORK")

    df = stub({}, [default, convenience, legal, cause]).search()

    assert set(df["Award ID"]) == {"A-F", "A-N"}


def test_brown_premature_clawback_is_emitted_without_synthetic_term_text(
    monkeypatch,
):
    brown = txn(
        "80NSSC25K0030",
        "P00001",
        "D",
        "ADJUSTMENT TO COMPLETED PROJECT",
        "2026-01-20",
        -448_257,
        "ASST_NON_80NSSC25K0030_080",
    )
    expired = txn(
        "POST-EXPIRY",
        "P00009",
        "D",
        "ROUTINE COST UNDERRUN",
        "2026-01-20",
        -25_000,
        "ASST_NON_POST-EXPIRY_080",
    )
    awards = {
        "ASST_NON_80NSSC25K0030_080": SimpleNamespace(
            award_amount=Decimal("0"),
            end_date=date(2028, 7, 21),
        ),
        "ASST_NON_POST-EXPIRY_080": SimpleNamespace(
            award_amount=Decimal("75,000".replace(",", "")),
            end_date=date(2025, 12, 31),
        ),
    }

    class AwardQuery:
        def award_ids(self, *award_ids):
            self.award_ids_requested = award_ids
            return self

        def grants(self):
            return self

        def page_size(self, size):
            return self

        def all(self):
            by_fain = {
                "80NSSC25K0030": awards["ASST_NON_80NSSC25K0030_080"],
                "POST-EXPIRY": awards["ASST_NON_POST-EXPIRY_080"],
            }
            for fain, award in by_fain.items():
                award.award_identifier = fain
            return [by_fain[aid] for aid in self.award_ids_requested]

    client = SimpleNamespace(awards=SimpleNamespace(search=AwardQuery))
    monkeypatch.setattr(
        Q,
        "_fetch_clawback_candidates",
        lambda self, start, end: [brown, expired],
    )
    monkeypatch.setattr(Q, "_fetch_keyword", lambda self, kw, s, e: [])
    monkeypatch.setattr(Q, "_fetch_termination_codes", lambda self, s, e: [])
    monkeypatch.setattr(Q, "export_to_csv", lambda self, df, name: None)

    df = Q(client=client).search()

    assert list(df["Award ID"]) == ["80NSSC25K0030"]
    row = df.iloc[0]
    assert row["source_type"] == "Grant"
    assert row["status"] == (
        "Pure-clawback deobligation P00001 on 2026-01-20 (100% of $448,257)"
    )
    assert row["description"] == "ADJUSTMENT TO COMPLETED PROJECT"
    assert not is_termination(row["description"])
    assert row["source_url"] == (
        "https://www.usaspending.gov/award/ASST_NON_80NSSC25K0030_080/"
    )


def test_keyword_and_clawback_overlap_keeps_newest_signal(stub):
    old_keyword = txn(
        "SHARED",
        "P00001",
        "M",
        "Stop work order",
        "2025-02-01",
    )
    new_clawback = txn(
        "SHARED",
        "P00002",
        "D",
        "ADJUSTMENT TO COMPLETED PROJECT",
        "2025-03-01",
        -25_000,
        "ASST_NON_SHARED_080",
    )
    hit = SimpleNamespace(
        transaction=new_clawback,
        fraction=Decimal("0.25"),
        pre_clawback_total=Decimal("100000"),
    )

    df = stub({"stop work": [old_keyword]}, [], [hit]).search()

    assert list(df["Award ID"]) == ["SHARED"]
    assert df.iloc[0]["status"].startswith(
        "Pure-clawback deobligation P00002 on 2025-03-01"
    )


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
