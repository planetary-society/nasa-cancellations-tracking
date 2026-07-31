"""utils.parse_mod_number - NPDV's latest-mod selection rests on this.

A mod that parses to 0 loses the latest-mod contest to every numbered mod, so
a parse failure on an award's FINAL mod would make NPDV keyword-scan a stale
description. That is exactly the silent-misordering shape the 2025-2026 audit
kept finding, hence these pins.
"""

import pytest

from utils import parse_mod_number


@pytest.mark.parametrize(
    ("text", "award_id", "mod_num"),
    [
        # The documented shapes.
        ("80JSC023FA010 Modification P00054", "80JSC023FA010", 54),
        ("AWARD Modification S022", "AWARD", 22),
        ("AWARD Modification A00002", "AWARD", 2),
        ("AWARD Modification 215", "AWARD", 215),
        ("AWARD Modification 0 (Base Record)", "AWARD", 0),
        ("AWARD", "AWARD", 0),
    ],
)
def test_documented_shapes(text, award_id, mod_num):
    assert parse_mod_number(text) == (award_id, mod_num)


def test_letter_suffixed_mod_parses_as_its_base_number():
    """P0037A is a sub-modification of mod 37, first seen 2026-07-30 on
    80JSC023FA010. The old parser stripped only LEADING non-digits, choked on
    the trailing letter, and defaulted to 0 - which would silently hand the
    latest-mod contest to any numbered mod if a letter-suffixed mod were the
    award's final one."""
    assert parse_mod_number("80JSC023FA010 Modification P0037A") == (
        "80JSC023FA010",
        37,
    )


def test_a_letter_suffixed_mod_ties_with_its_base_and_the_later_row_wins():
    """NPDV keeps a row when `mod_num >= stored`, so the tie between P00037
    and P0037A resolves to whichever the source file lists later - the
    revision. Parity of the two parses is what makes that work."""
    _, base = parse_mod_number("AWARD Modification P00037")
    _, suffixed = parse_mod_number("AWARD Modification P0037A")

    assert base == suffixed == 37


def test_none_and_empty():
    assert parse_mod_number(None) == ("", 0)
    assert parse_mod_number("   ") == ("", 0)
