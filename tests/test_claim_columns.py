"""Claims are captured write-once and can never be erased by a later source."""

import csv

import build_master_ledger as bml
import reverify_awards

COLS = [
    "Source",
    "Recipient Congressional District",
    "Recipient Name",
    "Award ID",
    "Latest Modification Number",
    "Start Date",
    "Current End Date",
    "Current Obligated Amount",
    "Total Outlays",
    "Award or Action Description",
    "Recipient Business Categories",
    "USAspending URL",
    "Claimed By",
    "DOGE Claimed Status",
    "DOGE Claimed Savings",
    "DOGE Claim Date",
]

DOGE_DESC = (
    "Status: TERMINATED. Reported savings: $1,423,496.00. "
    "DOGE Action Date: 4/14/2025. Knowledge management services."
)
GRANT_DESC = (
    "DOGE Action Date: 3/20/2025. Reported savings: $96,700. "
    "Dc-8 and sofia aircraft storage."
)


def row(aid, source, desc="", **extra):
    r = {c: "" for c in COLS}
    r.update(
        {
            "Source": source,
            "Award ID": aid,
            "Recipient Name": f"R {aid}",
            "Award or Action Description": desc,
            "USAspending URL": f"https://www.usaspending.gov/award/CONT_AWD_{aid}/",
        }
    )
    r.update(extra)
    return r


def write_snap(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def ledger():
    with open(bml.LEDGER_PATH, encoding="utf-8") as fh:
        return {r["Award ID"]: r for r in csv.DictReader(fh)}


# --- parsing ---------------------------------------------------------------


def test_parses_contract_claim_prose():
    c = bml.parse_claim_from_description(DOGE_DESC)
    assert c["Claimed By"] == "DOGE"
    assert c["DOGE Claimed Status"] == "TERMINATED"
    assert c["DOGE Claimed Savings"] == "1423496.00"
    assert c["DOGE Claim Date"] == "4/14/2025"


def test_parses_grant_claim_prose_without_status():
    c = bml.parse_claim_from_description(GRANT_DESC)
    assert c["Claimed By"] == "DOGE"
    assert c["DOGE Claimed Status"] == ""
    assert c["DOGE Claimed Savings"] == "96700.00"


def test_non_doge_description_is_not_a_claim():
    assert (
        bml.parse_claim_from_description("Terminate for convenience per mod P00003")
        == {}
    )
    assert bml.parse_claim_from_description("") == {}


def test_savings_normalization_makes_formats_comparable():
    """DOGE's serialization gained ".00" on 2025-05-23; that is not a restatement."""
    assert bml.normalize_savings("82838") == bml.normalize_savings("82838.00")
    assert bml.normalize_savings("$1,423,496") == "1423496.00"
    assert bml.normalize_savings("") == ""
    assert bml.normalize_savings("n/a") == "n/a"


# --- stickiness ------------------------------------------------------------


def test_claim_survives_a_day_when_another_source_wins_the_row(workdir):
    """The core regression: DOGE claims on days 1-2, only NPDV flags on day 3.

    Before claims were sticky, day 3's blank claim fields would overwrite the
    claim and it would vanish from the published ledger.
    """
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    write_snap(
        "consolidated/nasa_x_2026-01-01.csv", [keep, row("X-1", "DOGE", DOGE_DESC)]
    )
    write_snap(
        "consolidated/nasa_x_2026-01-02.csv", [keep, row("X-1", "DOGE", DOGE_DESC)]
    )
    write_snap(
        "consolidated/nasa_x_2026-01-03.csv",
        [keep, row("X-1", "NPDV", "Terminate for convenience mod P00004")],
    )
    bml.build()

    x = ledger()["X-1"]
    assert x["Claimed By"] == "DOGE"
    assert x["DOGE Claimed Status"] == "TERMINATED"
    assert x["DOGE Claimed Savings"] == "1423496.00"
    assert x["Flagged By"] == "DOGE; NPDV"


def test_claim_captured_from_real_columns_not_only_prose(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    write_snap(
        "consolidated/nasa_x_2026-01-01.csv",
        [
            keep,
            row(
                "X-1",
                "DOGE",
                "no prose here",
                **{
                    "Claimed By": "DOGE",
                    "DOGE Claimed Status": "Expired",
                    "DOGE Claimed Savings": "50000",
                    "DOGE Claim Date": "2025-03-20",
                },
            ),
        ],
    )
    bml.build()
    x = ledger()["X-1"]
    assert x["DOGE Claimed Status"] == "Expired"
    assert x["DOGE Claimed Savings"] == "50000.00"  # normalized on the column path too


def test_build_canonicalizes_a_legacy_nasa_assistance_url(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    legacy = row(
        "80NSSC22M0122",
        "NASAGrants",
        "Administrative - Decrease",
        **{
            "USAspending URL": (
                "https://www.usaspending.gov/award/ASST_NON_80NSSC22M0122_8000/"
            )
        },
    )
    write_snap("consolidated/nasa_x_2026-01-01.csv", [keep, legacy])

    bml.build()

    assert ledger()["80NSSC22M0122"]["USAspending URL"].endswith(
        "/ASST_NON_80NSSC22M0122_080/"
    )


def test_build_uses_auto_transaction_baseline_for_a_zero_snapshot_amount(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    claimed = row(
        "A-0",
        "DOGE",
        DOGE_DESC,
        **{"Current Obligated Amount": "0.00"},
    )
    write_snap("consolidated/nasa_x_2026-01-01.csv", [keep, claimed])

    auto = {column: "" for column in reverify_awards.AUTO_COLUMNS}
    auto.update(
        {
            "Award ID": "A-0",
            "Peak Cumulative Obligation": "44325.00",
        }
    )
    with open(bml.AUTO_VERIFICATION_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=reverify_awards.AUTO_COLUMNS)
        writer.writeheader()
        writer.writerow(auto)

    bml.build()

    result = ledger()["A-0"]
    assert result["Obligated Amount When First Flagged"] == "0.00"
    assert result["Peak Cumulative Obligation"] == "44325.00"
    assert result["Amount Trend"] == "shrank"
    assert result["DOGE Claim vs Outcome"] == "claimed_and_shrank"


def test_restatement_is_logged_not_overwritten(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    first = DOGE_DESC
    second = DOGE_DESC.replace("Status: TERMINATED", "Status: Expired")
    write_snap("consolidated/nasa_x_2026-01-01.csv", [keep, row("X-1", "DOGE", first)])
    write_snap("consolidated/nasa_x_2026-01-02.csv", [keep, row("X-1", "DOGE", second)])
    bml.build()

    x = ledger()["X-1"]
    assert x["DOGE Claimed Status"] == "TERMINATED", "original claim must be preserved"
    assert "DOGE Claimed Status=Expired" in x["DOGE Claim Revisions"]
    assert "2026-01-02" in x["DOGE Claim Revisions"]


def test_incremental_build_does_not_re_log_an_old_revision(workdir):
    """The two build paths must agree on Claim Revisions.

    `--update` reads the ledger back from disk, so the in-memory record of what
    was last seen is gone. Without reseeding it from the stored revisions, a
    claim revised on an earlier day gets appended again on every later run.
    """
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    first = DOGE_DESC
    second = DOGE_DESC.replace("Status: TERMINATED", "Status: Expired")
    write_snap("consolidated/nasa_x_2026-01-01.csv", [keep, row("X-1", "DOGE", first)])
    write_snap("consolidated/nasa_x_2026-01-02.csv", [keep, row("X-1", "DOGE", second)])
    bml.build()
    after_full = ledger()["X-1"]["DOGE Claim Revisions"]

    # A later day repeating the revised claim must add nothing.
    write_snap("consolidated/nasa_x_2026-01-03.csv", [keep, row("X-1", "DOGE", second)])
    bml.build(update_only=True)
    assert ledger()["X-1"]["DOGE Claim Revisions"] == after_full

    bml.build()
    assert ledger()["X-1"]["DOGE Claim Revisions"] == after_full


def test_incremental_build_cannot_infer_history_based_statuses(workdir):
    """Why search.py does a FULL rebuild, not --update.

    classify() reasons over every description ever observed. The incremental
    path rebuilds that history from the latest snapshot alone, so a status that
    depends on older text is lost. On 2026-07-30 this downgraded six rescinded
    awards from `reinstated` to `dropped_pending_review` in the published
    ledger. This test pins the difference so the daily path is never quietly
    switched back.
    """
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    resc = row("R-1", "NPDV", "Rescinding stop work notice")
    write_snap("consolidated/nasa_x_2026-01-01.csv", [keep, resc])
    write_snap("consolidated/nasa_x_2026-01-02.csv", [keep, resc])

    # While R-1 is still in the snapshot it is simply `listed`.
    bml.build()
    assert ledger()["R-1"]["Tracking Status"] == "listed"

    # It drops out. The rescission text now lives only in older snapshots,
    # which the incremental path never reads.
    write_snap("consolidated/nasa_x_2026-01-03.csv", [keep])
    bml.build(update_only=True)
    assert ledger()["R-1"]["Tracking Status"] == "dropped_pending_review", (
        "if --update can now infer this, the full-rebuild rationale in "
        "search.py should be revisited"
    )

    # A full rebuild sees the whole history and gets it right.
    bml.build()
    assert ledger()["R-1"]["Tracking Status"] == "reinstated"


def test_reverted_grants_experiment_never_enters_the_ledger(workdir):
    """The 2026-01-08 'Cancelled grants' run was reverted the next day.

    Its rows are dropped at ingest rather than recorded and reclassified, so
    66 phantom awards stay out of an artifact meant to be citable.
    """
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    errant = row("ERR-1", "NASAGrants", "Cancelled - grant status")
    write_snap("consolidated/nasa_x_2026-01-08.csv", [keep, errant])
    write_snap("consolidated/nasa_x_2026-01-09.csv", [keep])
    bml.build()
    assert "ERR-1" not in ledger()
    assert "KEEP-1" in ledger()


def test_only_that_source_on_that_date_is_dropped(workdir):
    """A NASAGrants award seen on other dates keeps its real observations -
    23 of the 89 rows that day were legitimate."""
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    real = row("REAL-1", "NASAGrants", "Administrative - Decrease")
    other_source = row("OTHER-1", "NPDV", "terminated")
    write_snap("consolidated/nasa_x_2026-01-07.csv", [keep, real])
    write_snap("consolidated/nasa_x_2026-01-08.csv", [keep, real, other_source])
    write_snap("consolidated/nasa_x_2026-01-09.csv", [keep, real])
    bml.build()
    led = ledger()
    # Seen on other dates, so it survives - and its First Seen predates the
    # experiment rather than being set by the ignored row.
    assert led["REAL-1"]["First Flagged Date"] == "2026-01-07"
    assert led["REAL-1"]["Last Flagged Date"] == "2026-01-09"
    # A different source on the same date is untouched.
    assert "OTHER-1" in led


def test_same_source_on_a_different_date_is_untouched(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    grant = row("G-1", "NASAGrants", "Administrative - Decrease")
    write_snap("consolidated/nasa_x_2026-01-09.csv", [keep, grant])
    bml.build()
    assert "G-1" in ledger()


def test_parse_claim_revisions_reads_back_what_record_claim_wrote():
    text = (
        "2026-01-02 DOGE Claimed Status=Expired; "
        "2026-03-04 DOGE Claimed Savings=50000.00"
    )
    assert bml.parse_claim_revisions(text) == {
        "DOGE Claimed Status": "Expired",
        "DOGE Claimed Savings": "50000.00",
    }
    assert bml.parse_claim_revisions("") == {}


def test_unchanged_claim_logs_no_revision(workdir):
    keep = row("KEEP-1", "NPDV", "ordinary termination")
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        write_snap(
            f"consolidated/nasa_x_{day}.csv", [keep, row("X-1", "DOGE", DOGE_DESC)]
        )
    bml.build()
    assert ledger()["X-1"]["DOGE Claim Revisions"] == ""
