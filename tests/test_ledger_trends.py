"""Derived outcome columns: what actually happened to an award over time."""

import build_master_ledger as bml


def rec(**kw):
    base = {
        "Obligated Amount When First Flagged": "",
        "Peak Cumulative Obligation": "",
        "Current Obligated Amount": "",
        "End Date When First Flagged": "",
        "Current End Date": "",
        "Claimed By": "",
    }
    base.update(kw)
    return base


def trends(**kw):
    r = rec(**kw)
    bml.derive_trends(r)
    return r


def test_growth_beyond_threshold_is_grew():
    r = trends(
        **{
            "Obligated Amount When First Flagged": "9046422.47",
            "Current Obligated Amount": "14272022.47",
        }
    )
    assert r["Amount Trend"] == "grew"


def test_shrink_beyond_threshold():
    r = trends(
        **{
            "Obligated Amount When First Flagged": "1000000",
            "Current Obligated Amount": "500000",
        }
    )
    assert r["Amount Trend"] == "shrank"


def test_movement_inside_threshold_is_flat():
    r = trends(
        **{
            "Obligated Amount When First Flagged": "1000000",
            "Current Obligated Amount": "1020000",
        }
    )
    assert r["Amount Trend"] == "flat"


def test_missing_or_unparseable_amount_is_unknown():
    assert (
        trends(
            **{
                "Obligated Amount When First Flagged": "",
                "Current Obligated Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )
    assert (
        trends(
            **{
                "Obligated Amount When First Flagged": "n/a",
                "Current Obligated Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )
    # A zero starting amount has no meaningful percentage change.
    assert (
        trends(
            **{
                "Obligated Amount When First Flagged": "0",
                "Current Obligated Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )


def test_transaction_baseline_repairs_a_zero_first_observation():
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "0",
            "Peak Cumulative Obligation": "28832",
            "Current Obligated Amount": "0",
        }
    )

    assert r["Amount Trend"] == "shrank"
    assert r["DOGE Claim vs Outcome"] == "claimed_and_shrank"


def test_transaction_baseline_repairs_a_missing_first_observation():
    r = trends(
        **{
            "Obligated Amount When First Flagged": "",
            "Peak Cumulative Obligation": "100",
            "Current Obligated Amount": "50",
        }
    )

    assert r["Amount Trend"] == "shrank"


def test_zero_or_unknown_transaction_baseline_leaves_trend_unknown():
    assert (
        trends(
            **{
                "Obligated Amount When First Flagged": "0",
                "Peak Cumulative Obligation": "0.00",
                "Current Obligated Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )
    assert (
        trends(
            **{
                "Obligated Amount When First Flagged": "0",
                "Peak Cumulative Obligation": "unknown",
                "Current Obligated Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )


def test_positive_first_observation_takes_precedence_over_transaction_baseline():
    r = trends(
        **{
            "Obligated Amount When First Flagged": "100",
            "Peak Cumulative Obligation": "500",
            "Current Obligated Amount": "200",
        }
    )

    assert r["Amount Trend"] == "grew"


def test_end_date_direction():
    assert (
        trends(
            **{
                "End Date When First Flagged": "2025-09-30",
                "Current End Date": "2027-03-31",
            }
        )["End Date Trend"]
        == "extended"
    )
    assert (
        trends(
            **{
                "End Date When First Flagged": "2027-03-31",
                "Current End Date": "2025-09-30",
            }
        )["End Date Trend"]
        == "truncated"
    )
    assert (
        trends(
            **{
                "End Date When First Flagged": "2026-01-01",
                "Current End Date": "2026-01-01",
            }
        )["End Date Trend"]
        == "unchanged"
    )
    assert (
        trends(**{"End Date When First Flagged": "", "Current End Date": "2026-01-01"})[
            "End Date Trend"
        ]
        == "unknown"
    )


# --- claim divergence ------------------------------------------------------


def test_divergence_is_blank_without_a_claim():
    """Divergence is only meaningful where somebody actually claimed something."""
    r = trends(
        **{
            "Obligated Amount When First Flagged": "100",
            "Current Obligated Amount": "500",
        }
    )
    assert r["Amount Trend"] == "grew"
    assert r["DOGE Claim vs Outcome"] == ""


def test_the_headline_case_80GSFC23CA041():
    """DOGE claimed $21.1M saved; the award grew $9.0M -> $14.3M and was extended."""
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "9046422.47",
            "Current Obligated Amount": "14272022.47",
            "End Date When First Flagged": "2026-02-28",
            "Current End Date": "2027-02-28",
        }
    )
    assert r["DOGE Claim vs Outcome"] == "claimed_but_grew"


def test_extension_without_growth_is_its_own_divergence():
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "1000",
            "Current Obligated Amount": "1000",
            "End Date When First Flagged": "2025-01-01",
            "Current End Date": "2026-01-01",
        }
    )
    assert r["DOGE Claim vs Outcome"] == "claimed_but_extended"


def test_claimed_and_shrank_is_consistent_with_the_claim():
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "1000000",
            "Current Obligated Amount": "10000",
            "End Date When First Flagged": "2026-01-01",
            "Current End Date": "2026-01-01",
        }
    )
    assert r["DOGE Claim vs Outcome"] == "claimed_and_shrank"


def test_claimed_and_flat():
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "1000",
            "Current Obligated Amount": "1000",
            "End Date When First Flagged": "2026-01-01",
            "Current End Date": "2026-01-01",
        }
    )
    assert r["DOGE Claim vs Outcome"] == "consistent"


def test_a_claimed_award_that_grew_is_still_reported_never_pruned():
    """Divergence is a comparison, not a judgement: the row stays."""
    r = trends(
        **{
            "Claimed By": "DOGE",
            "Obligated Amount When First Flagged": "100",
            "Current Obligated Amount": "500",
        }
    )
    assert r["DOGE Claim vs Outcome"] == "claimed_but_grew"
    assert r["Claimed By"] == "DOGE"
