"""Decision rules for reverify_awards.classify_transactions.

The acceptance gate at the bottom is the important test: the classifier is
rebuilding, from transaction data alone, verdicts a person already reached by
hand. It must reproduce most of them and must never contradict one at high
confidence.
"""

import csv
import os
import sys
from datetime import date

import pytest

import reverify_awards
from tests.helpers import FakeTxn
from reverify_awards import classify_transactions, generated_id, PAGE_SIZE

LEDGER_ROW = {"End Date": "2030-01-01"}


def classify(txns, is_contract=True, ledger_row=None):
    return classify_transactions(
        txns, is_contract=is_contract, ledger_row=ledger_row or LEDGER_ROW
    )


# --- failure and truncation ------------------------------------------------


def test_empty_history_is_unresolved_never_a_verdict():
    """Empty must never read as 'no termination happened'.

    This is the FPDS fail-open shape: an empty result that looks identical to
    a genuine negative is what silently dropped 21 awards.
    """
    v = classify([])
    assert v.status == "unresolved"
    assert v.confidence == "none"


def test_generated_id_canonicalizes_a_legacy_nasa_assistance_url():
    assert (
        generated_id(
            {"URL": "https://www.usaspending.gov/award/ASST_NON_80NSSC22M0122_8000/"}
        )
        == "ASST_NON_80NSSC22M0122_080"
    )


def test_full_first_page_is_not_mistaken_for_truncated_history():
    txns = [
        FakeTxn(f"2025-01-{i % 28 + 1:02d}", action_type="A") for i in range(PAGE_SIZE)
    ]
    assert classify(txns).status == "no_termination_signal"


def test_fetch_transactions_disables_the_orm_default_result_cap():
    class Query:
        def __init__(self):
            self.explicit_limit = None

        def award_id(self, _award_id):
            return self

        def order_by(self, _field, _direction):
            return self

        def page_size(self, size):
            assert size == PAGE_SIZE
            return self

        def limit(self, size):
            self.explicit_limit = size
            return self

        def all(self):
            return []

    query = Query()
    client = type("Client", (), {"transactions": query})()

    assert reverify_awards.fetch_transactions(client, "CONT_AWD_1") == []
    assert query.explicit_limit == sys.maxsize


# --- transaction-derived amount baseline ----------------------------------


def test_transaction_baseline_is_the_maximum_cumulative_obligation():
    txns = [
        FakeTxn("2024-01-01", federal_action_obligation=28832.0),
        FakeTxn("2025-05-01", federal_action_obligation=-28832.0),
    ]

    assert reverify_awards.transaction_baseline_amount(txns) == 28832.0


def test_same_day_numeric_modifications_use_natural_sequence_for_baseline():
    txns = [
        FakeTxn(
            "2024-01-01",
            modification_number="2",
            federal_action_obligation=100.0,
        ),
        FakeTxn(
            "2024-01-01",
            modification_number="10",
            federal_action_obligation=-100.0,
        ),
    ]

    assert reverify_awards.transaction_baseline_amount(txns) == 100.0


def test_same_day_numeric_modifications_use_natural_sequence_for_verdict():
    txns = [
        FakeTxn(
            "2025-01-01",
            modification_number="2",
            action_type="F",
            federal_action_obligation=-100.0,
        ),
        FakeTxn(
            "2025-01-01",
            modification_number="10",
            action_type="C",
            federal_action_obligation=100.0,
        ),
    ]

    assert classify(txns).status == "continued"


def test_transaction_baseline_requires_a_complete_numeric_history():
    assert reverify_awards.transaction_baseline_amount([]) is None
    assert (
        reverify_awards.transaction_baseline_amount(
            [
                FakeTxn("2024-01-01", federal_action_obligation=100.0),
                FakeTxn("2025-01-01", federal_action_obligation=None),
            ]
        )
        is None
    )


def test_zero_first_amount_is_selected_once_for_baseline_backfill():
    ledger = {
        "80NSSC25FA315": {
            "First Award Amount": "0",
            "Status": "listed",
            "Claiming Source": "",
        }
    }
    selected, tiers = reverify_awards.select_awards(
        ledger, {}, stale_days=30, include_excluded=False
    )

    assert selected == ["80NSSC25FA315"]
    assert tiers == {"0_baseline_backfill": 1}


def test_excluded_award_is_still_selected_for_baseline_backfill():
    aid = "80LARC19F0127"
    ledger = {
        aid: {
            "First Award Amount": "0.00",
            "Status": "excluded_by_design",
            "Claiming Source": "",
        }
    }

    selected, tiers = reverify_awards.select_awards(
        ledger, {}, stale_days=30, include_excluded=False
    )

    assert selected == [aid]
    assert tiers == {"0_baseline_backfill": 1}


def test_blank_baseline_from_a_bounded_migration_remains_selected():
    aid = "UNPROCESSED-1"
    ledger = {
        aid: {
            "First Award Amount": "0",
            "Status": "listed",
            "Claiming Source": "",
        }
    }
    previous = {
        aid: {
            "Transaction Baseline Amount": "",
            "Last Success Date": date.today().isoformat(),
        }
    }

    selected, tiers = reverify_awards.select_awards(
        ledger, previous, stale_days=30, include_excluded=False
    )

    assert selected == [aid]
    assert tiers == {"0_baseline_backfill": 1}


def test_computed_zero_baseline_is_not_selected_again_while_fresh():
    aid = "ZERO-1"
    ledger = {
        aid: {
            "First Award Amount": "0",
            "Status": "listed",
            "Claiming Source": "",
        }
    }
    previous = {
        aid: {
            "Transaction Baseline Amount": "0.00",
            "Last Success Date": date.today().isoformat(),
        }
    }

    selected, tiers = reverify_awards.select_awards(
        ledger, previous, stale_days=30, include_excluded=False
    )

    assert selected == []
    assert tiers == {"skipped_fresh": 1}


def test_attempted_unknown_baseline_is_not_selected_again_while_fresh():
    aid = "UNKNOWN-1"
    ledger = {
        aid: {
            "First Award Amount": "0",
            "Status": "listed",
            "Claiming Source": "",
        }
    }
    previous = {
        aid: {
            "Transaction Baseline Amount": "unknown",
            "Last Success Date": date.today().isoformat(),
        }
    }

    selected, tiers = reverify_awards.select_awards(
        ledger, previous, stale_days=30, include_excluded=False
    )

    assert selected == []
    assert tiers == {"skipped_fresh": 1}


def test_successful_reverification_stores_the_transaction_baseline():
    aid = "80NSSC24PC475"
    rec = {
        "URL": (
            "https://www.usaspending.gov/award/"
            "CONT_AWD_80NSSC24PC475_8000_80NSSC24AA005_8000/"
        )
    }
    txns = [
        FakeTxn("2024-01-01", federal_action_obligation=44325.0),
        FakeTxn("2024-02-01", federal_action_obligation=0.0),
        FakeTxn("2025-01-01", federal_action_obligation=-44325.0),
    ]
    verdict = reverify_awards.Verdict("still_terminated", "low", "transaction fixture")

    row = reverify_awards.build_row(
        aid,
        rec,
        verdict,
        txns,
        {},
        {},
        today="2026-07-30",
        ok=True,
    )

    assert row["Transaction Baseline Amount"] == "44325.00"


def test_successful_incomplete_history_stores_an_unknown_baseline_sentinel():
    aid = "UNKNOWN-1"
    verdict = reverify_awards.Verdict(
        "needs_manual_review", "none", "transaction fixture"
    )
    row = reverify_awards.build_row(
        aid,
        {"URL": f"https://www.usaspending.gov/award/CONT_AWD_{aid}/"},
        verdict,
        [FakeTxn("2025-01-01", federal_action_obligation=None)],
        {},
        {},
        today="2026-07-30",
        ok=True,
    )

    assert row["Transaction Baseline Amount"] == "unknown"


# --- termination for cause -------------------------------------------------


@pytest.mark.parametrize("code", ["E", "X"])
def test_cause_codes_excluded_by_design(code):
    v = classify(
        [
            FakeTxn("2025-01-01", "P00000", "A", "base award", 100.0),
            FakeTxn("2025-06-01", "P00001", code, "terminated", -50.0),
        ]
    )
    assert v.status == "excluded_by_design"
    assert v.confidence == "high"


def test_cause_text_excluded_even_without_code():
    v = classify(
        [
            FakeTxn(
                "2025-06-01",
                "P00001",
                "F",
                "Termination for cause of contractor",
                -50.0,
            ),
        ]
    )
    assert v.status == "excluded_by_design"


# --- the headline case: closeout supersedes the termination ---------------


def test_closeout_after_termination_is_still_terminated():
    """80NSSC24K1264's shape: the exact blind spot this pass exists to close."""
    v = classify(
        [
            FakeTxn("2025-01-01", "P00000", "A", "base award", 500000.0),
            FakeTxn(
                "2025-08-20", "P00002", "D", "Terminate for convenience", -13540.36
            ),
            FakeTxn(
                "2026-03-11",
                "P00003",
                "D",
                "Adjustment to Completed Project",
                -177900.37,
            ),
        ],
        is_contract=False,  # assistance: D is a closeout
    )
    assert v.status == "closed_out"
    assert v.confidence == "high"
    assert "remains terminated" in v.evidence.lower()


def test_contract_D_is_a_change_order_not_a_closeout():
    """The map must be chosen by award type: contract D != assistance D.

    Reading the wrong map flips still_terminated <-> closed_out.
    """
    txns = [
        FakeTxn("2025-08-20", "P00002", "F", "Terminate for convenience", -1000.0),
        FakeTxn("2026-03-11", "P00003", "D", "Adjustment to Completed Project", -25.0),
    ]
    assert classify(txns, is_contract=True).status != "closed_out"
    assert classify(txns, is_contract=False).status == "closed_out"


def test_contract_closeout_code_K():
    v = classify(
        [
            FakeTxn("2025-08-20", "P00002", "F", "Terminate for convenience", -1000.0),
            FakeTxn("2026-03-11", "P00003", "K", "Close out", -25.0),
        ]
    )
    assert v.status == "closed_out"


# --- reversals -------------------------------------------------------------


def test_rescission_after_termination_is_reinstated():
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Stop work order issued", -100.0),
            FakeTxn("2025-07-03", "P00002", "A", "Rescinding stop work notice", 0.0),
        ]
    )
    assert v.status == "reinstated"
    assert v.confidence == "high"


def test_vacatur_outranks_later_money():
    """A court order is a legal fact; later obligations don't undo it."""
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -100.0),
            FakeTxn(
                "2025-09-03",
                "P00002",
                "A",
                "The termination has been vacated and set aside pursuant to the order",
                5000.0,
            ),
        ]
    )
    assert v.status == "vacated"


def test_bare_cancel_is_not_a_reversal():
    """ "contract cancellation" must not read as rescinding a stop-work."""
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Stop work order issued", -100.0),
            FakeTxn(
                "2025-07-03", "P00002", "M", "contract cancellation processed", 0.0
            ),
        ]
    )
    assert v.status != "reinstated"


def test_reversal_word_needs_a_termination_subject():
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -100.0),
            FakeTxn(
                "2025-07-03", "P00002", "M", "rescinding the travel allowance", 0.0
            ),
        ]
    )
    assert v.status != "reinstated"


# --- descope vs continued --------------------------------------------------


def test_descope_beats_continued_money():
    """NNG09FA40C: DEI work de-scoped, contract kept taking $4M+."""
    v = classify(
        [
            FakeTxn(
                "2025-04-01",
                "P00001",
                "F",
                "Stop work with intent to de-scope DEI activities",
                -63547.48,
            ),
            FakeTxn("2026-06-23", "P00002", "C", "funding action", 4000000.0),
        ]
    )
    assert v.status == "descoped"


def test_new_obligations_after_termination_is_continued():
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Stop work order", -100.0),
            FakeTxn("2025-08-26", "P00005", "C", "funding action", 126413.0),
        ]
    )
    assert v.status == "continued"


def test_settlement_netting_zero_is_not_continued():
    """A -$100/+$100 settlement pair is not a continuation of substance."""
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -100.0),
            FakeTxn("2025-05-01", "P00002", "K", "termination settlement", 100.0),
            FakeTxn("2025-06-01", "P00003", "K", "close out", -100.0),
        ]
    )
    assert v.status != "continued"


def test_positive_money_on_a_closeout_is_not_continued():
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -1000.0),
            FakeTxn("2025-06-01", "P00002", "K", "close out adjustment", 250.0),
        ]
    )
    assert v.status == "closed_out"


# --- absence of evidence ---------------------------------------------------


def test_still_terminated_is_low_confidence():
    """Resting on absence of contrary evidence, so it may never set a Status."""
    v = classify(
        [
            FakeTxn(
                "2025-04-08", "P00002", "F", "Stop-work with intent to T4C", -1000.0
            ),
        ]
    )
    assert v.status == "still_terminated"
    assert v.confidence == "low"


def test_no_termination_and_past_end_date_is_naturally_expired():
    v = classify(
        [FakeTxn("2025-01-01", "P00000", "A", "base award", 100.0)],
        ledger_row={"End Date": "2025-06-30", "Sources": "DOGE"},
    )
    assert v.status == "naturally_expired"
    assert v.confidence == "low"


def test_no_termination_and_future_end_date():
    v = classify([FakeTxn("2025-01-01", "P00000", "A", "base award", 100.0)])
    assert v.status == "no_termination_signal"


def test_unknown_action_code_never_defaults():
    v = classify([FakeTxn("2025-01-01", "P00000", "Z", "mystery action", 100.0)])
    assert v.status == "needs_manual_review"


def test_unknown_obligation_is_not_treated_as_zero():
    v = classify(
        [
            FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -100.0),
            FakeTxn("2025-06-01", "P00002", "A", "later action", None),
        ]
    )
    assert v.status == "needs_manual_review"


def test_anchor_is_the_latest_termination():
    """Re-terminated after a rescission: the reversal must not win."""
    v = classify(
        [
            FakeTxn("2025-01-01", "P00001", "F", "Stop work order", -10.0),
            FakeTxn("2025-02-01", "P00002", "A", "Rescinding stop work notice", 10.0),
            FakeTxn("2025-03-01", "P00003", "F", "Terminate for convenience", -50.0),
        ]
    )
    assert v.status == "still_terminated"


def test_verdicts_are_deterministic():
    txns = [
        FakeTxn("2025-04-01", "P00001", "F", "Terminate for convenience", -100.0),
        FakeTxn("2025-06-01", "P00002", "K", "close out", -5.0),
    ]
    a, b = classify(list(txns)), classify(list(reversed(txns)))
    assert (a.status, a.evidence, a.signals) == (b.status, b.evidence, b.signals)


# --- acceptance gate against the hand-curated verdicts ---------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUMAN_PATH = os.path.join(REPO, "verification", "dropped_award_status.csv")

# Transaction histories reconstructed from the Evidence text a person wrote in
# verification/dropped_award_status.csv. Each is the machine-readable form of
# the same facts that person cited.
FIXTURES = {
    "80HQTR24F0072": (
        True,
        [
            FakeTxn(
                "2025-04-05", "P00001", "F", "Stop-work with intent to T4C", -1000.0
            ),
            FakeTxn("2025-08-20", "P00002", "K", "settlement deobligation", -13540.36),
        ],
    ),
    "80NSSC23K1348": (
        False,
        [
            FakeTxn("2025-04-08", "P00002", "D", "Stop-work/intent to T4C", -500.0),
        ],
    ),
    "80HQTR22F0076": (
        True,
        [
            FakeTxn("2026-03-18", "P00010", "F", "Terminate for Convenience", -2000.0),
        ],
    ),
    "80HQTR22F0109": (
        True,
        [
            FakeTxn("2026-03-18", "P00005", "F", "Terminate for Convenience", -1500.0),
        ],
    ),
    "80HQTR24F0019": (
        True,
        [
            FakeTxn("2026-01-10", "P00002", "F", "stop-work/T4C notice", -900.0),
            FakeTxn(
                "2026-04-09",
                "P00003",
                "K",
                "Termination settlement supplemental agreement",
                -300.0,
            ),
        ],
    ),
    "80HQTR24F0037": (
        True,
        [
            FakeTxn("2026-04-14", "P00003", "F", "Terminate for Convenience", -700.0),
        ],
    ),
    "80NSSC21K1069": (
        False,
        [
            FakeTxn(
                "2025-07-07",
                "P00004",
                "D",
                "Termination for convenience agreement",
                -400.0,
            ),
        ],
    ),
    "80KSC020C0012": (
        True,
        [
            FakeTxn(
                "2026-03-31", "P00031", "F", "stop work; deobligation", -35500000.0
            ),
            FakeTxn(
                "2026-06-15",
                "P00032",
                "M",
                "stop-work extended through 2026-09-27",
                0.0,
            ),
        ],
    ),
    "80NSSC24K1264": (
        False,
        [
            FakeTxn("2025-06-01", "P00002", "D", "Terminated for convenience", -1000.0),
            FakeTxn(
                "2026-03-11",
                "P00003",
                "D",
                "Adjustment to Completed Project",
                -177900.37,
            ),
        ],
    ),
    "80NSSC24K0913": (
        False,
        [
            FakeTxn("2025-06-01", "P00002", "D", "Terminated for convenience", -1000.0),
            FakeTxn(
                "2026-04-30", "P00003", "D", "Adjustment to Completed Project", -222.20
            ),
        ],
    ),
    "80NSSC22K1191": (
        False,
        [
            FakeTxn("2025-09-01", "P00003", "D", "Stop work order", -100.0),
            FakeTxn(
                "2026-07-13",
                "P00004",
                "D",
                "Adjustment to Completed Project",
                -17980.80,
            ),
        ],
    ),
    "80GRC024CA008": (
        False,
        [
            FakeTxn(
                "2025-04-11", "P00003", "C", "Stop-work with intent to de-scope", -100.0
            ),
        ],
    ),
    "NNG09FA40C": (
        True,
        [
            FakeTxn(
                "2025-03-01", "P00010", "F", "de-scope DEI work per EO 14148", -50000.0
            ),
            FakeTxn("2026-06-23", "P00011", "C", "funding action", 4000000.0),
        ],
    ),
    "80MSFC20C0050": (
        True,
        [
            FakeTxn(
                "2025-04-28",
                "P00012",
                "F",
                "Stop-work with intent to de-scope SERVIR",
                -63547.48,
            ),
        ],
    ),
    "80NSSC22M0122": (
        False,
        [
            FakeTxn("2025-03-01", "P00004", "C", "Stop work order", -10.0),
            FakeTxn("2025-05-01", "P00005", "C", "funding action", 833342.0),
        ],
    ),
    "80NSSC24M0190": (
        False,
        [
            FakeTxn("2026-01-01", "P00002", "C", "Stop work order", -10.0),
            FakeTxn("2026-04-08", "P00003", "C", "funding action", 607000.0),
        ],
    ),
    "80NSSC24K0840": (
        False,
        [
            FakeTxn("2025-06-01", "P00000", "C", "Stop work order", -10.0),
            FakeTxn("2025-09-12", "P00001", "C", "funding action", 153498.0),
        ],
    ),
    "80NSSC23K0309": (
        False,
        [
            FakeTxn("2025-06-01", "P00004", "C", "Stop work order", -10.0),
            FakeTxn("2025-08-26", "P00005", "C", "funding action", 126413.0),
        ],
    ),
    "80NSSC24K0966": (
        False,
        [
            FakeTxn("2024-05-01", "P00000", "A", "original obligation", 250000.0),
        ],
    ),
}


def _human_verdicts():
    with open(HUMAN_PATH, encoding="utf-8") as fh:
        return {r["Award ID"]: r["Status"] for r in csv.DictReader(fh)}


def test_fixtures_name_only_real_curated_awards():
    """Subset, not equality: `dropped_award_status.csv` is human-owned and
    grows as awards are adjudicated. Adding a row there is a data act and must
    not break CI — but a fixture for an award nobody curated is a stale test.
    """
    human = _human_verdicts()
    assert set(FIXTURES) <= set(human), (
        f"fixtures name awards absent from the curated file: "
        f"{sorted(set(FIXTURES) - set(human))}"
    )
    uncovered = sorted(set(human) - set(FIXTURES))
    if uncovered:
        print(f"\nNOTE: {len(uncovered)} curated awards have no fixture: {uncovered}")


def test_no_high_confidence_verdict_contradicts_a_human_one():
    """The hard gate. A confident machine call that disagrees with an
    evidence-backed human call means the RULES are wrong, not the person."""
    human = _human_verdicts()
    contradictions = []
    for aid, (is_contract, txns) in FIXTURES.items():
        v = classify(txns, is_contract=is_contract)
        if v.confidence == "high" and v.status != human[aid]:
            contradictions.append((aid, human[aid], v.status, v.evidence))
    assert not contradictions, "high-confidence contradictions:\n" + "\n".join(
        f"  {a}: human={h} auto={s} :: {e}" for a, h, s, e in contradictions
    )


# Awards the classifier is known NOT to reproduce, pinned by identity so a
# *different* one failing is caught. 80NSSC24K0966 has only its original
# obligation on record, so the machine reads "no termination signal" where a
# person wrote "needs manual review" — the same conclusion, and low confidence
# either way, so it can never set a Status.
EXPECTED_DISAGREEMENTS = {"80NSSC24K0966"}


def test_reproduces_human_verdicts_except_known_misses():
    human = _human_verdicts()
    missed = {
        aid
        for aid, (is_contract, txns) in FIXTURES.items()
        if classify(txns, is_contract=is_contract).status != human[aid]
    }
    assert missed == EXPECTED_DISAGREEMENTS, (
        f"classifier/human agreement changed: newly missed "
        f"{sorted(missed - EXPECTED_DISAGREEMENTS)}, newly fixed "
        f"{sorted(EXPECTED_DISAGREEMENTS - missed)}"
    )
