"""The vocabulary renders into Postgres's ARE dialect without changing meaning."""

import re

import pytest

from nasatrack.criteria import (
    CAUSE_TEXT,
    TERM_TEXT,
    TERMINATION_TEXT_SQL,
    pg_regex,
)


def test_word_boundaries_are_translated():
    rendered = pg_regex(TERM_TEXT)
    # \b is a backspace in Postgres, \y is the word boundary.
    assert r"\b" not in rendered
    assert rendered.count(r"\y") == TERM_TEXT.pattern.count(r"\b")
    assert r"\y" in rendered


@pytest.mark.parametrize(
    "pattern",
    [
        re.compile(r"terminat\w*(?=\s+for)"),
        re.compile(r"terminat\w*(?!\s+for\s+cause)"),
        re.compile(r"(?<=notice\s)of\s+termination"),
    ],
)
def test_lookarounds_are_rejected(pattern):
    with pytest.raises(ValueError, match="lookaround"):
        pg_regex(pattern)


def test_non_capturing_groups_survive():
    assert "(?:e|ed|ion)" in pg_regex(TERM_TEXT)


def test_termination_text_sql_is_one_well_formed_alternation():
    assert pg_regex(TERM_TEXT, CAUSE_TEXT) == TERMINATION_TEXT_SQL
    # Cause stays in the SQL net on purpose: SQL fetches those rows and
    # is_cause() drops them in Python, so the cause definition has one home.
    assert CAUSE_TEXT.pattern in TERMINATION_TEXT_SQL
    back = re.compile(TERMINATION_TEXT_SQL.replace(r"\y", r"\b"), re.IGNORECASE)
    assert back.search("terminate for convenience")
    assert back.search("terminated for cause")
    assert back.search("legal contract cancellation")
    assert not back.search("routine administrative modification")
