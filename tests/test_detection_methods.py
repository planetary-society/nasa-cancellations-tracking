import csv

import build_master_ledger as bml
import detection_methods as dm
import local_usaspending_mirror_query as mirror


def row(method):
    return {"detection_method": method}


def test_local_mirror_primary_method_prefers_direct_evidence():
    assert (
        dm.primary_local_method(
            [row("end_date_truncation"), row("description_regex"), row("action_code")]
        )
        == dm.TERMINATION_ACTION_CODE
    )


def test_local_mirror_primary_method_prefers_clawback_to_pop_change():
    assert (
        dm.primary_local_method([row("end_date_truncation"), row("clawback")])
        == dm.OBLIGATION_CLAWBACK
    )


def test_local_mirror_output_names_a_pure_pop_change():
    frame = mirror._combine(
        [
            {
                "award_id_native": "A-1",
                "detection_method": "end_date_truncation",
                "days_truncated": 180,
                "previous_end_date": "2027-01-01",
                "end_date": "2026-07-05",
                "action_date": "2026-01-01",
            }
        ]
    )

    assert frame.iloc[0]["detection_method"] == dm.POP_END_DATE_CHANGE


def test_historical_snapshot_methods_are_backfilled_for_every_source():
    # Literal on purpose: deriving these from dm._SOURCE_FALLBACKS would assert
    # the table against itself. The coverage check below is the part that must
    # track the code.
    cases = {
        "DOGE": dm.EXTERNAL_CLAIM,
        "NASA Procurement Data View": dm.DESCRIPTION_KEYWORD,
        "NASA Grants": dm.POP_END_DATE_CHANGE,
        "FPDS": dm.LEGACY_FPDS_KEYWORD,
        "Local USAspending Mirror": dm.LEGACY_LOCAL_MIRROR_SIGNAL,
        "USAspending Terminations": dm.LEGACY_USASPENDING_SIGNAL,
    }

    assert {
        source: dm.infer_snapshot_method({"Source": source}) for source in cases
    } == cases


def test_every_live_source_declares_what_its_legacy_snapshots_imply():
    """A seventh source must not silently degrade to LEGACY_SOURCE_SIGNAL.

    Snapshots archived before Primary Detection Method existed are back-filled
    from the source name alone, so a source with no entry in the fallback table
    produces rows that say only "some source found this".
    """
    import search

    for source in search.SOURCES:
        assert dm.infer_snapshot_method({"Source": source}) != dm.LEGACY_SOURCE_SIGNAL


def test_detection_text_recovers_a_precise_method_before_legacy_fallback():
    assert (
        dm.infer_snapshot_method(
            {
                "Source": "Local USAspending Mirror",
                "Detection Evidence": "End date shortened 365 days from 2027-01-01 to "
                "2026-01-01 by mod P00002 on 2026-01-01",
            }
        )
        == dm.POP_END_DATE_CHANGE
    )
    assert (
        dm.infer_snapshot_method(
            {
                "Source": "USAspending Terminations",
                "Detection Evidence": "Terminate-for-convenience action P00002 on 2026-01-01",
            }
        )
        == dm.TERMINATION_ACTION_CODE
    )


def test_master_ledger_rebuild_populates_every_historical_row(workdir, write_csv):
    columns = [
        "Source",
        "Award ID",
        "Recipient Name",
        "Award or Action Description",
        "Detection Evidence",
    ]
    write_csv(
        "consolidated/nasa_contract_cancellations_2026-01-01.csv",
        columns,
        [
            {
                "Source": "NASA Procurement Data View",
                "Award ID": "N-1",
                "Recipient Name": "NPDV recipient",
            },
            {
                "Source": "Local USAspending Mirror",
                "Award ID": "L-1",
                "Recipient Name": "Mirror recipient",
            },
        ],
    )

    bml.build()

    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        rows = {row["Award ID"]: row for row in csv.DictReader(fh)}
    assert rows["N-1"]["Primary Detection Method"] == dm.DESCRIPTION_KEYWORD
    assert rows["L-1"]["Primary Detection Method"] == dm.LEGACY_LOCAL_MIRROR_SIGNAL
    assert all(row["Primary Detection Method"] for row in rows.values())


def test_the_mirror_splits_f_from_n_though_one_net_finds_both():
    """Q1 covers both codes under one net label, so the row's own action_type
    is the only thing that can tell the published methods apart."""
    assert (
        dm.primary_local_method(
            [{"detection_method": "action_code", "action_type": "N"}]
        )
        == dm.LEGAL_CONTRACT_CANCELLATION
    )
    assert (
        dm.primary_local_method(
            [{"detection_method": "action_code", "action_type": "F"}]
        )
        == dm.TERMINATION_ACTION_CODE
    )


def test_an_f_alongside_an_n_reads_as_the_f():
    """Priority order, not set iteration order, decides."""
    rows = [
        {"detection_method": "action_code", "action_type": "N"},
        {"detection_method": "action_code", "action_type": "F"},
    ]
    assert dm.primary_local_method(rows) == dm.TERMINATION_ACTION_CODE


def test_backfill_reads_joined_phrases_in_priority_order():
    """The mirror joins one award's net phrases with "; ", so a row found by
    two nets contains both strings and first-match decides. This must mirror
    _METHOD_PRIORITY or the weaker signal wins."""
    both = {
        "Detection Evidence": (
            "Legal-contract-cancellation action P00005 on 2025-03-11; "
            "Terminate-for-convenience action P00007 on 2025-04-02"
        )
    }
    assert dm.infer_snapshot_method(both) == dm.TERMINATION_ACTION_CODE

    n_only = {
        "Detection Evidence": "Legal-contract-cancellation action P00005 on 2025-03-11"
    }
    assert dm.infer_snapshot_method(n_only) == dm.LEGAL_CONTRACT_CANCELLATION


def test_every_method_a_producer_can_emit_is_in_the_vocabulary():
    """The guard for the whole class of bug.

    contract_query.validate_source_frame raises per source frame on an unknown
    method, and build_master_ledger raises on the WHOLE ledger before writing -
    so emitting a value that is not in DETECTION_METHODS costs the day's
    artifact, not one row. Walk the producers rather than restating the list.
    """
    import usaspending_terminations_query as utq

    emitted = (
        set(dm._LOCAL_NET_METHODS.values())
        | set(utq.ACTION_CODE_METHODS.values())
        | {
            dm.EXTERNAL_CLAIM,
            dm.DESCRIPTION_KEYWORD,
            dm.POP_END_DATE_CHANGE,
            dm.TERMINATION_LANGUAGE,
            dm.OBLIGATION_CLAWBACK,
            dm.LEGAL_CONTRACT_CANCELLATION,
        }
    )
    assert emitted <= set(dm.DETECTION_METHODS)


def test_every_prioritised_method_can_actually_be_produced():
    """The other direction: a method in the priority tuple that no net emits is
    dead ranking, and primary_local_method raises for anything absent from it."""
    assert set(dm._METHOD_PRIORITY) <= set(dm.DETECTION_METHODS)
    assert set(dm._LOCAL_NET_METHODS.values()) | {
        dm.LEGAL_CONTRACT_CANCELLATION
    } <= set(dm._METHOD_PRIORITY)
