"""build_master_ledger.classify() — inferring a status for a dropped award.

This function writes the Status column of the published ledger and runs on the
daily path, but had no test coverage until the vocabulary was shared. These
tests pin each branch, and the two consistency tests at the bottom are what
keep classify() and reverify_awards from drifting apart again.
"""

import pytest

import build_master_ledger as bml
import reverify_awards as rv
from tests.helpers import FakeTxn

REC = {
    "First Flagged Date": "2025-04-05",
    "Last Flagged Date": "2026-07-29",
    "Flagged By": "NPDV",
}


def classify(descs, **rec_overrides):
    rec = {**REC, **rec_overrides}
    return bml.classify("A-1", rec, {"A-1": descs})


# --- text branches ---------------------------------------------------------


def test_vacatur_from_real_court_language():
    """80NSSC21K1443 / 80NSSC19K0326: the Harvard vacatur."""
    status, detail = classify(
        [
            "The termination has been vacated and set aside pursuant to the order "
            "entered on september 3 2025 in president and fellows of harvard college"
        ]
    )
    assert status == "vacated"
    assert "vacated" in detail.lower()


def test_rescission_is_reinstated():
    status, _ = classify(["Rescinding stop work notice. taurus balloon polarimeter"])
    assert status == "reinstated"


def test_rescission_spelling_now_matched():
    """Missed by the old bare "rescind" substring."""
    status, _ = classify(["rescission of the stop work order"])
    assert status == "reinstated"


def test_vacatur_spelling_now_matched():
    """Missed by the old bare "vacated" substring."""
    status, _ = classify(["vacatur of the termination"])
    assert status == "vacated"


def test_rescind_without_a_termination_subject_is_not_reinstated():
    """THE regression this change exists to prevent.

    The old bare `"rescind" in all_desc` test read this as a reinstatement and
    would have removed a live award from the cancellation count.
    """
    status, _ = classify(["Rescind the small business set-aside designation"])
    assert status != "reinstated"


def test_set_aside_alone_is_not_a_vacatur():
    """The old test matched `"set aside"` anywhere in the joined history."""
    status, _ = classify(["Award reserved as a 100% small business set aside"])
    assert status != "vacated"


@pytest.mark.parametrize(
    "text",
    [
        "Termination for cause of contractor",
        "terminated for cause",
        "termination for default",
    ],
)
def test_termination_for_cause_excluded(text):
    """The latter two were missed by the old exact substring."""
    status, detail = classify([text])
    assert status == "excluded_by_design"
    assert "cause" in detail.lower()


def test_guard_does_not_span_two_descriptions():
    """Pins the per-description semantics.

    Testing the joined history would pair the reversal word in one entry with
    the termination subject in an unrelated one, producing a false reinstatement.
    """
    status, _ = classify(
        [
            "Stop work order issued",
            "Rescind the small business set-aside designation",
        ]
    )
    assert status != "reinstated"


# --- non-text branches (unchanged, pinned so the refactor can't disturb them)


def test_classify_no_longer_special_cases_the_grants_experiment():
    """Those rows are dropped at ingest now, so classify() never sees them.

    The exclusion is covered by test_claim_columns; this pins that the dead
    branch stayed removed rather than being reinstated alongside it.
    """
    status, _ = classify(
        ["ordinary description"],
        **{
            "First Flagged Date": "2026-01-08",
            "Last Flagged Date": "2026-01-08",
            "Flagged By": "NASAGrants",
        },
    )
    assert status == "dropped_pending_review"


def test_fpds_retirement():
    status, detail = classify(
        ["ordinary description"],
        **{"Last Flagged Date": bml.FPDS_LAST_GOOD_DATE, "Flagged By": "NPDV; FPDS"},
    )
    assert status == "source_retired"
    assert "fpds" in detail.lower()


def test_fpds_source_but_a_later_last_seen_is_not_source_retired():
    status, _ = classify(
        ["ordinary description"],
        **{"Last Flagged Date": "2026-06-16", "Flagged By": "NPDV; FPDS"},
    )
    assert status == "dropped_pending_review"


def test_fallback_is_pending_review():
    status, detail = classify(["routine administrative modification"])
    assert status == "dropped_pending_review"
    assert "verify" in detail.lower()


def test_no_description_history_at_all():
    status, _ = bml.classify("A-1", dict(REC), {})
    assert status == "dropped_pending_review"


# --- the point of the whole change: the two modules must agree -------------

# (text, expected shared reading). Each is judged by classify() over a
# description history and by reverify_awards over a transaction carrying the
# same text, and the two must reach the same conclusion.
SHARED_CASES = [
    ("The termination has been vacated and set aside pursuant to the order", "vacated"),
    ("Rescinding stop work notice", "reinstated"),
    ("Rescind the small business set-aside designation", None),
    ("Termination for cause of contractor", "excluded_by_design"),
    ("terminated for cause", "excluded_by_design"),
    ("routine administrative modification", None),
]


@pytest.mark.parametrize("text,expected", SHARED_CASES)
def test_ledger_and_reverify_read_the_same_text_the_same_way(text, expected):
    ledger_status, _ = classify([text])

    # Give reverify a prior termination so the post-termination branches are
    # reachable, then the same text as a later transaction.
    verdict = rv.classify_transactions(
        [
            FakeTxn("2025-01-01", "P00001", "F", "Terminate for convenience", -100.0),
            FakeTxn("2025-06-01", "P00002", "A", text, 0.0),
        ],
        is_contract=True,
        ledger_row={"Current End Date": "2030-01-01"},
    )

    if expected is None:
        assert ledger_status not in ("vacated", "reinstated", "excluded_by_design")
        assert verdict.status not in ("vacated", "reinstated", "excluded_by_design")
    else:
        assert ledger_status == expected
        assert verdict.status == expected


def test_both_modules_use_the_same_predicate_objects():
    """Guards against one module quietly reintroducing a local copy."""
    import termination_vocabulary as tv

    assert rv.is_cause is tv.is_cause
    assert rv.is_termination is tv.is_termination
    assert rv.is_descope is tv.is_descope
    assert rv.CLOSEOUT_TEXT is tv.CLOSEOUT_TEXT
    assert rv.is_vacatur is tv.is_vacatur
    assert rv.is_reversal is tv.is_reversal
    assert bml.is_reversal is tv.is_reversal
    assert bml.is_cause is tv.is_cause
    assert bml.is_vacatur is tv.is_vacatur
