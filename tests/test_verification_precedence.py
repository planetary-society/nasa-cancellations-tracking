"""Precedence between machine screening and human adjudication.

The human file is the project's citable evidence. These tests encode the two
guarantees that make automation safe to add: it can never write that file, and
it can never win an argument with it.
"""

import csv
import os
import re

import pytest

import build_master_ledger as bml
import reverify_awards

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def auto_row(aid, status, confidence="high", **extra):
    r = {c: "" for c in reverify_awards.AUTO_COLUMNS}
    r.update(
        {
            "Award ID": aid,
            "Automated Verdict": status,
            "Confidence": confidence,
            "Automated Verdict Date": "2026-07-30",
            "Evidence": f"machine call for {aid}",
        }
    )
    r.update(extra)
    return r


# --- the human file is never written --------------------------------------


def test_reverify_never_opens_the_human_file_for_writing():
    """Static guard: no write path to verification/dropped_award_status.csv."""
    source = open(os.path.join(REPO, "reverify_awards.py"), encoding="utf-8").read()
    # It may reference the constant (to READ human verdicts) but must never
    # hand it to open()-for-write, shutil.move, or a writer.
    # (?<!AUTO_) so the machine-owned AUTO_VERIFICATION_PATH doesn't match.
    assert not re.search(
        r"open\(\s*(?<!AUTO_)VERIFICATION_PATH\s*,\s*['\"][wa]", source
    )
    assert not re.search(
        r"(shutil\.move|os\.replace|os\.remove)\([^)]*(?<!AUTO_)VERIFICATION_PATH",
        source,
    )
    assert "write_auto" in source and "AUTO_VERIFICATION_PATH" in source


def test_reverify_writes_only_machine_owned_paths():
    source = open(os.path.join(REPO, "reverify_awards.py"), encoding="utf-8").read()
    written = set(re.findall(r"shutil\.move\(tmp, (\w+)\)", source))
    assert written <= {"AUTO_VERIFICATION_PATH"}


# --- precedence ------------------------------------------------------------


def test_human_verdict_beats_a_contradicting_high_confidence_auto(workdir):
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-1", "continued")],
    )
    write_csv(
        bml.VERIFICATION_PATH,
        ["Award ID", "Tracking Status", "Verified Date", "Evidence"],
        [
            {
                "Award ID": "A-1",
                "Tracking Status": "still_terminated",
                "Verified Date": "2026-07-29",
                "Evidence": "hand-checked",
            }
        ],
    )

    overrides = bml.load_verification({"A-1": {}})
    assert overrides["A-1"] == ("still_terminated", "hand-checked")


def test_high_confidence_auto_applies_when_no_human_row(workdir):
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-2", "closed_out")],
    )
    overrides = bml.load_verification({"A-2": {}})
    assert overrides["A-2"][0] == "closed_out"
    assert overrides["A-2"][1].startswith("[auto 2026-07-30]")


def test_low_confidence_auto_never_applies(workdir):
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-3", "still_terminated", confidence="low")],
    )
    assert bml.load_verification({"A-3": {}}) == {}


def test_unresolved_can_never_become_a_status(workdir):
    """A lookup failure is structurally incapable of setting a status."""
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-4", "unresolved", confidence="high")],
    )
    assert bml.load_verification({"A-4": {}}) == {}


@pytest.mark.parametrize(
    "status", ["naturally_expired", "no_termination_signal", "needs_manual_review"]
)
def test_absence_of_evidence_verdicts_never_apply(workdir, status):
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-5", status)],
    )
    assert bml.load_verification({"A-5": {}}) == {}


# --- the claim retention rule ---------------------------------------------


def test_a_claimed_award_is_never_pruned_by_automation(workdir):
    """`continued` would remove a claimed award from the cancellation count.

    DOGE claims are the fact being tracked, so automation may refine how a
    claimed award is described but may never prune it.
    """
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-6", "continued")],
    )
    ledger = {"A-6": {"Claimed By": "DOGE"}}
    assert bml.load_verification(ledger) == {}
    # ...while the same verdict applies to an award nobody claimed.
    assert bml.load_verification({"A-6": {}})["A-6"][0] == "continued"


def test_every_emitted_verdict_is_explicitly_classified():
    """Forces a decision when a new verdict is added.

    Every status reverify_awards can emit must appear in exactly one of
    AUTO_APPLICABLE / AUTO_NOT_APPLICABLE. Without this, a new verdict silently
    defaults to unusable — which is how `still_terminated` sat in
    AUTO_APPLICABLE while only ever being emitted at low confidence, and so
    could never reach the ledger.
    """
    source = open(os.path.join(REPO, "reverify_awards.py"), encoding="utf-8").read()
    emitted = set(re.findall(r'Verdict\(\s*\n?\s*"(\w+)"', source))
    classified = bml.AUTO_APPLICABLE | set(bml.AUTO_NOT_APPLICABLE)

    assert not emitted - classified, (
        f"verdicts emitted but not classified as applicable or not: "
        f"{sorted(emitted - classified)}"
    )
    assert not bml.AUTO_APPLICABLE & set(bml.AUTO_NOT_APPLICABLE), (
        "a status cannot be both applicable and not"
    )
    assert not classified - emitted, (
        f"classified statuses nothing emits (stale): {sorted(classified - emitted)}"
    )


def test_applicable_verdicts_are_actually_emitted_at_high_confidence():
    """An applicable status only emitted at low confidence is unreachable."""
    source = open(os.path.join(REPO, "reverify_awards.py"), encoding="utf-8").read()
    pairs = set(re.findall(r'Verdict\(\s*\n?\s*"(\w+)",\s*\n?\s*"(\w+)"', source))
    high = {status for status, confidence in pairs if confidence == "high"}

    unreachable = bml.AUTO_APPLICABLE - high
    assert not unreachable, (
        f"in AUTO_APPLICABLE but never emitted at high confidence, so it can "
        f"never set a Status: {sorted(unreachable)}"
    )


def test_claimed_retain_set_is_a_subset_of_applicable():
    assert bml.AUTO_APPLICABLE_CLAIMED <= bml.AUTO_APPLICABLE


def test_a_claimed_award_may_still_be_refined_within_the_retain_set(workdir):
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("A-7", "closed_out")],
    )
    ledger = {"A-7": {"Claimed By": "DOGE"}}
    assert bml.load_verification(ledger)["A-7"][0] == "closed_out"


def snapshot_row(aid, **extra):
    import search

    record = {column: "" for column in search.SNAPSHOT_COLUMNS}
    record.update(
        {
            "Source": "NASA Procurement Data View",
            "Award ID": aid,
            "Recipient Name": f"Recipient {aid}",
            "Award or Action Description": "terminate for convenience",
        }
    )
    record.update(extra)
    return record


def built_ledger():
    return {r["Award ID"]: r for r in bml.read_rows(bml.LEDGER_PATH)}


def test_a_human_verdict_outranks_presence_in_todays_snapshot(workdir, write_csv):
    """The invariant README states and nothing pinned.

    An award a person ruled out of scope is still emitted every day by the
    source that keeps re-detecting the same closeout modification. When
    snapshot presence won, 25 of 41 adjudications were discarded and their
    evidence blanked - 14 of them pre-window awards republished as active
    cancellations.
    """
    import search

    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        search.SNAPSHOT_COLUMNS,
        [snapshot_row("EXCLUDED-1"), snapshot_row("PLAIN-1")],
    )
    write_csv(
        bml.VERIFICATION_PATH,
        bml.VERIFICATION_COLUMNS,
        [
            {
                "Award ID": "EXCLUDED-1",
                "Tracking Status": "excluded_by_design",
                "Verified Date": "2026-07-31",
                "Evidence": "Pre-window: closeout of a 2023 decision.",
            }
        ],
    )

    bml.build()

    records = built_ledger()
    assert records["EXCLUDED-1"]["Tracking Status"] == "excluded_by_design"
    # The evidence for the ruling must survive with it; the old code blanked
    # Tracking Status Detail in the same assignment that overwrote the status.
    assert "Pre-window" in records["EXCLUDED-1"]["Tracking Status Detail"]
    assert records["PLAIN-1"]["Tracking Status"] == "currently_flagged"


def test_a_machine_verdict_does_not_outrank_presence(workdir, write_csv):
    """Only humans outrank presence. Automation applies once an award has left
    the snapshot, which is what stops it pruning a live detection."""
    import search

    write_csv(
        "consolidated/nasa_x_2026-07-31.csv",
        search.SNAPSHOT_COLUMNS,
        [snapshot_row("LISTED-1")],
    )
    write_csv(
        bml.AUTO_VERIFICATION_PATH,
        reverify_awards.AUTO_COLUMNS,
        [auto_row("LISTED-1", "continued")],
    )

    bml.build()

    assert built_ledger()["LISTED-1"]["Tracking Status"] == "currently_flagged"
