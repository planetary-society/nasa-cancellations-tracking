"""Historical cancellation-award report: shared query, row, and CSV contract."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

import cancellations_for_convenience_by_fiscal_year as script
from nasatrack import cancellations_by_fiscal_year as report_module
from nasatrack import criteria, fiscal_year_stats, mirror, schema
from nasatrack.schema import write_csv


@dataclass(frozen=True)
class ExampleFiscalYearRow:
    fiscal_year: int
    action_code_cancellation_awards: int
    keyword_cancellation_awards: int
    action_code_or_keyword_cancellation_awards: int


def test_shared_csv_writer_accepts_display_labels(tmp_path):
    path = tmp_path / "report.csv"

    write_csv(
        path,
        [
            ExampleFiscalYearRow(
                fiscal_year=2010,
                action_code_cancellation_awards=7,
                keyword_cancellation_awards=3,
                action_code_or_keyword_cancellation_awards=9,
            )
        ],
        column_labels={
            "fiscal_year": "FY",
            "action_code_cancellation_awards": "Action Code Cancellation Awards",
            "keyword_cancellation_awards": "Keyword Cancellation Awards",
            "action_code_or_keyword_cancellation_awards": (
                "Action Code or Keyword Cancellation Awards"
            ),
        },
    )

    assert path.read_text(encoding="utf-8") == (
        "FY,Action Code Cancellation Awards,Keyword Cancellation Awards,"
        "Action Code or Keyword Cancellation Awards\n2010,7,3,9\n"
    )


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


def test_report_object_publishes_with_the_shared_writer(monkeypatch, tmp_path):
    assert (
        Path("output/cancellations_for_convenience_awards_by_fiscal_year.csv")
        == report_module.DEFAULT_OUTPUT_PATH
    )
    rows = [schema.CancellationAwardsByFiscalYearRow(2010, 7, 3, 9)]
    output_path = tmp_path / "report.csv"
    calls = []

    def fake_write_csv(path, given_rows, *, column_labels):
        calls.append((path, list(given_rows), column_labels))

    monkeypatch.setattr(fiscal_year_stats, "write_csv", fake_write_csv)
    report = report_module.CancellationsByFiscalYearReport(
        fetch_rows=lambda: rows,
        output_path=output_path,
    )

    assert report.run() == rows
    assert calls == [
        (
            output_path,
            rows,
            {
                "fiscal_year": "FY",
                "action_code_cancellation_awards": "Action Code Cancellation Awards",
                "keyword_cancellation_awards": "Keyword Cancellation Awards",
                "action_code_or_keyword_cancellation_awards": (
                    "Action Code or Keyword Cancellation Awards"
                ),
            },
        )
    ]


def test_report_refuses_to_replace_an_output_with_no_rows(tmp_path):
    output_path = tmp_path / "report.csv"
    output_path.write_text("previous complete report\n", encoding="utf-8")
    report = report_module.CancellationsByFiscalYearReport(
        fetch_rows=lambda: [],
        output_path=output_path,
    )

    with pytest.raises(RuntimeError, match="returned no fiscal-year rows"):
        report.run()

    assert output_path.read_text(encoding="utf-8") == "previous complete report\n"


def test_standalone_script_delegates_to_the_report_object(monkeypatch, capsys, tmp_path):
    output_path = tmp_path / "report.csv"

    class FakeReport:
        def __init__(self):
            self.output_path = output_path

        def run(self):
            return [
                schema.CancellationAwardsByFiscalYearRow(2010, 7, 3, 9),
                schema.CancellationAwardsByFiscalYearRow(2011, 9, 4, 12),
            ]

    monkeypatch.setattr(script, "CancellationsByFiscalYearReport", FakeReport)

    assert script.main() == 0
    assert capsys.readouterr().out == f"{output_path}: 2 fiscal years\n"
