"""Derived outcome columns: what actually happened to an award over time."""

import build_master_ledger as bml


def rec(**kw):
    base = {
        "First Award Amount": "",
        "Transaction Baseline Amount": "",
        "Award Amount": "",
        "First End Date": "",
        "End Date": "",
        "Claiming Source": "",
    }
    base.update(kw)
    return base


def trends(**kw):
    r = rec(**kw)
    bml.derive_trends(r)
    return r


def test_growth_beyond_threshold_is_grew():
    r = trends(**{"First Award Amount": "9046422.47", "Award Amount": "14272022.47"})
    assert r["Amount Trend"] == "grew"


def test_shrink_beyond_threshold():
    r = trends(**{"First Award Amount": "1000000", "Award Amount": "500000"})
    assert r["Amount Trend"] == "shrank"


def test_movement_inside_threshold_is_flat():
    r = trends(**{"First Award Amount": "1000000", "Award Amount": "1020000"})
    assert r["Amount Trend"] == "flat"


def test_missing_or_unparseable_amount_is_unknown():
    assert (
        trends(**{"First Award Amount": "", "Award Amount": "5"})["Amount Trend"]
        == "unknown"
    )
    assert (
        trends(**{"First Award Amount": "n/a", "Award Amount": "5"})["Amount Trend"]
        == "unknown"
    )
    # A zero starting amount has no meaningful percentage change.
    assert (
        trends(**{"First Award Amount": "0", "Award Amount": "5"})["Amount Trend"]
        == "unknown"
    )


def test_transaction_baseline_repairs_a_zero_first_observation():
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "0",
            "Transaction Baseline Amount": "28832",
            "Award Amount": "0",
        }
    )

    assert r["Amount Trend"] == "shrank"
    assert r["Claim Divergence"] == "claimed_and_shrank"


def test_transaction_baseline_repairs_a_missing_first_observation():
    r = trends(
        **{
            "First Award Amount": "",
            "Transaction Baseline Amount": "100",
            "Award Amount": "50",
        }
    )

    assert r["Amount Trend"] == "shrank"


def test_zero_or_unknown_transaction_baseline_leaves_trend_unknown():
    assert (
        trends(
            **{
                "First Award Amount": "0",
                "Transaction Baseline Amount": "0.00",
                "Award Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )
    assert (
        trends(
            **{
                "First Award Amount": "0",
                "Transaction Baseline Amount": "unknown",
                "Award Amount": "5",
            }
        )["Amount Trend"]
        == "unknown"
    )


def test_positive_first_observation_takes_precedence_over_transaction_baseline():
    r = trends(
        **{
            "First Award Amount": "100",
            "Transaction Baseline Amount": "500",
            "Award Amount": "200",
        }
    )

    assert r["Amount Trend"] == "grew"


def test_end_date_direction():
    assert (
        trends(**{"First End Date": "2025-09-30", "End Date": "2027-03-31"})[
            "End Date Trend"
        ]
        == "extended"
    )
    assert (
        trends(**{"First End Date": "2027-03-31", "End Date": "2025-09-30"})[
            "End Date Trend"
        ]
        == "truncated"
    )
    assert (
        trends(**{"First End Date": "2026-01-01", "End Date": "2026-01-01"})[
            "End Date Trend"
        ]
        == "unchanged"
    )
    assert (
        trends(**{"First End Date": "", "End Date": "2026-01-01"})["End Date Trend"]
        == "unknown"
    )


# --- claim divergence ------------------------------------------------------


def test_divergence_is_blank_without_a_claim():
    """Divergence is only meaningful where somebody actually claimed something."""
    r = trends(**{"First Award Amount": "100", "Award Amount": "500"})
    assert r["Amount Trend"] == "grew"
    assert r["Claim Divergence"] == ""


def test_the_headline_case_80GSFC23CA041():
    """DOGE claimed $21.1M saved; the award grew $9.0M -> $14.3M and was extended."""
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "9046422.47",
            "Award Amount": "14272022.47",
            "First End Date": "2026-02-28",
            "End Date": "2027-02-28",
        }
    )
    assert r["Claim Divergence"] == "claimed_but_grew"


def test_extension_without_growth_is_its_own_divergence():
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "1000",
            "Award Amount": "1000",
            "First End Date": "2025-01-01",
            "End Date": "2026-01-01",
        }
    )
    assert r["Claim Divergence"] == "claimed_but_extended"


def test_claimed_and_shrank_is_consistent_with_the_claim():
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "1000000",
            "Award Amount": "10000",
            "First End Date": "2026-01-01",
            "End Date": "2026-01-01",
        }
    )
    assert r["Claim Divergence"] == "claimed_and_shrank"


def test_claimed_and_flat():
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "1000",
            "Award Amount": "1000",
            "First End Date": "2026-01-01",
            "End Date": "2026-01-01",
        }
    )
    assert r["Claim Divergence"] == "consistent"


def test_a_claimed_award_that_grew_is_still_reported_never_pruned():
    """Divergence is a comparison, not a judgement: the row stays."""
    r = trends(
        **{
            "Claiming Source": "DOGE",
            "First Award Amount": "100",
            "Award Amount": "500",
        }
    )
    assert r["Claim Divergence"] == "claimed_but_grew"
    assert r["Claiming Source"] == "DOGE"
