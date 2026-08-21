"""The doors' coarse filters must be SUPERSETS of the Python verdict.

Design axiom 1: SQL and the ORM only prefilter; `criteria.is_explicit_termination`
decides. A prefilter that is NARROWER than the verdict silently loses awards and
nothing downstream would notice - so containment is asserted here, in the one
direction that matters.

The mirror's WHERE clause is re-implemented below in Python and run over the
same synthetic rows the real query would return. Offline by construction: the
SQL is treated as text, no database is contacted.
"""

import re
from datetime import date

import pytest

from nasatrack import criteria, mirror

# The candidates CTE's regex, back-translated: Postgres's \y is Python's \b and
# `~*` is a case-insensitive search.
SQL_PATTERN = re.compile(criteria.TERMINATION_TEXT_SQL.replace(r"\y", r"\b"), re.IGNORECASE)


def sql_candidate(record) -> bool:
    """`(is_fpds AND action_type = ANY(codes)) OR description ~* pattern`, inside the window."""
    coded = bool(record["is_fpds"]) and record["action_type"] in criteria.TERMINATION_ACTION_CODES
    worded = bool(SQL_PATTERN.search(record["description"]))
    return record["action_date"] >= criteria.WINDOW_START and (coded or worded)


def row(description="", *, action_type="", is_fpds=True, type_code="C", day="2025-06-01"):
    """One row shaped exactly like SQL_TERMINATION_CANDIDATES returns."""
    return {
        "award_id": 1,
        "native_award_id": "80NSSC25C0001",
        "generated_unique_award_id": "CONT_AWD_80NSSC25C0001",
        "sort_key": "80NSSC25C0001_0",
        "is_fpds": is_fpds,
        "award_type_code": type_code,
        "action_date": date.fromisoformat(day),
        "action_type": action_type,
        "modification_number": "P00003",
        "description": description,
        "award_description": "MARS RELAY NETWORK OPERATIONS",
        "recipient_address1": "1 ROCKET RD",
        "recipient_address2": "",
        "recipient_city": "HAWTHORNE",
        "recipient_state": "CA",
        "recipient_zip": "90250",
        "recipient_district": "36",
        "pop_city": "PASADENA",
        "pop_state": "CA",
        "pop_zip": "91109",
        "pop_district": "28",
        "total_obligated": None,
        "total_potential_value": None,
        "amount": None,
        "recipient_name": "ACME AEROSPACE",
    }


# (label, row, whether the Python verdict accepts it). The SQL answer is not
# listed: the invariant is containment, and asserting SQL row by row would just
# restate the predicate above.
CASES = [
    ("f code, no language", row(action_type="F", description="ADMINISTRATIVE MOD"), True),
    (
        "n code with its prose",
        row(action_type="N", description="LEGAL CONTRACT CANCELLATION"),
        True,
    ),
    ("language, no code", row("TERMINATION FOR CONVENIENCE OF THE GOVERNMENT"), True),
    ("idv with an f code", row(action_type="F", type_code="IDV_B", description="MOD"), True),
    ("stop-work order", row("STOP-WORK ORDER ISSUED"), True),
    ("termination notice", row("TERMINATION NOTICE ISSUED: IN SPACE PRODUCTION"), True),
    # NASA's misspellings, all field-observed in the archived descriptions.
    ("misspelled convience", row("TERMINATE FOR CONVIENCE"), True),
    ("misspelled connivence", row("TERMINATION FOR CONNIVENCE"), True),
    ("misspelled convicne", row("TERMINATION FOR CONVICNE"), True),
    ("run-together spelling", row("TERMINATIONFORCONVENIENCE"), True),
    # Grants carry no reason-for-modification code at all, so their
    # terminations are language-only - and an F on a grant is not a
    # termination code, which is why the text arm must not filter on is_fpds.
    (
        "grant terminated in prose",
        row("TERMINATION FOR CONVENIENCE AGREEMENT", is_fpds=False, type_code="04"),
        True,
    ),
    (
        "grant with a stray f",
        row(action_type="F", is_fpds=False, type_code="04", description="AMENDMENT"),
        False,
    ),
    # SQL keeps cause in the net so `is_cause` owns that definition alone.
    ("cause only", row("TERMINATED FOR CAUSE"), False),
    ("cause with an f code", row(action_type="F", description="TERMINATION FOR DEFAULT"), False),
    # The window lives in both doors: the nasa CTE bounds action_date and
    # `in_window` re-checks it.
    (
        "pre-window termination",
        row(action_type="F", description="STOP WORK", day="2025-01-19"),
        False,
    ),
    ("window edge day", row(action_type="F", description="STOP WORK", day="2025-01-20"), True),
    # A rescission names what it rescinds, so both nets see a termination here;
    # `accept_award`, not the prefilter, is what clears it.
    ("reversal text", row("RESCISSION OF THE STOP WORK ORDER"), True),
    ("unrelated mod", row("EXERCISE OPTION YEAR THREE"), False),
    # Flight termination systems are range-safety hardware, not contract actions.
    ("termination hardware", row("PURCHASE OF FLIGHT TERMINATION RECEIVER/DECODER"), False),
]


@pytest.mark.parametrize(("label", "record", "accepted"), CASES, ids=[case[0] for case in CASES])
def test_sql_prefilter_contains_the_python_verdict(label, record, accepted):
    txn = mirror.txn_from_row(record)
    assert criteria.is_explicit_termination(txn) is accepted
    if accepted:
        assert sql_candidate(record), f"SQL prefilter would never fetch {label!r}"


def test_the_prefilter_is_strictly_wider_in_the_permitted_direction():
    # Cause rows are fetched and then dropped in Python - the one home for the
    # cause definition. A superset is allowed; the reverse never is.
    cause = row("TERMINATED FOR CAUSE")
    assert sql_candidate(cause)
    assert not criteria.is_explicit_termination(mirror.txn_from_row(cause))


# The other door's coarse filter is held to the same containment rule by
# test_vocabulary.test_every_api_keyword_satisfies_is_termination.


def test_the_vocabulary_is_bound_never_interpolated():
    sql = mirror.SQL_TERMINATION_CANDIDATES
    assert "%(pattern)s" in sql and "%(codes)s" in sql and "%(window_start)s" in sql
    assert criteria.TERMINATION_TEXT_SQL not in sql
    assert "%(min_days)s" in mirror.SQL_POP_CHANGES
    # The agency scope is the shared constant, not a number typed twice.
    for query in (sql, mirror.SQL_POP_CHANGES):
        assert f"ts.awarding_agency_id = {criteria.NASA_AGENCY_ID}" in query


# ---------------------------------------------------------------------------
# Award identity and type: one answer for both doors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # From the `type` code alone - what the mirror's termination query has.
        ({"type_code": "C", "is_fpds": True}, "contract"),
        ({"type_code": "A", "is_fpds": True}, "contract"),
        ({"type_code": "IDV_B_A", "is_fpds": True}, "idv"),
        ({"type_code": "idv_c", "is_fpds": True}, "idv"),
        ({"type_code": "04", "is_fpds": False}, "grant"),
        ({"type_code": "", "is_fpds": False}, "grant"),
        # A missing type code falls back to the FPDS flag rather than guessing.
        ({"type_code": None, "is_fpds": True}, "contract"),
        # From the generated award id alone.
        ({"generated_id": "CONT_IDV_80HQTR23AA002_8000", "is_fpds": True}, "idv"),
        ({"generated_id": "CONT_AWD_80NSSC25C0001_8000", "is_fpds": True}, "contract"),
        ({"generated_id": "ASST_NON_80NSSC25K0030_8000", "is_fpds": False}, "grant"),
        ({"generated_id": "", "is_fpds": True}, "contract"),
        # Both, agreeing - and either one alone is enough to say "idv".
        ({"type_code": "IDV_B", "generated_id": "CONT_IDV_X", "is_fpds": True}, "idv"),
        ({"type_code": "C", "generated_id": "CONT_IDV_X", "is_fpds": True}, "idv"),
        ({"type_code": "IDV_B", "generated_id": "", "is_fpds": True}, "idv"),
        # Nothing at all: the FPDS flag is the only evidence left.
        ({}, "grant"),
    ],
)
def test_award_type(kwargs, expected):
    assert criteria.award_type(**kwargs) == expected


def test_award_key_falls_back_to_a_namespaced_piid():
    # Some IDV transactions carry no generated id; a bare PIID must not land in
    # the generated id's key space.
    assert criteria.award_key("", "80NSSC25C0001") == "PIID:80NSSC25C0001"
    assert criteria.award_key("CONT_AWD_X", "80NSSC25C0001") == "CONT_AWD_X"
    assert criteria.is_fallback_key("PIID:80NSSC25C0001")
    assert not criteria.is_fallback_key("CONT_AWD_X")


def test_the_mirror_door_keys_its_rows_the_shared_way():
    record = row(action_type="F")
    record["generated_unique_award_id"] = None
    txn = mirror.txn_from_row(record)
    assert txn.award_key == "PIID:80NSSC25C0001"
    assert txn.generated_award_id == ""
    assert mirror.txn_from_row(row()).award_key == "CONT_AWD_80NSSC25C0001"


def test_rows_are_normalised_with_the_mirror_as_their_source():
    txn = mirror.txn_from_row(row(action_type="f", description="STOP WORK ORDER"))
    assert txn.source == "mirror"
    assert txn.action_type == "F"
    assert txn.action_date == date(2025, 6, 1)
    assert txn.sort_key == "80NSSC25C0001_0"
