"""The mirror door's fiscal-year cancellation-count query and its SQL contract."""

from contextlib import contextmanager
from datetime import date

from nasatrack import criteria, mirror, schema


def test_mirror_counts_distinct_action_code_keyword_and_union_awards_by_fiscal_year(
    monkeypatch,
):
    calls = []

    class FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchall(self):
            return [
                {
                    "fy": 2010,
                    "action_code_cancellation_awards": 7,
                    "keyword_cancellation_awards": 3,
                    "action_code_or_keyword_cancellation_awards": 9,
                },
                {
                    "fy": 2011,
                    "action_code_cancellation_awards": 0,
                    "keyword_cancellation_awards": 2,
                    "action_code_or_keyword_cancellation_awards": 2,
                },
            ]

    @contextmanager
    def fake_cursor(timeout):
        calls.append(("timeout", timeout))
        yield FakeCursor()

    monkeypatch.setattr(mirror, "_cursor", fake_cursor)

    assert mirror.fetch_cancellations_for_convenience_awards_by_fy() == [
        schema.CancellationAwardsByFiscalYearRow(
            fiscal_year=2010,
            action_code_cancellation_awards=7,
            keyword_cancellation_awards=3,
            action_code_or_keyword_cancellation_awards=9,
        ),
        schema.CancellationAwardsByFiscalYearRow(
            fiscal_year=2011,
            action_code_cancellation_awards=0,
            keyword_cancellation_awards=2,
            action_code_or_keyword_cancellation_awards=2,
        ),
    ]

    sql, params = calls[1]
    normalized_sql = " ".join(sql.split())
    assert calls[0] == ("timeout", 120)
    assert "FROM rpt.transaction_search" in sql
    assert "ts.awarding_agency_id" in sql
    assert "ts.action_date >= %(start_date)s" in sql
    assert "ts.action_date <= CURRENT_DATE" in sql
    assert "source.is_fpds IS TRUE" in sql
    assert "source.action_type = ANY(%(action_codes)s)" in sql
    assert "ts.award_id" in sql
    assert "ts.transaction_description" in sql
    assert "~* %(keyword_pattern)s" in sql
    assert "!~* %(cause_pattern)s" in sql
    assert "count(DISTINCT award_id) FILTER (WHERE by_action_code)" in normalized_sql
    assert "count(DISTINCT award_id) FILTER (WHERE by_keyword)" in normalized_sql
    assert "count(DISTINCT award_id) FILTER (WHERE by_action_code OR by_keyword)" in normalized_sql
    assert "generate_series" in sql
    assert "LEFT JOIN cancellations USING (fy)" in sql
    assert "COALESCE(cancellations.action_code_cancellation_awards, 0)" in normalized_sql
    assert "COALESCE(cancellations.keyword_cancellation_awards, 0)" in normalized_sql
    assert "COALESCE(cancellations.action_code_or_keyword_cancellation_awards, 0)" in normalized_sql
    assert "GROUP BY fy" in sql
    assert "ORDER BY fiscal_years.fy" in sql
    assert params == {
        "start_date": date(2009, 10, 1),
        "start_fiscal_year": 2010,
        "action_codes": ["F"],
        "keyword_pattern": criteria.TERMINATION_KEYWORD_SQL,
        "cause_pattern": criteria.CAUSE_TEXT_SQL,
    }
