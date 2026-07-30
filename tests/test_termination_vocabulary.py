"""The shared text predicates.

Every module that asks "does this text assert a termination" now answers from
here, so these cases are the whole contract. Each table lists what must match
and — more importantly — what must not.
"""

import pytest

import termination_vocabulary as tv

# --- reversals -------------------------------------------------------------

REVERSAL_YES = [
    "Rescinding stop work notice",
    "Rescind - stop work notice issued with notification of intent to terminate",
    "rescission of the stop work order",
    "Stop-work order rescinded; work resumes",
    "reinstating the terminated award",
    "cancellation of the stop-work order rescinded",
]

REVERSAL_NO = [
    # The guard this predicate exists for: a reversal word with no termination
    # subject. Bare substring matching reads this as a reinstatement.
    "Rescind the small business set-aside designation",
    "rescinding the travel allowance",
    "rescission of the prior invoice",
    # A termination subject with no reversal word.
    "Stop work order issued",
    "Terminate for convenience",
    # "cancel" is deliberately not a reversal word.
    "contract cancellation processed",
    "",
]


@pytest.mark.parametrize("text", REVERSAL_YES)
def test_is_reversal_matches(text):
    assert tv.is_reversal(text)


@pytest.mark.parametrize("text", REVERSAL_NO)
def test_is_reversal_rejects(text):
    assert not tv.is_reversal(text)


def test_reversal_requires_both_halves_in_the_same_text():
    """Callers must test one description at a time.

    build_master_ledger holds a list of descriptions per award; testing a
    concatenation would let these two unrelated entries pair up.
    """
    assert not tv.is_reversal("Rescind the set-aside designation")
    assert not tv.is_reversal("Stop work order issued")
    assert tv.is_reversal("Rescind the set-aside designation || Stop work order issued")


# --- termination for cause -------------------------------------------------

CAUSE_YES = [
    "Termination for cause of contractor",
    "terminated for cause",
    "Terminate for Cause",
    "termination for default",
    "terminating for default",
]

CAUSE_NO = [
    "Terminate for convenience",
    "termination settlement agreement",
    "for cause and effect analysis",
    "",
]


@pytest.mark.parametrize("text", CAUSE_YES)
def test_is_cause_matches(text):
    assert tv.is_cause(text)


@pytest.mark.parametrize("text", CAUSE_NO)
def test_is_cause_rejects(text):
    assert not tv.is_cause(text)


# --- vacatur ---------------------------------------------------------------


def test_is_vacatur_matches_real_court_language():
    assert tv.is_vacatur(
        "The termination has been vacated and set aside pursuant to the order "
        "entered on september 3 2025"
    )
    assert tv.is_vacatur("vacatur of the termination")
    assert tv.is_vacatur("vacating the stop work order")


def test_set_aside_needs_a_termination_subject():
    """A procurement set-aside is not a court vacating a termination."""
    assert not tv.is_vacatur("Award reserved as a 100% small business set aside")
    assert not tv.is_vacatur("Rescind the small business set-aside designation")
    assert tv.is_vacatur("the termination was set aside by the court")


def test_is_vacatur_known_overmatch():
    """`\\bvacat\\w*` still stands alone, so "vacation" matches.

    Pinned so a future narrowing is deliberate. Low blast radius: reverify
    consults vacatur only once a termination anchor exists, and classify() only
    for awards that already left the snapshot.
    """
    assert tv.is_vacatur("annual vacation schedule")


# --- termination detection -------------------------------------------------

TERM_YES = [
    "Terminate for convenience",
    "termination for convenience of the government",
    "terminated for convenience",
    "T4C executed",
    "stop work order",
    "stop-work notice",
    "termination settlement supplemental agreement",
    "notice of termination issued",
]

TERM_NO = [
    # Narrow by design: a bare termination word is not enough. NPDV's broader
    # DEFAULT_SEARCH_PHRASES is the detection net; this is for classification.
    "terminated",
    "termination",
    "modification to reflect termination of the subject order",
    "administrative change order",
    "",
]


@pytest.mark.parametrize("text", TERM_YES)
def test_is_termination_matches(text):
    assert tv.is_termination(text)


@pytest.mark.parametrize("text", TERM_NO)
def test_is_termination_rejects(text):
    assert not tv.is_termination(text)


# --- descope ---------------------------------------------------------------


def test_is_descope():
    assert tv.is_descope("Stop work with intent to de-scope DEI activities")
    assert tv.is_descope("partial termination of CLIN 0002")
    assert tv.is_descope("reduce scope of work")
    assert not tv.is_descope("Terminate for convenience")


# --- None-safety -----------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [tv.is_reversal, tv.is_cause, tv.is_vacatur, tv.is_termination, tv.is_descope],
)
def test_predicates_tolerate_none(predicate):
    assert predicate(None) is False
