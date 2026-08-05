"""Settles the premise the end-date COALESCEs rest on.

Three queries pick an end date from `ordering_period_end_date` and
`period_of_performance_current_end_date`, and two of them disagree about which
should win:

  * search.py _award_end_date          - period of performance first
  * Q3_END_DATE_TRUNCATION             - period of performance first
  * INITIAL_END_DATE_SQL               - ordering period first (write-once
                                         provenance, deliberately left alone)

The precedence only changes an answer for a transaction carrying BOTH values.
The original argument for ordering-period-first was that no such transaction
exists - `ordering_period_end_date` being an FPDS IDV field, null elsewhere -
but that was asserted in a comment, never checked, and it is load-bearing: if
it is false, the ordering rule reports a shortened ordering window as a
shortened period of performance on ordinary contracts.

This test answers it against a real mirror. It cannot run in CI or on any
machine without the local Postgres copy, so it skips there rather than
passing - a skip is "unanswered", not "fine". Run it on a DB machine to
settle the question for good.
"""

import pytest

from local_usaspending_mirror_query import (
    LocalMirrorUnavailableError,
    LocalUSASpendingMirrorQuery,
)

BOTH_PRESENT_SQL = """
SELECT COUNT(*) AS both_present
FROM rpt.transaction_search ts
WHERE ts.awarding_agency_id = 862
  AND ts.action_date >= '2020-01-01'
  AND NULLIF(TRIM(ts.ordering_period_end_date), '') IS NOT NULL
  AND ts.period_of_performance_current_end_date IS NOT NULL
  AND NULLIF(TRIM(ts.ordering_period_end_date), '')::date
      IS DISTINCT FROM ts.period_of_performance_current_end_date
"""

STATEMENT_TIMEOUT = "SET statement_timeout = '60s'"


@pytest.fixture
def mirror_cursor():
    """A cursor on the local mirror, or a skip explaining why there is none."""
    if not LocalUSASpendingMirrorQuery.is_configured():
        pytest.skip("no local USAspending mirror configured; premise unanswered")
    query = LocalUSASpendingMirrorQuery()
    try:
        with query._cursor() as cur:
            cur.execute(STATEMENT_TIMEOUT)
            yield cur
    except LocalMirrorUnavailableError as exc:
        pytest.skip(f"local USAspending mirror unreachable ({exc}); premise unanswered")


def test_no_transaction_carries_two_different_end_dates(mirror_cursor):
    """The premise itself.

    A pass means the three queries agree whatever their arm order, and
    INITIAL_END_DATE_SQL can be aligned with the other two. A failure means
    the precedence is live: read the count as the number of NASA transactions
    where picking the wrong arm picks the wrong date.
    """
    mirror_cursor.execute(BOTH_PRESENT_SQL)
    both_present = dict(mirror_cursor.fetchone())["both_present"]

    assert both_present == 0, (
        f"{both_present} NASA transactions carry an ordering-period end date "
        f"AND a different period-of-performance end date. The COALESCE arm "
        f"order decides which is used, so INITIAL_END_DATE_SQL "
        f"(ordering-first) and Q3_END_DATE_TRUNCATION (period-first) are "
        f"answering differently on these rows. Reconcile them before trusting "
        f"either - see the comments on both queries."
    )
