"""Historical NASA convenience-cancellation fiscal-year report."""

from collections.abc import Callable, Iterable
from pathlib import Path

from nasatrack import mirror
from nasatrack.fiscal_year_stats import FiscalYearStatsReport
from nasatrack.schema import CancellationAwardsByFiscalYearRow

DEFAULT_OUTPUT_PATH = Path("output") / "cancellations_for_convenience_awards_by_fiscal_year.csv"
COLUMN_LABELS = {
    "fiscal_year": "FY",
    "action_code_cancellation_awards": "Action Code Cancellation Awards",
    "keyword_cancellation_awards": "Keyword Cancellation Awards",
    "action_code_or_keyword_cancellation_awards": ("Action Code or Keyword Cancellation Awards"),
}


class CancellationsByFiscalYearReport(FiscalYearStatsReport[CancellationAwardsByFiscalYearRow]):
    """Publish historical NASA cancellation-signal award counts."""

    def __init__(
        self,
        *,
        fetch_rows: Callable[[], Iterable[CancellationAwardsByFiscalYearRow]] | None = None,
        output_path: Path = DEFAULT_OUTPUT_PATH,
    ) -> None:
        fetch = (
            mirror.fetch_cancellations_for_convenience_awards_by_fy
            if fetch_rows is None
            else fetch_rows
        )
        super().__init__(
            fetch_rows=fetch,
            output_path=output_path,
            column_labels=COLUMN_LABELS,
        )
