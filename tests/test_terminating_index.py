"""The two published answers to "which modification terminated this award".

Both are derived from the same transaction history and both reach the master
ledger, so they need one owner and an asserted relationship. They used to be
two near-identical scans in two modules; on live data they disagreed on 94 of
360 awards, and 60 rows published `still_terminated` beside a blank
termination modification because the ledger carried only the code-only answer.
"""

import award_transaction_facts as atf
import build_master_ledger as bml
import csv_aliases
import reverify_awards
from award_transaction_facts import terminating_index, transaction_history_facts
from tests.helpers import FakeTxn

# The shape behind the 79 real awards the code-only rule leaves blank: NASA
# recorded the termination under M, "other administrative action".
GENERIC_CODE_TERMINATION = [
    FakeTxn("2024-01-01", "0", "A"),
    FakeTxn("2025-04-08", "P00005", "M", "TERMINATE FOR CONVENIENCE - STOP ALL WORK"),
]


def index_pair(txns, is_contract=True):
    """(action-code answer, verified answer) for one already-ordered history."""
    return tuple(
        terminating_index(
            txns, is_contract=is_contract, accept_text_evidence=accept_text
        )
        for accept_text in (False, True)
    )


def test_a_termination_under_a_generic_action_code_is_text_only():
    """The exact divergence the ledger was publishing as a blank."""
    coded, verified = index_pair(GENERIC_CODE_TERMINATION)

    assert coded is None
    assert GENERIC_CODE_TERMINATION[verified].modification_number == "P00005"


def test_the_sidecar_leaves_that_award_blank_and_says_so_in_its_column_name():
    facts = transaction_history_facts(GENERIC_CODE_TERMINATION, is_contract=True)

    assert facts.termination is None
    assert atf.history_columns(facts)["Action Code Termination Modification"] == ""


def test_the_verified_rule_never_precedes_the_action_code_rule():
    """Both take the last match, and the verified rule accepts a superset of
    the non-rescinded F transactions - so it can only ever anchor later."""
    txns = [
        FakeTxn("2024-01-01", "0", "A"),
        FakeTxn("2025-02-01", "P00001", "F", "TERMINATION FOR CONVENIENCE"),
        FakeTxn("2026-03-17", "P00002", "M", "STOP WORK ORDER ISSUED"),
    ]

    coded, verified = index_pair(txns)

    assert txns[coded].modification_number == "P00001"
    assert txns[verified].modification_number == "P00002"
    assert verified >= coded


def test_a_rescinded_termination_anchors_only_the_action_code_rule():
    """Skipping reversals is load-bearing: a rescission names the thing it
    undoes, so it reads as a termination unless it is stepped over."""
    txns = [
        FakeTxn("2024-01-01", "0", "A"),
        FakeTxn("2025-02-01", "P00001", "F", "TERMINATION FOR CONVENIENCE"),
        FakeTxn("2025-06-01", "P00002", "M", "RESCINDING STOP WORK NOTICE"),
    ]

    coded, verified = index_pair(txns)

    assert txns[coded].modification_number == "P00001"
    assert txns[verified].modification_number == "P00001"


def test_termination_for_cause_anchors_only_the_action_code_rule():
    """The other direction, and why neither rule is a superset of the other.

    reverify_awards excludes cause by methodology before it asks, so the
    verified answer stays blank while the formal record still names the mod.
    """
    txns = [FakeTxn("2025-02-01", "P00001", "E", "TERMINATE FOR DEFAULT")]

    coded, verified = index_pair(txns)

    assert txns[coded].modification_number == "P00001"
    assert verified is None


def test_the_two_rules_read_assistance_codes_with_the_assistance_vocabulary():
    """D is a contract funding action and an assistance closeout; neither is a
    termination, so a text-only match must not be attributed to the code."""
    txns = [FakeTxn("2025-02-01", "P00001", "D", "STOP WORK")]

    for is_contract in (True, False):
        coded, verified = index_pair(txns, is_contract=is_contract)
        assert coded is None
        assert verified == 0


def test_the_classifier_anchors_on_the_shared_rule():
    """reverify's verdict and its published mod come from terminating_index,
    not from a second copy of the scan."""
    verdict = reverify_awards.classify_transactions(
        GENERIC_CODE_TERMINATION,
        is_contract=True,
        ledger_row={"Current End Date": "2030-01-01"},
    )

    assert verdict.status == "still_terminated"
    assert verdict.signals["term_mod"] == "P00005"


def test_every_overlaid_column_is_one_the_producer_actually_writes():
    """The overlay copies by name out of auto_verification.csv. A name in the
    overlay that reverify_awards does not write is not an error - it silently
    fills the public ledger column with blanks."""
    assert set(bml.AUTO_OVERLAY_COLUMNS) <= set(reverify_awards.AUTO_COLUMNS)


def test_both_answers_are_published_and_neither_name_is_unqualified():
    """The ledger must carry both, named for the rule that produced them."""
    assert set(bml.VERIFIED_TERMINATION_COLUMNS) <= set(bml.AUTO_OVERLAY_COLUMNS)
    for column in (
        "Verified Termination Modification",
        "Verified Termination Date",
        "Action Code Termination Modification",
        "Action Code Termination Date",
    ):
        assert column in bml.LEDGER_COLUMNS

    # Nothing published may be called simply "the" termination modification.
    assert "Termination Modification Number" not in bml.LEDGER_COLUMNS
    assert "Termination Action Date" not in bml.LEDGER_COLUMNS


def test_the_verified_pair_is_not_expected_from_a_daily_snapshot():
    """It is an auto-verification overlay; a snapshot has no verdict yet, and
    listing it in REFRESHED_COLUMNS would let a blank snapshot erase it."""
    import search

    assert "Verified Termination Modification" not in search.SNAPSHOT_COLUMNS
    assert "Verified Termination Modification" not in bml.REFRESHED_COLUMNS


def test_archived_files_still_resolve_the_old_termination_names():
    for table in (
        csv_aliases.SNAPSHOT,
        csv_aliases.LEDGER,
        csv_aliases.TRANSACTION_FACTS,
    ):
        assert (
            table["Termination Modification Number"]
            == "Action Code Termination Modification"
        )
    assert (
        csv_aliases.AUTO_VERDICTS["Termination Mod"]
        == "Verified Termination Modification"
    )
