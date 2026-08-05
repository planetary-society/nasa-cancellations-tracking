"""Transaction-history provenance survives the snapshot-to-ledger boundary."""

import csv
import glob

import pytest

import award_transaction_facts as atf
import build_master_ledger as bml
import search
from tests.helpers import FakeTxn

TRANSACTION_COLUMNS = bml.TRANSACTION_HISTORY_COLUMNS


def row(aid, source="NPDV", **extra):
    record = {column: "" for column in search.SNAPSHOT_COLUMNS}
    record.update(
        {
            "Source": source,
            "Award ID": aid,
            "Recipient Name": f"Recipient {aid}",
            "Award or Action Description": "terminate for convenience",
        }
    )
    record.update(extra)
    return record


def ledger():
    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        return {record["Award ID"]: record for record in csv.DictReader(fh)}


def test_transaction_history_fields_are_refreshed_ledger_columns():
    for column in TRANSACTION_COLUMNS:
        assert column in search.SNAPSHOT_COLUMNS
        assert column in bml.LEDGER_COLUMNS
        assert column in bml.REFRESHED_COLUMNS


def test_full_build_carries_transaction_history_facts(workdir, write_csv):
    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        search.SNAPSHOT_COLUMNS,
        [
            row("KEEP-1"),
            row(
                "A-1",
                source="USAspendingTerminations",
                **{
                    "First Action Type": "A",
                    "First Action Type Description": "NEW",
                    "First Action Date": "2024-01-01",
                    "Latest Action Type": "B",
                    "Latest Action Type Description": "CONTINUATION",
                    "Latest Action Date": "2025-06-01",
                    "Termination Modification Number": "P00002",
                    "Termination Action Date": "2025-02-01",
                    "Closeout Modification Number": "P00003",
                    "Closeout Action Date": "2025-03-01",
                },
            ),
        ],
    )

    bml.build()

    record = ledger()["A-1"]
    assert record["First Action Type"] == "A"
    assert record["First Action Type Description"] == "NEW"
    assert record["First Action Date"] == "2024-01-01"
    assert record["Latest Action Type"] == "B"
    assert record["Latest Action Type Description"] == "CONTINUATION"
    assert record["Latest Action Date"] == "2025-06-01"
    assert record["Termination Modification Number"] == "P00002"
    assert record["Termination Action Date"] == "2025-02-01"
    assert record["Closeout Modification Number"] == "P00003"
    assert record["Closeout Action Date"] == "2025-03-01"


def test_legacy_snapshot_builds_blank_transaction_fields(workdir, write_csv):
    legacy_columns = [
        column
        for column in search.SNAPSHOT_COLUMNS
        if column not in TRANSACTION_COLUMNS
    ]
    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        legacy_columns,
        [row("KEEP-1"), row("A-1", source="USAspendingTerminations")],
    )

    bml.build()

    record = ledger()["A-1"]
    assert all(record[column] == "" for column in TRANSACTION_COLUMNS)


def test_blank_later_snapshot_does_not_erase_formal_modifications(workdir, write_csv):
    first = row(
        "A-1",
        source="USAspendingTerminations",
        **{
            "Termination Modification Number": "P00002",
            "Termination Action Date": "2025-02-01",
            "Closeout Modification Number": "P00003",
            "Closeout Action Date": "2025-03-01",
        },
    )
    write_csv(
        "consolidated/nasa_x_2026-07-30.csv",
        search.SNAPSHOT_COLUMNS,
        [row("KEEP-1"), first],
    )
    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        search.SNAPSHOT_COLUMNS,
        [row("KEEP-1"), row("A-1")],
    )

    bml.build()

    record = ledger()["A-1"]
    assert record["Termination Modification Number"] == "P00002"
    assert record["Termination Action Date"] == "2025-02-01"
    assert record["Closeout Modification Number"] == "P00003"
    assert record["Closeout Action Date"] == "2025-03-01"


def fact_row(aid="A-1", *, checked="2026-07-31"):
    return atf.build_fact_row(
        aid,
        f"CONT_AWD_{aid}",
        "contract",
        [
            FakeTxn(
                "2024-01-01",
                "0",
                "A",
                action_type_description="NEW",
            ),
            FakeTxn(
                "2025-02-01",
                "P00002",
                "F",
                action_type_description="TERMINATE FOR CONVENIENCE",
            ),
            FakeTxn(
                "2025-03-01",
                "P00003",
                "K",
                action_type_description="CLOSE OUT",
            ),
            FakeTxn(
                "2025-06-01",
                "P00004",
                "B",
                action_type_description="CONTINUATION",
            ),
        ],
        checked=checked,
    )


def test_transaction_fact_sidecar_round_trips_and_validates_dates(workdir):
    atf.write_facts({"A-1": fact_row()})

    stored = atf.load_facts()

    assert stored["A-1"]["Transaction Count"] == "4"
    assert stored["A-1"]["Latest Modification Number"] == "P00004"
    assert stored["A-1"]["Termination Action Date"] == "2025-02-01"
    assert stored["A-1"]["Closeout Action Date"] == "2025-03-01"

    stored["A-1"]["Latest Action Date"] = "not-a-date"
    atf.write_facts(stored)
    with pytest.raises(RuntimeError, match="invalid Latest Action Date"):
        atf.load_facts()


def test_empty_history_is_not_persistable():
    with pytest.raises(ValueError, match="empty transaction history"):
        atf.build_fact_row("A-1", "CONT_AWD_A-1", "contract", [], checked="2026-07-31")


def test_failed_refresh_retains_prior_persisted_facts(workdir):
    original = fact_row()
    atf.write_facts({"A-1": original})
    award = type(
        "Award",
        (),
        {
            "award_identifier": "A-1",
            "generated_unique_award_id": "CONT_AWD_A-1",
            "category": "contract",
            "transactions": [],
        },
    )()
    obj = search.Search.__new__(search.Search)
    obj.unique_award_ids = ["A-1"]
    obj.awards_by_id = {"A-1": award}
    obj.transaction_histories = {}

    obj._enrich_transaction_facts()

    assert atf.load_facts()["A-1"] == original
    assert not obj.transaction_facts_changed


def test_sidecar_overlays_accepted_ledger_without_accepting_quarantine(
    workdir, write_csv
):
    legacy_columns = [
        column
        for column in search.SNAPSHOT_COLUMNS
        if column not in TRANSACTION_COLUMNS
    ]
    write_csv(
        "consolidated/nasa_x_2026-07-30.csv",
        legacy_columns,
        [row("A-1")],
    )
    write_csv(
        "consolidated/quarantine/nasa_x_2026-07-31.csv",
        search.SNAPSHOT_COLUMNS,
        [row("QUARANTINED-ONLY")],
    )
    atf.write_facts({"A-1": fact_row()})

    bml.build()

    records = ledger()
    assert set(records) == {"A-1"}
    assert records["A-1"]["Latest Modification Number"] == "P00004"
    assert records["A-1"]["First Action Type"] == "A"
    assert records["A-1"]["Latest Action Date"] == "2025-06-01"
    assert records["A-1"]["Termination Modification Number"] == "P00002"
    assert records["A-1"]["Closeout Modification Number"] == "P00003"


def test_search_backfills_a_ledger_only_award_once(workdir, write_csv):
    write_csv(
        bml.LEDGER_PATH,
        bml.LEDGER_COLUMNS,
        [
            {
                "Award ID": "A-1",
                "Recipient Name": "Historical recipient",
                "USAspending URL": "https://www.usaspending.gov/award/CONT_AWD_A-1/",
            }
        ],
    )

    class Query:
        def __init__(self):
            self.calls = 0

        def award_id(self, generated_id):
            assert generated_id == "CONT_AWD_A-1"
            self.calls += 1
            return self

        def order_by(self, field, direction):
            assert (field, direction) == ("action_date", "asc")
            return self

        def page_size(self, size):
            assert size == atf.PAGE_SIZE
            return self

        def limit(self, size):
            assert size > 10_000
            return self

        def all(self):
            return [FakeTxn("2024-01-01", "0", "A")]

    def make_search(query):
        obj = search.Search.__new__(search.Search)
        obj.unique_award_ids = []
        obj.awards_by_id = {}
        obj.transaction_histories = {}
        obj.client = type("Client", (), {"transactions": query})()
        return obj

    first_query = Query()
    first = make_search(first_query)
    first._enrich_transaction_facts()

    assert first_query.calls == 1
    assert atf.load_facts()["A-1"]["First Action Date"] == "2024-01-01"

    second_query = Query()
    second = make_search(second_query)
    second._enrich_transaction_facts()

    assert second_query.calls == 0
    assert not second.transaction_facts_changed


def test_quarantined_search_rebuilds_ledger_when_transaction_facts_changed(
    workdir, write_csv, monkeypatch
):
    previous = [row(f"OLD-{i}") for i in range(5)]
    write_csv(
        "consolidated/nasa_contract_cancellations_2026-07-30.csv",
        search.SNAPSHOT_COLUMNS,
        previous,
    )
    candidate = row("OLD-0")

    obj = search.Search.__new__(search.Search)
    obj.sources_cancellation_data = {
        "DOGE": __import__("pandas").DataFrame([{"Award ID": "OLD-0"}])
    }
    obj.unique_award_ids = ["OLD-0"]
    obj.unique_cancellations = {}
    obj.awards = []
    obj.awards_by_id = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = []
    obj.skipped_sources = set()
    obj.window_rejects = []
    obj.initial_end_dates_changed = False
    obj.transaction_facts_changed = False

    monkeypatch.setattr(obj, "_collect_source_data", lambda: None)
    monkeypatch.setattr(obj, "_build_claim_index", lambda: None)
    monkeypatch.setattr(obj, "_fetch_awards", lambda: None)
    monkeypatch.setattr(obj, "_resolve_stragglers", lambda: None)
    monkeypatch.setattr(obj, "_enrich_initial_reported_end_dates", lambda: None)
    monkeypatch.setattr(
        obj,
        "_add_source_awards",
        lambda source, award_ids: obj.unique_cancellations.update({"OLD-0": candidate}),
    )

    def enrich_transaction_facts():
        obj.transaction_facts_changed = True

    monkeypatch.setattr(obj, "_enrich_transaction_facts", enrich_transaction_facts)
    builds = []
    monkeypatch.setattr(bml, "build", lambda: builds.append(1))

    with pytest.raises(SystemExit):
        obj._search()

    assert builds == [1]
    assert glob.glob("consolidated/quarantine/*.csv")
    assert not glob.glob("consolidated/nasa_contract_cancellations_2026-07-31.csv")
