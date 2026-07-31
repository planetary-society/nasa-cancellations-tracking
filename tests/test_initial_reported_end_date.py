"""Transaction-derived Initial Reported End Date enrichment and persistence."""

import csv
import os
from pathlib import Path

import pytest

import build_master_ledger as bml
import search as search_module
import usaspending_terminations_query as utq


def target(aid="A-1", category="contract", gid=None, lookup=""):
    prefixes = {
        "contract": "CONT_AWD_",
        "idv": "CONT_IDV_",
        "assistance": "ASST_NON_",
    }
    return utq.InitialEndDateTarget(
        aid,
        gid or f"{prefixes[category]}{aid}_8000",
        category,
        lookup,
    )


def txn(
    transaction_id,
    action_date,
    modification_number,
    end_date="",
):
    return {
        "transaction_id": transaction_id,
        "action_date": action_date,
        "modification_number": modification_number,
        "end_date": end_date,
    }


# --- pure selection -------------------------------------------------------


def test_zero_base_modification_wins_over_an_earlier_nonbase_row():
    result = utq.select_initial_reported_end_date(
        target(),
        [
            txn("T-P1", "2020-01-01", "P00001", "2028-01-01"),
            txn("T-0", "2020-01-02", "0", "2027-01-01"),
        ],
    )

    assert result.initial_end_date == "2027-01-01"
    assert result.transaction_id == "T-0"
    assert result.basis == "base_transaction"
    assert result.status == "resolved"


def test_same_day_modifications_use_natural_order_and_stable_transaction_tie_break():
    result = utq.select_initial_reported_end_date(
        target(),
        [
            txn("T-10", "2020-01-01", "P10", "2030-01-01"),
            txn("T-2B", "2020-01-01", "P2", "2022-02-02"),
            txn("T-2A", "2020-01-01", "P2", "2021-01-01"),
        ],
    )

    assert result.initial_end_date == "2021-01-01"
    assert result.transaction_id == "T-2A"
    assert result.basis == "earliest_nonblank"


def test_blank_base_date_falls_forward_to_earliest_nonblank_transaction():
    result = utq.select_initial_reported_end_date(
        target(),
        [
            txn("T-0", "2020-01-01", "0000"),
            txn("T-1", "2020-03-01", "P00001", "2029-09-30"),
        ],
    )

    assert result.initial_end_date == "2029-09-30"
    assert result.transaction_id == "T-1"
    assert result.basis == "earliest_nonblank"


def test_no_reported_date_is_a_terminal_blank_result():
    result = utq.select_initial_reported_end_date(
        target(), [txn("T-0", "2020-01-01", "0")]
    )

    assert result.initial_end_date == ""
    assert result.status == "no_reported_end_date"


@pytest.mark.parametrize("field", ["action_date", "end_date"])
def test_malformed_dates_fail_loudly(field):
    row = txn("T-0", "2020-01-01", "0", "2025-01-01")
    row[field] = "not-a-date"

    with pytest.raises(RuntimeError, match=field):
        utq.select_initial_reported_end_date(target(), [row])


# --- download parsing and batching ---------------------------------------


class FakeQuery:
    def __init__(self):
        self.category = ""
        self.ids = ()
        self.period = ()

    def contracts(self):
        self.category = "contract"
        return self

    def idvs(self):
        self.category = "idv"
        return self

    def grants(self):
        self.category = "assistance"
        return self

    def award_ids(self, *ids):
        self.ids = ids
        return self

    def time_period(self, start, end):
        self.period = (start, end)
        return self


class FakeJob:
    def __init__(self, path):
        self.path = path
        self.wait_args = None

    def wait_for_completion(self, **kwargs):
        self.wait_args = kwargs
        return [self.path]


class FakeDownloads:
    def __init__(self):
        self.calls = []
        self.destinations = []

    def search(self, query, *, spending_level, destination_dir):
        self.calls.append((query, spending_level))
        self.destinations.append(destination_dir)
        category = query.category
        aid = query.ids[0].strip('"')
        if category == "assistance":
            id_header = "award_id_fain"
            transaction_header = "assistance_transaction_unique_key"
        elif category == "idv":
            id_header = "award_id_piid"
            transaction_header = "idv_transaction_unique_key"
        else:
            id_header = "award_id_piid"
            transaction_header = "contract_transaction_unique_key"

        path = os.path.join(destination_dir, f"{category}_transactions.csv")
        fieldnames = [
            id_header,
            transaction_header,
            "action_date",
            "modification_number",
            "period_of_performance_current_end_date",
            "last_date_to_order",
        ]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(
                {
                    id_header: aid,
                    transaction_header: f"TX-{category}",
                    "action_date": "2020-01-01",
                    "modification_number": "0",
                    "period_of_performance_current_end_date": "2028-01-01",
                    "last_date_to_order": "2027-01-01" if category == "idv" else "",
                }
            )
        return FakeJob(path)


def test_downloads_are_batched_by_category_and_idv_prefers_last_date_to_order():
    downloads = FakeDownloads()
    client = type(
        "Client",
        (),
        {
            "awards": type("Awards", (), {"search": staticmethod(FakeQuery)})(),
            "downloads": downloads,
        },
    )()
    targets = [
        target("C-1", "contract"),
        target("I-1", "idv"),
        target("G-1", "assistance"),
    ]

    results = utq.fetch_initial_reported_end_dates(
        client, targets, end_date="2026-07-31"
    )

    assert [result.category for result in results] == [
        "contract",
        "idv",
        "assistance",
    ]
    assert {result.award_id: result.initial_end_date for result in results} == {
        "C-1": "2028-01-01",
        "I-1": "2027-01-01",
        "G-1": "2028-01-01",
    }
    assert len(downloads.calls) == 3
    for query, spending_level in downloads.calls:
        assert query.ids[0] in {"C-1", "I-1", "G-1"}
        assert query.period == (utq.INITIAL_END_DATE_START, "2026-07-31")
        assert spending_level == ["transactions"]
    # TemporaryDirectory owns every extracted artifact.
    assert all(not os.path.exists(path) for path in downloads.destinations)


def test_download_without_required_transaction_columns_fails_loudly(tmp_path):
    path = tmp_path / "not_transactions.csv"
    path.write_text("Award ID,Action Date\nA-1,2020-01-01\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no transaction CSV"):
        utq._transaction_download_rows([str(path)], "contract", {"A-1"})


# --- durable cache and search enrichment ---------------------------------


class FakeAward:
    award_identifier = "NEW-1"
    generated_unique_award_id = "CONT_AWD_NEW-1_8000_-NONE-_-NONE-"


def test_search_backfills_ledger_and_current_awards_atomically(
    workdir, write_csv, monkeypatch
):
    write_csv(
        bml.LEDGER_PATH,
        ["Award ID", "URL"],
        [
            {
                "Award ID": "OLD-1",
                "URL": "https://www.usaspending.gov/award/ASST_NON_OLD-1_080/",
            }
        ],
    )
    seen = []

    def fake_fetch(client, targets, *, end_date):
        seen.extend(targets)
        return [
            utq.InitialEndDateResult(
                item.award_id,
                item.generated_award_id,
                item.category,
                "2024-12-31",
                f"TX-{item.award_id}",
                "2020-01-01",
                "0",
                "base_transaction",
                "resolved",
            )
            for item in targets
        ]

    monkeypatch.setattr(search_module, "fetch_initial_reported_end_dates", fake_fetch)
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}

    obj._enrich_initial_reported_end_dates()

    assert {item.award_id for item in seen} == {"OLD-1", "NEW-1"}
    assert obj.initial_end_dates_changed
    stored = bml.load_initial_end_dates()
    assert set(stored) == {"OLD-1", "NEW-1"}
    assert stored["OLD-1"]["Award Category"] == "assistance"
    assert stored["NEW-1"]["Initial Reported End Date"] == "2024-12-31"


def test_cached_terminal_result_prevents_repeat_download(workdir, write_csv, monkeypatch):
    row = {column: "" for column in bml.INITIAL_END_DATE_COLUMNS}
    row.update(
        {
            "Award ID": "NEW-1",
            "Generated Award ID": FakeAward.generated_unique_award_id,
            "Award Category": "contract",
            "Initial Reported End Date": "2024-12-31",
            "Source Transaction ID": "TX-1",
            "Source Action Date": "2020-01-01",
            "Source Modification Number": "0",
            "Source Basis": "base_transaction",
            "Lookup Status": "resolved",
            "Last Checked Date": "2026-07-31",
        }
    )
    write_csv(
        bml.INITIAL_END_DATES_PATH,
        bml.INITIAL_END_DATE_COLUMNS,
        [row],
    )
    monkeypatch.setattr(
        search_module,
        "fetch_initial_reported_end_dates",
        lambda *args, **kwargs: pytest.fail("cached award was downloaded again"),
    )
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}

    obj._enrich_initial_reported_end_dates()

    assert not obj.initial_end_dates_changed
    assert obj.initial_end_date_rows["NEW-1"]["Lookup Status"] == "resolved"


def test_enrichment_only_change_rebuilds_unchanged_snapshot_ledger(
    workdir, write_csv, monkeypatch
):
    prior_row = {column: "" for column in search_module.SNAPSHOT_COLUMNS}
    prior_row.update(
        {
            "Source": "DOGE",
            "Award ID": "NEW-1",
            "Recipient": "Recipient",
            "Latest Modification Date": "2026-01-01",
            "Initial Reported End Date": "2024-12-31",
        }
    )
    write_csv(
        "consolidated/nasa_contract_cancellations_2026-07-30.csv",
        search_module.SNAPSHOT_COLUMNS,
        [prior_row],
    )

    class Source:
        def search(self):
            return __import__("pandas").DataFrame([{"Award ID": "NEW-1"}])

    obj = search_module.Search.__new__(search_module.Search)
    obj.sources = {"DOGE": Source}
    obj.sources_cancellation_data = {}
    obj.unique_award_ids = []
    obj.unique_cancellations = {}
    obj.awards = []
    obj.awards_by_id = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = []
    obj.initial_end_date_rows = {}
    obj.initial_end_dates_changed = False

    monkeypatch.setattr(obj, "_build_claim_index", lambda: None)
    monkeypatch.setattr(obj, "_fetch_awards", lambda: None)
    monkeypatch.setattr(obj, "_resolve_stragglers", lambda: None)

    def enrich():
        obj.initial_end_dates_changed = True
        obj.initial_end_date_rows = {
            "NEW-1": {"Initial Reported End Date": "2024-12-31"}
        }

    monkeypatch.setattr(obj, "_enrich_initial_reported_end_dates", enrich)
    monkeypatch.setattr(
        obj,
        "_add_source_awards",
        lambda source, award_ids: obj.unique_cancellations.update(
            {"NEW-1": prior_row}
        ),
    )
    monkeypatch.setattr(obj, "_report_review_queue", lambda: None)
    builds = []
    monkeypatch.setattr(search_module.build_master_ledger, "build", lambda: builds.append(1))

    obj._search()

    assert builds == [1]


def test_winning_source_cannot_suppress_initial_end_date():
    obj = search_module.Search.__new__(search_module.Search)
    obj.sources_cancellation_data = {
        "DOGE": __import__("pandas").DataFrame(
            [
                {
                    "Award ID": "NEW-1",
                    "description": "claimed termination",
                    "status": "TERMINATED",
                }
            ]
        )
    }
    obj.unique_cancellations = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = []
    award = type(
        "Award",
        (),
        {
            "award_identifier": "NEW-1",
            "category": "contract",
            "raw": {},
            "usa_spending_url": "https://www.usaspending.gov/award/CONT_AWD_NEW-1/",
            "award_amount": 1,
            "total_outlay": 0,
            "description": "description",
            "transactions": [],
            "period_of_performance": type(
                "Period",
                (),
                {
                    "last_modified_date": "2026-01-01",
                    "start_date": "2020-01-01",
                    "end_date": "2026-12-31",
                },
            )(),
            "recipient": type(
                "Recipient",
                (),
                {
                    "name": "Recipient",
                    "business_types": [],
                    "location": type("Location", (), {"district": "CA-01"})(),
                },
            )(),
        },
    )()
    obj.awards_by_id = {"NEW-1": award}
    obj.initial_end_date_rows = {
        "NEW-1": {"Initial Reported End Date": "2024-12-31"}
    }

    obj._add_source_awards("DOGE", ["NEW-1"])

    assert (
        obj.unique_cancellations["NEW-1"]["Initial Reported End Date"]
        == "2024-12-31"
    )


# --- ledger durability and trend baseline --------------------------------


SNAPSHOT_COLUMNS = [
    "Source",
    "Award ID",
    "Recipient",
    "End Date",
    "Initial Reported End Date",
    "Description",
    "URL",
]


def snapshot_row(aid, end="2026-12-31", initial=""):
    return {
        "Source": "NPDV",
        "Award ID": aid,
        "Recipient": f"R {aid}",
        "End Date": end,
        "Initial Reported End Date": initial,
        "Description": "termination",
        "URL": f"https://www.usaspending.gov/award/CONT_AWD_{aid}/",
    }


def sidecar_row(aid, initial):
    row = {column: "" for column in bml.INITIAL_END_DATE_COLUMNS}
    row.update(
        {
            "Award ID": aid,
            "Generated Award ID": f"CONT_AWD_{aid}",
            "Award Category": "contract",
            "Initial Reported End Date": initial,
            "Source Transaction ID": f"TX-{aid}",
            "Source Action Date": "2020-01-01",
            "Source Modification Number": "0",
            "Source Basis": "base_transaction",
            "Lookup Status": "resolved",
            "Last Checked Date": "2026-07-31",
        }
    )
    return row


def read_ledger():
    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        return {row["Award ID"]: row for row in csv.DictReader(fh)}


def test_full_build_backfills_sidecar_and_prefers_it_for_trend(workdir, write_csv):
    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        SNAPSHOT_COLUMNS,
        [snapshot_row("A-1", end="2026-12-31")],
    )
    write_csv(
        bml.INITIAL_END_DATES_PATH,
        bml.INITIAL_END_DATE_COLUMNS,
        [sidecar_row("A-1", "2028-12-31")],
    )

    bml.build()

    row = read_ledger()["A-1"]
    assert row["Initial Reported End Date"] == "2028-12-31"
    assert row["First End Date"] == "2026-12-31"
    assert row["End Date Trend"] == "truncated"


def test_incremental_build_does_not_overwrite_recorded_initial_date(
    workdir, write_csv
):
    write_csv(
        "consolidated/nasa_x_2026-07-30.csv",
        SNAPSHOT_COLUMNS,
        [snapshot_row("A-1", initial="2028-12-31")],
    )
    bml.build()
    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        SNAPSHOT_COLUMNS,
        [snapshot_row("A-1", initial="2029-12-31")],
    )
    write_csv(
        bml.INITIAL_END_DATES_PATH,
        bml.INITIAL_END_DATE_COLUMNS,
        [sidecar_row("A-1", "2029-12-31")],
    )

    bml.build(update_only=True)

    assert read_ledger()["A-1"]["Initial Reported End Date"] == "2028-12-31"


def test_legacy_first_end_date_remains_the_fallback():
    rec = {
        "First Award Amount": "",
        "Transaction Baseline Amount": "",
        "Award Amount": "",
        "Initial Reported End Date": "",
        "First End Date": "2024-01-01",
        "End Date": "2025-01-01",
        "Claiming Source": "",
    }

    bml.derive_trends(rec)

    assert rec["End Date Trend"] == "extended"
