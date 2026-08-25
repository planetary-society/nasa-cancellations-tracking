"""De-scoped awards leave terminations.csv and publish to descoped.csv instead.

The descriptions here are the field-observed strings the vocabulary was built
from, not invented prose: a de-scope arrives dressed as a stop-work notice, and
an F-coded settlement can say "partial termination" while still being the
reported termination act.
"""

import pytest

from nasatrack.terminations import is_descoped, partition_descoped
from tests.test_merge import row

DESCOPE_NOTICE = "STOP WORK NOTICE ISSUED WITH NOTIFICATION OF INTENT TO DESCOPE"
PARTIAL_SETTLEMENT = "FINALIZE THE PARTIAL TERMINATION SETTLEMENT"


def descope_row(award_key="CONT_AWD_DESCOPE", **overrides):
    """An un-coded contract row whose description is a de-scope notice."""
    values = {
        "source": "api",
        "action_type": "M",
        "transaction_description": DESCOPE_NOTICE,
        **overrides,
    }
    return row(award_key, **values)


def test_uncoded_descope_language_moves_to_descoped():
    published = descope_row()
    assert is_descoped(published)


def test_a_standalone_f_code_beats_descope_language():
    # "F" is TERMINATE FOR CONVENIENCE (COMPLETE OR PARTIAL): the settlement mod
    # reports the termination act, so the "partial termination" prose does not
    # reclassify it.
    published = row(
        "CONT_AWD_F", source="api", action_type="F", transaction_description=PARTIAL_SETTLEMENT
    )
    assert not is_descoped(published)


def test_a_descoped_override_moves_even_an_f_coded_row():
    # NNG09FA40C's shape: the mod carries an F, but a human read the award and
    # saw DEI work pulled off a contract that kept collecting obligations.
    published = row(
        "CONT_AWD_F",
        source="api",
        action_type="F",
        transaction_description=PARTIAL_SETTLEMENT,
        override_status="descoped",
    )
    assert is_descoped(published)


@pytest.mark.parametrize("status", ["termination_confirmed", "closed_out", "needs_manual_review"])
def test_any_other_human_status_pins_the_row_to_terminations(status):
    # A human looked at this award and did not call it a de-scope, so the
    # language classifier never gets a say.
    published = descope_row(override_status=status)
    assert not is_descoped(published)


def test_a_grant_with_descope_prose_moves():
    # FABS has no reason-for-modification field, so there is no code to beat.
    published = descope_row(
        "ASST_NON_80NSSC25K0001", award_type="grant", action_type="", award_id="80NSSC25K0001"
    )
    assert is_descoped(published)


def test_an_ordinary_termination_stays():
    published = row("CONT_AWD_A", source="api")
    assert not is_descoped(published)


def test_both_partitions_keep_the_committed_order():
    rows = [
        row("CONT_AWD_B", source="api", day="2026-01-15"),
        descope_row("CONT_AWD_D1", day="2025-12-01"),
        row("CONT_AWD_A", source="api", day="2025-06-01"),
        descope_row("CONT_AWD_D2", day="2025-03-01"),
        row("CONT_AWD_C", source="api", day="2025-02-01"),
    ]
    kept, descoped = partition_descoped(rows)
    assert [r.award_key for r in kept] == ["CONT_AWD_B", "CONT_AWD_A", "CONT_AWD_C"]
    assert [r.award_key for r in descoped] == ["CONT_AWD_D1", "CONT_AWD_D2"]
