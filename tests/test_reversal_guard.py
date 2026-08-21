"""A reversal word only reverses when a termination subject sits beside it."""

import pytest

from nasatrack.criteria import is_reversal, is_vacatur

REVERSALS = [
    "rescission of the stop work order",
    "rescind the termination for convenience",
    "reinstatement of the terminated contract",
    "resumption of work following the stop-work order",
    "lift the suspension and reinstate the contract",
    # The one cancel-shaped reversal: guarded by the "of ... <subject>"
    # construction (within two words), so it cannot reach "contract
    # cancellation". Observed on 80JSC024F0024/80JSC024F0026.
    "cancellation of the stop-work order",
    "Cancellation of partial stop work notice as non-applicable",
]

# A reversal word with no termination subject, and a termination subject with
# no reversal word. Bare "cancel" (without the guarded "of ..." construction)
# is deliberately absent from the reversal vocabulary: "contract cancellation"
# IS a termination.
NON_REVERSALS = [
    "rescind the small business set-aside designation",
    "rescission of the prior invoice",
    "cancellation of the award",
    "legal contract cancellation",
    "terminate for convenience",
    "",
]


@pytest.mark.parametrize("text", REVERSALS)
def test_reversal_language(text):
    assert is_reversal(text)


@pytest.mark.parametrize("text", NON_REVERSALS)
def test_reversal_needs_a_termination_subject(text):
    assert not is_reversal(text)


@pytest.mark.parametrize(
    "text",
    [
        "the court vacated the termination",
        "VACATUR ORDER ENTERED",
        "termination set aside by the board",
    ],
)
def test_vacatur_language(text):
    assert is_vacatur(text)


@pytest.mark.parametrize(
    "text",
    [
        "100% small business set aside",
        "total small business set aside award",
        "",
    ],
)
def test_set_aside_alone_is_a_procurement_category(text):
    assert not is_vacatur(text)
