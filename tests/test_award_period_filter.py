"""Transaction-level period shortening and NASA confirmation tests."""

import csv
from pathlib import Path

import pandas as pd
import pytest

import award_period
import award_period_change_facts as period_facts
import local_usaspending_mirror_query as mirror
import reverify_awards
import search
import sources
from tests.helpers import FakeTxn


def txn(
    action_date,
    modification_number,
    end_date,
    *,
    obligation=0,
    transaction_id=None,
    award_id="A-1",
):
    return {
        "award_id_native": award_id,
        "generated_unique_award_id": f"ASST_NON_{award_id}_080",
        "transaction_id": transaction_id or f"T-{modification_number}",
        "action_date": action_date,
        "modification_number": modification_number,
        "end_date": end_date,
        "federal_action_obligation": obligation,
    }


def fact_row(award_id="A-1", **overrides):
    row = {
        "Award ID": award_id,
        "Generated Award ID": f"ASST_NON_{award_id}_080",
        "Previous End Date": "2027-02-28",
        "Resulting End Date": "2026-02-25",
        "Shortening Days": "368",
        "Modification Number": "P00002",
        "Action Date": "2026-02-25",
        "Source Transaction ID": "T-P00002",
        "Federal Action Obligation": "-19352.13",
        "Last Checked Date": "2026-08-03",
    }
    row.update(overrides)
    return row


def test_shortening_arithmetic_rejects_extensions_and_is_strict_at_threshold():
    assert award_period.shortening_days("2026-01-01", "2027-01-01") == -365
    assert not award_period.is_significant_shortening(
        "2026-06-30", "2026-04-01", min_days=90
    )
    assert award_period.is_significant_shortening(
        "2026-07-01", "2026-04-01", min_days=90
    )


@pytest.mark.parametrize("value", ["nope", "-1", "1.5"])
def test_invalid_shortening_configuration_fails_loudly(value):
    with pytest.raises(RuntimeError, match=award_period.SHORTENING_MIN_DAYS_ENV):
        award_period._configured_min_days(value)


def test_blank_configuration_uses_the_three_month_default():
    assert award_period._configured_min_days("") == 90


def test_reference_rule_uses_consecutive_dated_transactions_and_largest_drop():
    rows = [
        txn("2025-02-01", "0", "2027-01-01"),
        txn("2025-03-01", "P00001", None),  # does not break the dated chain
        txn("2025-04-01", "P0002", "2026-09-01", obligation=0),
        txn("2025-04-01", "P00010", "2026-01-01", obligation=-1),
        # Later continuation does not erase the largest historical decrease.
        txn("2026-06-01", "P00011", "2028-01-01", obligation=100),
    ]

    selected = period_facts.select_largest_change(rows, run_date="2026-08-03")

    assert selected["modification_number"] == "P00010"
    assert selected["previous_end_date"] == "2026-09-01"
    assert selected["end_date"] == "2026-01-01"
    assert selected["days_truncated"] == 243


@pytest.mark.parametrize(
    "action,end_date,obligation,expected",
    [
        ("2025-01-20", "2025-06-01", 0, False),  # action boundary is strict
        ("2025-01-21", "2025-01-20", 0, True),  # resulting lower bound inclusive
        ("2025-01-21", "2025-01-19", 0, False),
        ("2026-08-03", "2026-08-03", 0, True),  # run-date upper bound inclusive
        ("2026-08-03", "2026-08-04", 0, False),
        ("2025-01-21", "2025-06-01", -1, True),
        ("2025-01-21", "2025-06-01", 1, False),
    ],
)
def test_reference_rule_applies_action_effect_and_obligation_gates(
    action, end_date, obligation, expected
):
    rows = [
        txn("2025-01-01", "0", "2027-01-01"),
        txn(action, "P00001", end_date, obligation=obligation),
    ]
    assert (
        period_facts.select_largest_change(rows, run_date="2026-08-03") is not None
    ) is expected


def test_threshold_is_strict_for_consecutive_changes():
    exactly = [
        txn("2025-01-01", "0", "2026-06-30"),
        txn("2025-02-01", "P1", "2026-04-01"),
    ]
    above = [
        txn("2025-01-01", "0", "2026-07-01"),
        txn("2025-02-01", "P1", "2026-04-01"),
    ]
    assert period_facts.select_largest_change(exactly, run_date="2026-08-03") is None
    assert period_facts.select_largest_change(above, run_date="2026-08-03")


def test_80nssc25k7577_remains_a_qualifying_detection():
    rows = [
        txn("2025-05-19", "0", "2026-02-28", obligation=20000),
        txn("2025-09-01", "P00001", "2027-02-28", obligation=0),
        txn(
            "2026-02-25",
            "P00002",
            "2026-02-25",
            obligation=-19352.13,
            award_id="80NSSC25K7577",
        ),
    ]

    selected = period_facts.select_largest_change(rows, run_date="2026-08-03")
    fact = period_facts.build_fact_row(selected, checked="2026-08-03")

    assert fact["Shortening Days"] == "368"
    assert fact["Modification Number"] == "P00002"
    assert period_facts.detection_text(fact) == (
        "End date shortened 368 days from 2027-02-28 to 2026-02-25 "
        "by mod P00002 on 2026-02-25"
    )

    verdict = reverify_awards.classify_transactions(
        [
            FakeTxn("2025-05-19", "0", "A", federal_action_obligation=20000),
            FakeTxn("2025-09-01", "P00001", "B", federal_action_obligation=0),
            FakeTxn(
                "2026-02-25",
                "P00002",
                "D",
                federal_action_obligation=-19352.13,
                action_type_description="ADJUSTMENT TO COMPLETED PROJECT",
            ),
        ],
        is_contract=False,
        ledger_row={"Current End Date": "2026-02-25"},
    )
    assert verdict.status == "naturally_expired"


def test_facts_round_trip_and_replace_atomically(tmp_path):
    path = tmp_path / "facts.csv"
    period_facts.write_facts({"A-1": fact_row()}, str(path))

    assert period_facts.load_facts(str(path))["A-1"] == fact_row()
    assert not list(tmp_path.glob(".award-period-change-facts-*"))


def test_failed_atomic_replace_retains_prior_facts(tmp_path, monkeypatch):
    path = tmp_path / "facts.csv"
    period_facts.write_facts({"A-1": fact_row()}, str(path))
    before = path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("disk unavailable")

    monkeypatch.setattr(period_facts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        period_facts.write_facts(
            {"A-2": fact_row("A-2")},
            str(path),
        )

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".award-period-change-facts-*"))


def _write_default_facts(rows):
    path = Path(period_facts.FACTS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=period_facts.FACT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _search_obj(nasa_ids, *, other_source_ids=(), skipped_sources=()):
    obj = search.Search.__new__(search.Search)
    # Real state, not test scaffolding: the filter consults it to tell
    # "no candidate qualified" from "nothing could be checked".
    obj.skipped_sources = set(skipped_sources)
    obj.sources_cancellation_data = {
        "NASA Grants": pd.DataFrame(
            {
                "Award ID": nasa_ids,
                "status": ["Administrative - Decrease"] * len(nasa_ids),
                "action_date": [""] * len(nasa_ids),
                "detection_basis": ["inference"] * len(nasa_ids),
            }
        ),
        "Other": pd.DataFrame({"Award ID": list(other_source_ids)}),
    }
    obj.unique_award_ids = list(dict.fromkeys([*nasa_ids, *other_source_ids]))
    return obj


def test_nasa_grants_requires_and_uses_persisted_mirror_confirmation(workdir, capsys):
    _write_default_facts([fact_row("CONFIRMED")])
    obj = _search_obj(["CONFIRMED", "UNCONFIRMED"])

    obj._filter_nasa_grant_period_changes()

    frame = obj.sources_cancellation_data["NASA Grants"]
    assert frame["Award ID"].tolist() == ["CONFIRMED"]
    assert frame.iloc[0]["action_date"] == "2026-02-25"
    assert frame.iloc[0]["status"].startswith("End date shortened 368 days")
    assert obj.unique_award_ids == ["CONFIRMED"]
    assert "UNCONFIRMED" in capsys.readouterr().err


def test_unconfirmed_nasa_candidate_survives_through_another_source(workdir):
    _write_default_facts([])
    obj = _search_obj(["A-1"], other_source_ids=("A-1",))

    obj._filter_nasa_grant_period_changes()

    assert obj.sources_cancellation_data["NASA Grants"].empty
    assert obj.unique_award_ids == ["A-1"]


def test_mirror_failure_never_replaces_prior_period_facts(monkeypatch):
    query = mirror.LocalUSASpendingMirrorQuery.__new__(
        mirror.LocalUSASpendingMirrorQuery
    )
    monkeypatch.setattr(query, "_require_configured", lambda: None)

    def fail_query():
        raise RuntimeError("Q3 failed")

    monkeypatch.setattr(query, "_query_mirror", fail_query)
    writes = []
    monkeypatch.setattr(period_facts, "write_facts", lambda rows: writes.append(rows))

    with pytest.raises(RuntimeError, match="Q3 failed"):
        query.search()

    assert writes == []


def test_complete_mirror_query_writes_facts_before_export(monkeypatch):
    query = mirror.LocalUSASpendingMirrorQuery.__new__(
        mirror.LocalUSASpendingMirrorQuery
    )
    query.period_change_fact_rows = {"A-1": fact_row()}
    monkeypatch.setattr(query, "_require_configured", lambda: None)
    frame = pd.DataFrame([{"Award ID": "A-1"}])
    monkeypatch.setattr(query, "_query_mirror", lambda: frame)
    events = []
    monkeypatch.setattr(
        period_facts, "write_facts", lambda rows: events.append(("facts", rows))
    )
    monkeypatch.setattr(
        query,
        "export_to_csv",
        lambda data, filename: events.append(("export", filename)),
    )

    assert query.search() is frame
    assert [event[0] for event in events] == ["facts", "export"]


def test_mirror_sql_encodes_the_transaction_level_methodology():
    sql = mirror.Q3_END_DATE_TRUNCATION
    assert "LAG(end_date)" in sql
    assert "previous_end_date - end_date > 90" in sql
    assert "action_date > '2025-01-20'" in sql
    assert "end_date BETWEEN '2025-01-20'" in sql
    assert "federal_action_obligation <= 0" in sql
    assert "ROW_NUMBER()" in sql
    assert "days_truncated DESC" in sql
    assert "max_end_ever" not in sql


def test_grants_degrades_to_skipped_when_nothing_can_confirm_it(workdir, capsys):
    """An unconfirmable source is unknown, not zero.

    Only the local mirror writes period-change facts. With the mirror away and
    no facts on file, every candidate is rejected - and a source reported as
    zero trips validate_snapshot's presence and shrinkage guards, quarantining
    every run until the mirror returns. Declaring it skipped degrades the way
    the mirror it depends on already does.
    """
    _write_default_facts([])
    obj = _search_obj(
        ["UNCONFIRMED-1", "UNCONFIRMED-2"],
        skipped_sources=[sources.LOCAL_MIRROR],
    )

    obj._filter_nasa_grant_period_changes()

    assert sources.NASA_GRANTS in obj.skipped_sources
    assert obj.sources_cancellation_data[sources.NASA_GRANTS].empty
    assert sources.LOCAL_MIRROR in capsys.readouterr().err


def test_grants_is_not_skipped_when_the_mirror_ran_and_confirmed_nothing(workdir):
    """A mirror that ran and found no qualifying transaction is a real zero.

    Suppressing the guards here would hide the failure they exist to catch.
    """
    _write_default_facts([])
    obj = _search_obj(["UNCONFIRMED-1"])

    obj._filter_nasa_grant_period_changes()

    assert sources.NASA_GRANTS not in obj.skipped_sources


def test_a_partial_confirmation_is_not_a_skip(workdir):
    """Prior facts outliving an absent mirror is the documented behaviour, so
    a run that still confirms something has produced real rows."""
    _write_default_facts([fact_row("CONFIRMED")])
    obj = _search_obj(
        ["CONFIRMED", "UNCONFIRMED"],
        skipped_sources=[sources.LOCAL_MIRROR],
    )

    obj._filter_nasa_grant_period_changes()

    assert sources.NASA_GRANTS not in obj.skipped_sources
    assert obj.sources_cancellation_data[sources.NASA_GRANTS]["Award ID"].tolist() == [
        "CONFIRMED"
    ]
