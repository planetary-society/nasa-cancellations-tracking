"""Stripping source status preambles from the ledger's Description.

Some sources prepend their own status to the award's description. The ledger
carries that data in real columns now, so the prose is redundant there - but
the patterns must be anchored tightly, because a loose rule silently eats real
content.
"""

import pytest

import build_master_ledger as bml

strip = bml.strip_claim_prefix


# --- the two DOGE preambles -----------------------------------------------


def test_doge_contract_preamble():
    assert (
        strip(
            "Status: TERMINATED. Reported savings: $1,423,496.00. "
            "DOGE Action Date: 4/14/2025. Knowledge management supports"
        )
        == "Knowledge management supports"
    )


def test_doge_grant_preamble_has_no_status_segment():
    assert (
        strip(
            "DOGE Action Date: 3/21/2025. Reported savings: $380,667.00. "
            "Basic research: earth observations"
        )
        == "Basic research: earth observations"
    )


@pytest.mark.parametrize(
    "case_state", ["Approved/ Awarded", "Work In Progress", "Cancelled", "Pending"]
)
def test_nasa_grants_preamble_is_deliberately_kept(case_state):
    """Unlike DOGE's, this preamble is not redundant.

    "<case_state> - <pr_task>." is the *reason* the grant was flagged, and no
    ledger column holds it. Stripping it would delete the only record of why
    the award is tracked, so it stays until that data has somewhere else to
    live.
    """
    text = (
        f"{case_state} - Administrative | Administrative - Decrease, "
        f"Administrative - Change Pop End Date. Modeling of exoplanet atmospheres"
    )
    assert strip(text) == text


def test_a_genuinely_short_description_survives():
    """DOGE really does report descriptions this terse."""
    assert (
        strip(
            "Status: TERMINATED. Reported savings: $0. "
            "DOGE Action Date: 2/18/2025. Scan program"
        )
        == "Scan program"
    )


# --- what must NOT be stripped --------------------------------------------


def test_termination_evidence_is_never_mistaken_for_a_preamble():
    """NNM16AA08C. A loose '<words> - <text>. ' rule strips this to
    'ALL TARGET ENCOUNTERS WILL BE FLY-BY ENCOUNTERS.', destroying the only
    record of why the award is tracked."""
    text = (
        "STOP WORK NOTICE ISSUED WITH NOTIFICATION OF INTENT TO DESCOPE - "
        "LUCY IS A PLANNED NASA SPACE PROBE. ALL TARGET ENCOUNTERS WILL BE "
        "FLY-BY ENCOUNTERS."
    )
    assert strip(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "TERMINATION FOR CONVENIENCE AGREEMENT - AGU, AAS, ASGSR TO NASA: "
        "SUPPORTING EARLY CAREER RESEARCHERS. FURTHER DETAIL.",
        "Reason for modification: terminate for convenience (complete or "
        "partial). general services",
        "INSTRUMENT & LAB EQPT - 16 INCH DIAMETER FLAT MIRROR",
        "Rescind - stop work notice issued with notification of intent to terminate",
    ],
)
def test_source_text_without_a_preamble_is_untouched(text):
    assert strip(text) == text


def test_empty_and_none():
    assert strip("") == ""
    assert strip(None) == ""


# --- idempotence -----------------------------------------------------------


def test_stripping_twice_changes_nothing():
    """The incremental path reads the ledger back and rewrites it, so this
    runs over already-stripped values every build."""
    once = strip(
        "Status: TERMINATED. Reported savings: $0. "
        "DOGE Action Date: 2/18/2025. Knowledge management supports"
    )
    assert strip(once) == once


def test_only_one_preamble_is_removed():
    """Guards against chewing through a description that happens to open with
    something preamble-shaped after a real preamble."""
    assert (
        strip(
            "Status: TERMINATED. Reported savings: $0. DOGE Action Date: 1/1/2025. "
            "Status: this text belongs to the award."
        )
        == "Status: this text belongs to the award."
    )
