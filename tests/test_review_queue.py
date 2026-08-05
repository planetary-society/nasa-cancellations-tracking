"""The review queue search.py prints at the end of a run.

An award a source flagged but USAspending could not resolve never reaches the
snapshot and leaves no trace in it. Before this report, 26 DOGE-claimed grants
were dropped on every run with no output at all.
"""

import pandas as pd
import pytest

import award_transaction_facts as transaction_facts
import build_master_ledger as bml
import search as s


def test_search_closes_usaspending_client_when_source_fails(monkeypatch):
    class CloseTrackingClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FailingSource:
        def search(self):
            raise OSError("source unavailable")

    client = CloseTrackingClient()
    monkeypatch.setattr(s, "USASpendingClient", lambda: client)
    search = s.Search()
    search.sources = {"Broken": FailingSource}

    with pytest.raises(RuntimeError, match="Source 'Broken' failed"):
        search.search()

    assert client.closed


class FakeAward:
    """Minimal stand-in for a usaspending Award."""

    def __init__(
        self,
        award_id,
        *,
        category="contract",
        end_date="2026-12-31",
        last_date_to_order=None,
    ):
        self.award_identifier = award_id
        self.category = category
        self.raw = {"Last Date to Order": last_date_to_order}
        self.usa_spending_url = (
            f"https://www.usaspending.gov/award/CONT_AWD_{award_id}/"
        )
        self.award_amount = 1000
        self.total_outlay = 500
        self.description = "desc"
        self.transactions = []
        self.period_of_performance = type(
            "P",
            (),
            {
                "start_date": "2025-01-01",
                "end_date": end_date,
            },
        )()
        self.recipient = type(
            "R",
            (),
            {
                "name": "Recip",
                "business_types": [],
                "location": type("L", (), {"district": "CA-01"})(),
            },
        )()


def make_search(source_rows, awards, ignore=()):
    obj = s.Search.__new__(s.Search)
    obj.sources = {name: None for name in source_rows}
    obj.sources_cancellation_data = {
        name: pd.DataFrame(rows) for name, rows in source_rows.items()
    }
    obj.unique_cancellations = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = list(ignore)
    obj.window_rejects = []
    obj.awards = awards
    obj.awards_by_id = {a.award_identifier: a for a in awards}
    for name, rows in source_rows.items():
        obj._add_source_awards(name, [r["Award ID"] for r in rows])
    return obj


def test_a_built_row_fills_every_snapshot_column():
    """The snapshot writer cannot report a column-name mismatch.

    search.py writes with `pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)`, so a
    name that is renamed in the constant but not in the row builder produces a
    column of empty strings rather than an error - it would take a person
    noticing a blank column in published data.
    """
    obj = make_search({"NASA Procurement Data View": [row("A-1")]}, [FakeAward("A-1")])

    built = set(obj.unique_cancellations["A-1"])

    assert built == set(s.SNAPSHOT_COLUMNS), (
        f"only in the row builder: {sorted(built - set(s.SNAPSHOT_COLUMNS))}; "
        f"only in SNAPSHOT_COLUMNS: {sorted(set(s.SNAPSHOT_COLUMNS) - built)}"
    )


def row(aid, desc="terminated", status="", action_date="2025-06-01", basis="evidence"):
    """One source row. Defaults are in-window so window enforcement is opt-in.

    The tracking-window fields are part of every source's output contract now
    (contract_query.FINAL_COLUMNS), so a fixture that omits them is not a
    smaller fixture - it is an invalid one, and the ingest gate says so.
    """
    return {
        "Award ID": aid,
        "description": desc,
        "status": status,
        "savings": "",
        "claim_date": "",
        "action_date": action_date,
        "detection_basis": basis,
    }


# --- unresolved tracking ---------------------------------------------------


def test_resolvable_award_is_not_flagged():
    obj = make_search({"NASA Procurement Data View": [row("A-1")]}, [FakeAward("A-1")])
    assert obj.unresolved == {}
    assert "A-1" in obj.unique_cancellations


def test_idv_last_date_to_order_fills_a_missing_period_end():
    award = FakeAward(
        "80KSC020D0016",
        category="idv",
        end_date=None,
        last_date_to_order="2025-09-24",
    )
    obj = make_search(
        {"USAspending Terminations": [row("80KSC020D0016")]},
        [award],
    )

    assert obj.unique_cancellations["80KSC020D0016"]["Current End Date"] == "2025-09-24"


def test_idv_snake_case_last_date_to_order_is_also_supported():
    award = FakeAward("IDV-1", category="idv", end_date=None)
    award.raw = {"last_date_to_order": "2025-09-24"}
    obj = make_search({"USAspending Terminations": [row("IDV-1")]}, [award])

    assert obj.unique_cancellations["IDV-1"]["Current End Date"] == "2025-09-24"


def test_idv_period_end_remains_authoritative_when_present():
    award = FakeAward(
        "IDV-1",
        category="idv",
        end_date="2026-12-31",
        last_date_to_order="2025-09-24",
    )
    obj = make_search({"USAspending Terminations": [row("IDV-1")]}, [award])

    assert obj.unique_cancellations["IDV-1"]["Current End Date"] == "2026-12-31"


def test_non_idv_never_uses_last_date_to_order():
    award = FakeAward(
        "CONTRACT-1",
        category="contract",
        end_date=None,
        last_date_to_order="2025-09-24",
    )
    obj = make_search({"USAspending Terminations": [row("CONTRACT-1")]}, [award])

    assert obj.unique_cancellations["CONTRACT-1"]["Current End Date"] == ""


def test_search_publishes_the_canonical_nasa_assistance_url():
    award = FakeAward("80NSSC22M0122", category="grant")
    award.usa_spending_url = (
        "https://www.usaspending.gov/award/ASST_NON_80NSSC22M0122_8000/"
    )

    obj = make_search({"NASA Grants": [row("80NSSC22M0122")]}, [award])

    assert obj.unique_cancellations["80NSSC22M0122"]["USAspending URL"].endswith(
        "/ASST_NON_80NSSC22M0122_080/"
    )


def test_source_description_normalizes_carriage_returns():
    obj = make_search(
        {"DOGE": [row("A-1", "first line\r  second line  ")]},
        [FakeAward("A-1")],
    )

    assert obj.unique_cancellations["A-1"]["Award or Action Description"] == (
        "first line\n  second line  "
    )


# --- detection evidence ----------------------------------------------------


def test_detection_is_a_snapshot_column():
    """It has to be here or the source's evidence never leaves the query."""
    assert "Detection Evidence" in s.SNAPSHOT_COLUMNS


def test_one_full_history_supplies_snapshot_and_persisted_facts(workdir):
    class Txn:
        def __init__(self, when, mod, code, description):
            self.action_date = when
            self.modification_number = mod
            self.action_type = code
            self.action_type_description = description

    class Query:
        def __init__(self, rows):
            self.rows = rows
            self.all_calls = 0

        def order_by(self, field, direction):
            assert (field, direction) == ("action_date", "asc")
            return self

        def page_size(self, size):
            assert size == transaction_facts.PAGE_SIZE
            return self

        def limit(self, size):
            assert size > 10_000
            return self

        def all(self):
            self.all_calls += 1
            return list(self.rows)

    query = Query(
        [
            # Deliberately unordered: local ordering must define first/latest.
            Txn("2025-06-01", "P00004", "B", "CONTINUATION"),
            Txn("2024-01-01", "0", "A", "NEW"),
            Txn("2025-03-01", "P00003", "K", "CLOSE OUT"),
            Txn("2025-02-01", "P00002", "F", "TERMINATE FOR CONVENIENCE"),
        ]
    )
    award = FakeAward("A-1")
    award.generated_unique_award_id = "CONT_AWD_A-1"
    award.transactions = query

    obj = make_search({"NASA Procurement Data View": [row("A-1")]}, [award])
    obj.unique_award_ids = ["A-1"]
    obj._enrich_transaction_facts()
    record = obj.unique_cancellations["A-1"]
    persisted = transaction_facts.load_facts()["A-1"]

    assert query.all_calls == 1
    assert record["First Action Type"] == "A"
    assert record["First Action Type Description"] == "NEW"
    assert record["First Action Date"] == "2024-01-01"
    assert record["Latest Action Type"] == "B"
    assert record["Latest Action Type Description"] == "CONTINUATION"
    assert record["Latest Action Date"] == "2025-06-01"
    assert record["Latest Modification Number"] == "P00004"
    assert record["Action Code Termination Modification"] == "P00002"
    assert record["Action Code Termination Date"] == "2025-02-01"
    assert record["Closeout Modification Number"] == "P00003"
    assert record["Closeout Action Date"] == "2025-03-01"
    assert persisted["First Action Type"] == "A"
    assert persisted["Latest Modification Number"] == "P00004"
    assert persisted["Action Code Termination Modification"] == "P00002"
    assert persisted["Closeout Modification Number"] == "P00003"


def test_transaction_fields_are_blank_when_usaspending_returns_no_history():
    obj = make_search({"NASA Procurement Data View": [row("A-1")]}, [FakeAward("A-1")])
    record = obj.unique_cancellations["A-1"]

    for column in (
        "First Action Type",
        "First Action Type Description",
        "First Action Date",
        "Latest Action Type",
        "Latest Action Type Description",
        "Latest Action Date",
        "Action Code Termination Modification",
        "Action Code Termination Date",
        "Closeout Modification Number",
        "Closeout Action Date",
    ):
        assert record[column] == ""


def test_snapshot_carries_each_source_detection_string():
    """One real string per source, taken from the 2026-07-30 source CSVs.

    The winning source owns the cell, so a dashboard classifying detections has
    to cope with every shape at once.
    """
    detections = {
        "DOGE": "TERMINATED",
        "NASA Grants": "Administrative - Change Pop End Date",
        "USAspending Terminations": (
            "Terminate-for-convenience action P00180 on 2026-05-06"
        ),
        "Local USAspending Mirror": (
            "Terminate-for-convenience action P00002 on 2025-01-24; "
            "End date truncated 221 days by mod P00002 on 2025-01-24"
        ),
    }
    obj = make_search(
        {name: [row(f"{name}-1", status=text)] for name, text in detections.items()},
        [FakeAward(f"{name}-1") for name in detections],
    )

    for name, text in detections.items():
        assert obj.unique_cancellations[f"{name}-1"]["Detection Evidence"] == text


def test_source_without_detection_evidence_leaves_the_cell_empty():
    """NPDV infers nothing it can name: it matches on description text only."""
    obj = make_search({"NASA Procurement Data View": [row("A-1")]}, [FakeAward("A-1")])

    assert obj.unique_cancellations["A-1"]["Detection Evidence"] == ""


def test_an_all_blank_status_column_does_not_become_the_string_nan():
    """pandas hands back NaN for a column of Nones, and str(NaN) is 'nan'."""
    obj = make_search(
        {"NASA Procurement Data View": [row("A-1", status=None)]}, [FakeAward("A-1")]
    )

    assert obj.unique_cancellations["A-1"]["Detection Evidence"] == ""


def test_unresolvable_award_is_recorded_with_its_source():
    """The real case: a DOGE grant id in generated-id form matches nothing."""
    obj = make_search({"DOGE": [row("ASST_NON_80NSSC24K0913_8000")]}, [])
    assert obj.unresolved == {"ASST_NON_80NSSC24K0913_8000": ["DOGE"]}
    assert obj.unique_cancellations == {}


def test_ignored_awards_are_not_reported_as_unresolved():
    """They are excluded from the lookup on purpose; absence is expected."""
    obj = make_search(
        {"NASA Procurement Data View": [row("80LARC19F0086")]},
        [],
        ignore=["80LARC19F0086"],
    )
    assert obj.unresolved == {}


def test_blank_award_id_is_not_reported():
    obj = make_search({"NASA Procurement Data View": [row("")]}, [])
    assert obj.unresolved == {}


def test_same_award_unresolved_from_two_sources():
    obj = make_search(
        {"DOGE": [row("X-1")], "NASA Procurement Data View": [row("X-1")]}, []
    )
    assert sorted(obj.unresolved["X-1"]) == ["DOGE", "NASA Procurement Data View"]


# --- generated-id extraction ----------------------------------------------


def test_generated_id_yields_the_fain():
    """The real DOGE shape: 26 grants arrived like this and matched nothing."""
    from utils import award_id_from_generated_id as extract

    assert extract("ASST_NON_80NSSC24K0913_8000") == "80NSSC24K0913"
    assert extract("ASST_AGG_1234ABC_8000") == "1234ABC"
    assert extract("CONT_AWD_80MSFC22CA005_8000_-NONE-_-NONE-") == "80MSFC22CA005"


def test_legacy_nasa_assistance_generated_id_is_canonicalized():
    import utils

    assert (
        utils.canonical_generated_award_id("ASST_NON_80NSSC22M0122_8000")
        == "ASST_NON_80NSSC22M0122_080"
    )
    assert (
        utils.canonical_generated_award_id("ASST_AGG_NNX12AB34_8000")
        == "ASST_AGG_NNX12AB34_080"
    )
    assert (
        utils.canonical_generated_award_id(
            "CONT_AWD_80NSSC25FA315_8000_80NSSC24AA005_8000"
        )
        == "CONT_AWD_80NSSC25FA315_8000_80NSSC24AA005_8000"
    )
    assert (
        utils.canonical_generated_award_id("ASST_NON_OTHERAGENCY123_8000")
        == "ASST_NON_OTHERAGENCY123_8000"
    )


def test_plain_ids_pass_through_untouched():
    from utils import award_id_from_generated_id as extract

    assert extract("80MSFC22CA005") == "80MSFC22CA005"
    assert extract("NNG09FA40C") == "NNG09FA40C"
    assert extract("") == ""
    assert extract(None) == ""


def test_doge_grant_url_now_yields_a_fain():
    """End to end through the DOGE extractor, not just the util."""
    from doge_search import DOGEQuery

    q = DOGEQuery.__new__(DOGEQuery)
    q.verbose = False
    got = q._extract_usa_spending_award_id_from_grant_url(
        "https://usaspending.gov/award/ASST_NON_80NSSC24K0913_8000"
    )
    assert got == "80NSSC24K0913"


def test_award_recovered_by_generated_id_is_indexed_under_the_source_id():
    """award_identifier is read-only, so the alias lives in the index."""
    obj = s.Search.__new__(s.Search)
    obj.awards = []
    obj.awards_by_id = {}
    obj.unique_award_ids = ["ASST_NON_80NSSC24K0913_8000"]
    recovered = FakeAward("80NSSC24K0913")
    obj.client = type(
        "C",
        (),
        {
            "awards": type(
                "A", (), {"find_by_generated_id": staticmethod(lambda gid: recovered)}
            )()
        },
    )()
    obj._resolve_stragglers()
    assert obj.awards_by_id["ASST_NON_80NSSC24K0913_8000"] is recovered


def test_straggler_lookup_failure_does_not_abort_the_run():
    obj = s.Search.__new__(s.Search)
    obj.awards = []
    obj.awards_by_id = {}
    obj.unique_award_ids = ["ASST_NON_BAD_8000"]

    def boom(gid):
        raise RuntimeError("upstream down")

    obj.client = type(
        "C",
        (),
        {"awards": type("A", (), {"find_by_generated_id": staticmethod(boom)})()},
    )()
    obj._resolve_stragglers()  # must not raise
    assert obj.awards_by_id == {}


def test_award_enrichment_queries_idv_vehicles_alongside_contracts_and_grants():
    calls = []

    class Query:
        def award_ids(self, *award_ids):
            calls.append(("award_ids", award_ids))
            return self

        def contracts(self):
            self.category = "contract"
            return self

        def idvs(self):
            self.category = "idv"
            return self

        def grants(self):
            self.category = "grant"
            return self

        def all(self):
            calls.append(("all", self.category))
            return [self.category]

    obj = s.Search.__new__(s.Search)
    obj.unique_award_ids = ["80HQTR23AA002", "80NSSC25K0030"]
    obj.client = type(
        "C",
        (),
        {"awards": type("A", (), {"search": staticmethod(Query)})()},
    )()

    obj._fetch_awards()

    assert obj.awards == ["contract", "idv", "grant"]
    assert [call for call in calls if call[0] == "all"] == [
        ("all", "contract"),
        ("all", "idv"),
        ("all", "grant"),
    ]


# --- the printed report ----------------------------------------------------


@pytest.fixture
def ledger(workdir, write_csv):
    def _write(rows):
        write_csv(bml.LEDGER_PATH, bml.LEDGER_COLUMNS, rows)

    return _write


def test_report_names_every_unresolved_award(capsys, ledger):
    ledger([])
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {
        "ASST_NON_80NSSC24K0913_8000": ["DOGE"],
        "B-2": ["NASA Procurement Data View"],
    }
    obj._report_review_queue()
    out = capsys.readouterr().out
    assert "REVIEW QUEUE" in out
    assert "ASST_NON_80NSSC24K0913_8000" in out
    assert "B-2" in out
    # and explains the generated-id shape rather than leaving it a mystery
    assert "generated ids" in out
    assert "ASST_NON_<FAIN>_<code>" in out


def test_report_is_explicit_when_nothing_is_pending(capsys, ledger):
    ledger([])
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {}
    obj._report_review_queue()
    out = capsys.readouterr().out
    assert "All source-flagged award ids resolved" in out
    assert "No ledger awards awaiting review" in out


def test_report_lists_unexplained_ledger_statuses(capsys, ledger):
    def rec(aid, status, auto=""):
        r = {c: "" for c in bml.LEDGER_COLUMNS}
        r.update(
            {
                "Award ID": aid,
                "Tracking Status": status,
                "Last Flagged Date": "2026-07-30",
                "Automated Verdict": auto,
            }
        )
        return r

    ledger(
        [
            rec("P-1", "unflagged_pending_review"),
            rec("P-2", "needs_manual_review"),
            rec("OK-1", "currently_flagged"),
            rec("OK-2", "excluded_by_design"),
        ]
    )
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {}
    obj._report_review_queue()
    out = capsys.readouterr().out
    assert "P-1" in out and "P-2" in out
    assert "OK-1" not in out and "OK-2" not in out


def test_report_surfaces_machine_disagreement(capsys, ledger):
    def rec(aid, status, auto):
        r = {c: "" for c in bml.LEDGER_COLUMNS}
        r.update(
            {
                "Award ID": aid,
                "Tracking Status": status,
                "Last Flagged Date": "2026-07-30",
                "Automated Verdict": auto,
            }
        )
        return r

    ledger(
        [
            rec("D-1", "source_retired", "continued"),
            rec(
                "L-1", "currently_flagged", "continued"
            ),  # currently flagged: not a disagreement
            rec("S-1", "still_terminated", "still_terminated"),  # agrees
        ]
    )
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {}
    obj._report_review_queue()
    out = capsys.readouterr().out
    assert "D-1" in out
    assert "never auto-applied" in out
    assert "L-1" not in out and "S-1" not in out


def test_report_tolerates_a_missing_ledger(capsys, workdir):
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {}
    obj._report_review_queue()
    assert "REVIEW QUEUE" in capsys.readouterr().out
