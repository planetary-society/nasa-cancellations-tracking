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
        "NPDV": dm.DESCRIPTION_KEYWORD,
        "NASAGrants": dm.POP_END_DATE_CHANGE,
        "FPDS": dm.LEGACY_FPDS_KEYWORD,
        "LocalUSASpendingMirror": dm.LEGACY_LOCAL_MIRROR_SIGNAL,
        "USAspendingTerminations": dm.LEGACY_USASPENDING_SIGNAL,
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
                "Source": "LocalUSASpendingMirror",
                "Detection": "End date shortened 365 days from 2027-01-01 to "
                "2026-01-01 by mod P00002 on 2026-01-01",
            }
        )
        == dm.POP_END_DATE_CHANGE
    )
    assert (
        dm.infer_snapshot_method(
            {
                "Source": "USAspendingTerminations",
                "Detection": "Terminate-for-convenience action P00002 on 2026-01-01",
            }
        )
        == dm.TERMINATION_ACTION_CODE
    )


def test_master_ledger_rebuild_populates_every_historical_row(workdir, write_csv):
    columns = ["Source", "Award ID", "Recipient", "Description", "Detection"]
    write_csv(
        "consolidated/nasa_contract_cancellations_2026-01-01.csv",
        columns,
        [
            {
                "Source": "NPDV",
                "Award ID": "N-1",
                "Recipient": "NPDV recipient",
            },
            {
                "Source": "LocalUSASpendingMirror",
                "Award ID": "L-1",
                "Recipient": "Mirror recipient",
            },
        ],
    )

    bml.build()

    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        rows = {row["Award ID"]: row for row in csv.DictReader(fh)}
    assert rows["N-1"]["Primary Detection Method"] == dm.DESCRIPTION_KEYWORD
    assert rows["L-1"]["Primary Detection Method"] == dm.LEGACY_LOCAL_MIRROR_SIGNAL
    assert all(row["Primary Detection Method"] for row in rows.values())
