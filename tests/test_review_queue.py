"""The review queue search.py prints at the end of a run.

An award a source flagged but USAspending could not resolve never reaches the
snapshot and leaves no trace in it. Before this report, 26 DOGE-claimed grants
were dropped on every run with no output at all.
"""

import pandas as pd
import pytest

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

    def __init__(self, award_id):
        self.award_identifier = award_id
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
                "last_modified_date": "2026-01-01",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
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
    obj.awards = awards
    obj.awards_by_id = {a.award_identifier: a for a in awards}
    for name, rows in source_rows.items():
        obj._add_source_awards(name, [r["Award ID"] for r in rows])
    return obj


def row(aid, desc="terminated"):
    return {
        "Award ID": aid,
        "description": desc,
        "status": "",
        "savings": "",
        "claim_date": "",
    }


# --- unresolved tracking ---------------------------------------------------


def test_resolvable_award_is_not_flagged():
    obj = make_search({"NPDV": [row("A-1")]}, [FakeAward("A-1")])
    assert obj.unresolved == {}
    assert "A-1" in obj.unique_cancellations


def test_unresolvable_award_is_recorded_with_its_source():
    """The real case: a DOGE grant id in generated-id form matches nothing."""
    obj = make_search({"DOGE": [row("ASST_NON_80NSSC24K0913_8000")]}, [])
    assert obj.unresolved == {"ASST_NON_80NSSC24K0913_8000": ["DOGE"]}
    assert obj.unique_cancellations == {}


def test_ignored_awards_are_not_reported_as_unresolved():
    """They are excluded from the lookup on purpose; absence is expected."""
    obj = make_search({"NPDV": [row("80LARC19F0086")]}, [], ignore=["80LARC19F0086"])
    assert obj.unresolved == {}


def test_blank_award_id_is_not_reported():
    obj = make_search({"NPDV": [row("")]}, [])
    assert obj.unresolved == {}


def test_same_award_unresolved_from_two_sources():
    obj = make_search({"DOGE": [row("X-1")], "NPDV": [row("X-1")]}, [])
    assert sorted(obj.unresolved["X-1"]) == ["DOGE", "NPDV"]


# --- generated-id extraction ----------------------------------------------


def test_generated_id_yields_the_fain():
    """The real DOGE shape: 26 grants arrived like this and matched nothing."""
    from utils import award_id_from_generated_id as extract

    assert extract("ASST_NON_80NSSC24K0913_8000") == "80NSSC24K0913"
    assert extract("ASST_AGG_1234ABC_8000") == "1234ABC"
    assert extract("CONT_AWD_80MSFC22CA005_8000_-NONE-_-NONE-") == "80MSFC22CA005"


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


# --- the printed report ----------------------------------------------------


@pytest.fixture
def ledger(workdir, write_csv):
    def _write(rows):
        write_csv(bml.LEDGER_PATH, bml.LEDGER_COLUMNS, rows)

    return _write


def test_report_names_every_unresolved_award(capsys, ledger):
    ledger([])
    obj = s.Search.__new__(s.Search)
    obj.unresolved = {"ASST_NON_80NSSC24K0913_8000": ["DOGE"], "B-2": ["NPDV"]}
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
                "Status": status,
                "Last Seen": "2026-07-30",
                "Auto Status": auto,
            }
        )
        return r

    ledger(
        [
            rec("P-1", "dropped_pending_review"),
            rec("P-2", "needs_manual_review"),
            rec("OK-1", "listed"),
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
                "Status": status,
                "Last Seen": "2026-07-30",
                "Auto Status": auto,
            }
        )
        return r

    ledger(
        [
            rec("D-1", "source_retired", "continued"),
            rec("L-1", "listed", "continued"),  # listed: not a disagreement
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
