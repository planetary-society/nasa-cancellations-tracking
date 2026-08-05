"""Transaction-derived Initial Reported End Date enrichment and persistence.

The provider is the local mirror. The public API publishes no
period-of-performance date on any transaction route, and the bulk-download
route that did carry it was removed as far too expensive for one column -
see initial_end_dates. Without mirror credentials the enrichment resolves
nothing and leaves the committed sidecar untouched.
"""

import csv

import pytest

import build_master_ledger as bml
import initial_end_dates as ied
import local_usaspending_mirror_query as mirror
import search as search_module


def target(aid="A-1", category="contract", gid=None):
    prefixes = {
        "contract": "CONT_AWD_",
        "idv": "CONT_IDV_",
        "assistance": "ASST_NON_",
    }
    return ied.InitialEndDateTarget(
        aid,
        gid or f"{prefixes[category]}{aid}_8000",
        category,
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
    result = ied.select_initial_reported_end_date(
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
    result = ied.select_initial_reported_end_date(
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
    result = ied.select_initial_reported_end_date(
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
    result = ied.select_initial_reported_end_date(
        target(), [txn("T-0", "2020-01-01", "0")]
    )

    assert result.initial_end_date == ""
    assert result.status == "no_reported_end_date"


@pytest.mark.parametrize("field", ["action_date", "end_date"])
def test_malformed_dates_fail_loudly(field):
    row = txn("T-0", "2020-01-01", "0", "2025-01-01")
    row[field] = "not-a-date"

    with pytest.raises(RuntimeError, match=field):
        ied.select_initial_reported_end_date(target(), [row])


# --- the status vocabulary -------------------------------------------------


def test_every_status_is_classified_terminal_or_transient():
    """The persist/retry decision must be forced, not defaulted. A status that
    is in neither set would fall through search.py's filter and be written to a
    write-once file; one in both would be ambiguous."""
    assert not (ied.TERMINAL_STATUSES & ied.TRANSIENT_STATUSES)
    assert ied.INITIAL_END_DATE_STATUSES == (
        ied.TERMINAL_STATUSES | ied.TRANSIENT_STATUSES
    )


def test_the_ledger_validator_accepts_exactly_the_emittable_statuses():
    """build_master_ledger validates the persisted vocabulary. It used to hold
    its own copy, which could drift from what the producers emit - a new status
    would abort the NEXT build, after the sidecar had already been written."""
    assert bml.INITIAL_END_DATE_STATUSES is ied.INITIAL_END_DATE_STATUSES


def test_every_status_any_producer_emits_is_in_the_vocabulary():
    """Greps the producers rather than trusting the constant: the point is to
    catch a literal added at a call site without updating the sets."""
    import pathlib
    import re

    emitted = set()
    for name in (
        "initial_end_dates.py",
        "local_usaspending_mirror_query.py",
        "search.py",
    ):
        text = pathlib.Path(name).read_text()
        emitted |= set(re.findall(r'unresolved\(\s*target,\s*"([a-z_]+)"', text))
        emitted |= set(re.findall(r'"(resolved|no_reported_end_date)"', text))
    assert emitted, "found no status literals - has the emit pattern changed?"
    assert emitted <= ied.INITIAL_END_DATE_STATUSES, (
        f"unclassified status(es): {sorted(emitted - ied.INITIAL_END_DATE_STATUSES)}"
    )


# --- the mirror provider ---------------------------------------------------


class FakeCursor:
    """Stands in for a psycopg dict_row cursor over rpt.transaction_search."""

    def __init__(self, rows, execute_error=None):
        self.rows = rows
        self.execute_error = execute_error
        self.executed = []
        self._result = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().startswith("SET"):
            self._result = []
            return
        if self.execute_error:
            raise self.execute_error
        wanted = set(params["generated_award_ids"])
        self._result = [
            r for r in self.rows if r["generated_unique_award_id"] in wanted
        ]

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """Context-manager protocol lives on the type, not on instance attributes."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mirror_with(cursor, monkeypatch, configured=True, connect_error=None):
    """A mirror query whose psycopg connection yields `cursor`.

    Offline by construction, matching tests/test_local_mirror.py: the driver is
    stubbed into sys.modules, so no socket is ever opened.
    """
    import sys as _sys
    import types

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.OperationalError = type("OperationalError", (Exception,), {})

    def connect(*args, **kwargs):
        if connect_error:
            raise fake_psycopg.OperationalError(connect_error)
        return FakeConnection(cursor)

    fake_psycopg.connect = connect
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    fake_psycopg.rows = rows_mod
    monkeypatch.setitem(_sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(_sys.modules, "psycopg.rows", rows_mod)

    q = mirror.LocalUSASpendingMirrorQuery.__new__(mirror.LocalUSASpendingMirrorQuery)
    monkeypatch.setattr(q, "is_configured", lambda: configured)
    monkeypatch.setattr(q, "_dsn", lambda: "postgresql://x/y")
    return q


def _row(gid, txn_id, action_date, mod, end_date):
    return {
        "generated_unique_award_id": gid,
        "transaction_id": txn_id,
        "action_date": action_date,
        "modification_number": mod,
        "end_date": end_date,
    }


def test_mirror_resolves_the_base_transaction_end_date(monkeypatch):
    cur = FakeCursor(
        [
            _row("CONT_AWD_A-1_8000", "TX-1", "2017-07-01", "0", "2022-12-31"),
            _row("CONT_AWD_A-1_8000", "TX-2", "2025-09-02", "P00032", "2024-09-30"),
        ]
    )
    q = _mirror_with(cur, monkeypatch)

    (result,) = q.fetch_initial_reported_end_dates([target("A-1")])

    assert result.initial_end_date == "2022-12-31"
    assert result.basis == "base_transaction"
    assert result.status == "resolved"
    assert result.transaction_id == "TX-1"


def test_mirror_reaches_back_before_the_tracking_window(monkeypatch):
    """The originally-awarded end date predates the window for any long-running
    award, so this query must not carry the window bound the detection nets do."""
    cur = FakeCursor(
        [_row("CONT_AWD_A-1_8000", "TX-1", "2017-07-01", "0", "2022-12-31")]
    )
    q = _mirror_with(cur, monkeypatch)
    q.fetch_initial_reported_end_dates([target("A-1")])

    sql = [s for s, _ in cur.executed if s.strip().startswith("SELECT")]
    assert mirror.INITIAL_END_DATE_START in sql[0]
    assert mirror.TRACKING_WINDOW_START not in sql[0]


def test_an_award_absent_from_the_lagging_mirror_is_recorded_not_raised(monkeypatch):
    """The mirror lags the live API by 2-6 weeks, so a just-flagged award can be
    genuinely missing. One absent award must not abort the others."""
    cur = FakeCursor(
        [_row("CONT_AWD_A-1_8000", "TX-1", "2017-07-01", "0", "2022-12-31")]
    )
    q = _mirror_with(cur, monkeypatch)

    results = {
        r.award_id: r
        for r in q.fetch_initial_reported_end_dates([target("A-1"), target("B-2")])
    }

    assert results["A-1"].status == "resolved"
    assert results["B-2"].status == "not_in_mirror"
    assert results["B-2"].initial_end_date == ""


def test_the_query_is_keyed_on_the_generated_award_id(monkeypatch):
    """Not COALESCE(piid, fain, uri). This query has no tracking-window bound to
    give it index access, so a non-sargable expression here would leave nothing
    keeping Postgres off a scan of the 236M-row transaction table."""
    cur = FakeCursor(
        [_row("CONT_AWD_A-1_8000", "TX-1", "2017-07-01", "0", "2022-12-31")]
    )
    q = _mirror_with(cur, monkeypatch)
    q.fetch_initial_reported_end_dates([target("A-1")])

    sql, params = next(
        (s, p) for s, p in cur.executed if s.strip().startswith("SELECT")
    )
    assert "generated_unique_award_id = ANY" in sql
    assert "COALESCE(ts.piid" not in sql
    assert params == {"generated_award_ids": ["CONT_AWD_A-1_8000"]}


def test_a_missing_column_fails_loudly_and_names_what_it_needs(monkeypatch):
    """transaction_unique_id and ordering_period_end_date are the two columns
    no detection net proves exist. This feeds a write-once provenance file, so
    a silently substituted column would be recorded as `resolved` and never
    revisited - it must abort instead."""
    cur = FakeCursor([], execute_error=Exception('column "ordering_period_end_date"'))
    q = _mirror_with(cur, monkeypatch)

    with pytest.raises(RuntimeError, match="ordering_period_end_date"):
        q.fetch_initial_reported_end_dates([target("A-1")])


def test_the_query_never_asks_for_the_api_display_name(monkeypatch):
    """`last_date_to_order` is what the API CALLS this value, not what any
    table stores it as - the SQL asked for it by display name and every run
    died on UndefinedColumn. The real column is ordering_period_end_date, and
    it is TEXT here while the period-of-performance end is DATE, so both arms
    of the COALESCE must be text or Postgres cannot match the types."""
    cur = FakeCursor([])
    q = _mirror_with(cur, monkeypatch)
    q.fetch_initial_reported_end_dates([target("A-1")])

    sql, _ = next((s, p) for s, p in cur.executed if s.strip().startswith("SELECT"))
    # Comments stripped: the SQL explains the trap by name, and only what
    # Postgres actually parses is under test here.
    executable = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    assert "last_date_to_order" not in executable
    assert "ts.ordering_period_end_date" in executable
    assert "period_of_performance_current_end_date::text" in executable


def test_no_targets_makes_no_connection(monkeypatch):
    q = mirror.LocalUSASpendingMirrorQuery.__new__(mirror.LocalUSASpendingMirrorQuery)
    assert q.fetch_initial_reported_end_dates([]) == []


def test_missing_credentials_raise_the_typed_unavailable_error(monkeypatch):
    cur = FakeCursor([])
    q = _mirror_with(cur, monkeypatch, configured=False)
    with pytest.raises(mirror.LocalMirrorUnavailableError):
        q.fetch_initial_reported_end_dates([target("A-1")])


def test_a_lagging_award_is_not_written_and_so_is_retried(workdir, monkeypatch):
    """not_in_mirror is the one NON-terminal outcome. The sidecar is write-once
    and enrichment skips anything already in it, so persisting a 2-6 week
    replication lag would retire the award from lookup permanently."""
    monkeypatch.setattr(
        search_module.LocalUSASpendingMirrorQuery,
        "fetch_initial_reported_end_dates",
        lambda self, targets: [
            ied.InitialEndDateResult(
                t.award_id,
                t.generated_award_id,
                t.category,
                "",
                "",
                "",
                "",
                "",
                "not_in_mirror",
            )
            for t in targets
        ],
    )
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}
    obj.skipped_sources = set()

    obj._enrich_initial_reported_end_dates()

    assert obj.initial_end_date_rows == {}
    assert not obj.initial_end_dates_changed
    assert bml.load_initial_end_dates() == {}


def test_without_mirror_credentials_enrichment_is_skipped_not_fatal(
    workdir, monkeypatch, capsys
):
    """The CI path. The public API cannot supply this column at all, so a run
    without mirror access resolves nothing and leaves the committed sidecar
    exactly as it was."""

    def unavailable(self, targets):
        raise mirror.LocalMirrorUnavailableError("no database credentials")

    monkeypatch.setattr(
        search_module.LocalUSASpendingMirrorQuery,
        "fetch_initial_reported_end_dates",
        unavailable,
    )
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}
    obj.skipped_sources = set()

    obj._enrich_initial_reported_end_dates()

    assert not obj.initial_end_dates_changed
    assert "Skipping Initial Reported End Date" in capsys.readouterr().err


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

    def fake_fetch(targets):
        seen.extend(targets)
        return [
            ied.InitialEndDateResult(
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

    monkeypatch.setattr(
        search_module.LocalUSASpendingMirrorQuery,
        "fetch_initial_reported_end_dates",
        lambda self, targets: fake_fetch(targets),
    )
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}
    obj.skipped_sources = set()

    obj._enrich_initial_reported_end_dates()

    assert {item.award_id for item in seen} == {"OLD-1", "NEW-1"}
    assert obj.initial_end_dates_changed
    stored = bml.load_initial_end_dates()
    assert set(stored) == {"OLD-1", "NEW-1"}
    assert stored["OLD-1"]["Award Category"] == "assistance"
    assert stored["NEW-1"]["Initial Reported End Date"] == "2024-12-31"


def test_cached_terminal_result_prevents_a_repeat_lookup(
    workdir, write_csv, monkeypatch
):
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
        search_module.LocalUSASpendingMirrorQuery,
        "fetch_initial_reported_end_dates",
        lambda self, targets: pytest.fail("cached award was queried again"),
    )
    obj = search_module.Search.__new__(search_module.Search)
    obj.client = object()
    obj.unique_award_ids = ["NEW-1"]
    obj.awards_by_id = {"NEW-1": FakeAward()}
    obj.skipped_sources = set()

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
            "Latest Action Date": "2026-01-01",
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
            return __import__("pandas").DataFrame(
                [
                    {
                        "Award ID": "NEW-1",
                        "action_date": "2025-06-01",
                        "detection_basis": "evidence",
                        "detection_method": "external_claim",
                    }
                ]
            )

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
    obj.transaction_facts_changed = False
    obj.window_rejects = []

    monkeypatch.setattr(obj, "_build_claim_index", lambda: None)
    monkeypatch.setattr(obj, "_fetch_awards", lambda: None)
    monkeypatch.setattr(obj, "_resolve_stragglers", lambda: None)

    def enrich():
        obj.initial_end_dates_changed = True
        obj.initial_end_date_rows = {
            "NEW-1": {"Initial Reported End Date": "2024-12-31"}
        }

    monkeypatch.setattr(obj, "_enrich_initial_reported_end_dates", enrich)
    monkeypatch.setattr(obj, "_enrich_transaction_facts", lambda: None)
    monkeypatch.setattr(
        obj,
        "_add_source_awards",
        lambda source, award_ids: obj.unique_cancellations.update({"NEW-1": prior_row}),
    )
    monkeypatch.setattr(obj, "_report_review_queue", lambda: None)
    builds = []
    monkeypatch.setattr(
        search_module.build_master_ledger, "build", lambda: builds.append(1)
    )

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
                    # Part of every source's output contract now; the ingest
                    # tracking-window gate reads both.
                    "action_date": "2025-06-01",
                    "detection_basis": "evidence",
                }
            ]
        )
    }
    obj.unique_cancellations = {}
    obj.claims = {}
    obj.unresolved = {}
    obj.ignore_award_ids = []
    obj.window_rejects = []
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
    obj.initial_end_date_rows = {"NEW-1": {"Initial Reported End Date": "2024-12-31"}}

    obj._add_source_awards("DOGE", ["NEW-1"])

    assert (
        obj.unique_cancellations["NEW-1"]["Initial Reported End Date"] == "2024-12-31"
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


def test_incremental_build_does_not_overwrite_recorded_initial_date(workdir, write_csv):
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
