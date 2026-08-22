"""Shared OO coordinator for typed fiscal-year statistics CSV reports."""

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Generic, TypeVar

from nasatrack.schema import write_csv

RowT = TypeVar("RowT")


class FiscalYearStatsReport(Generic[RowT]):
    """Fetch, validate, and publish one typed fiscal-year statistics report."""

    def __init__(
        self,
        *,
        fetch_rows: Callable[[], Iterable[RowT]],
        output_path: Path,
        column_labels: Mapping[str, str],
    ) -> None:
        self._fetch_rows = fetch_rows
        self.output_path = Path(output_path)
        self._column_labels = dict(column_labels)

    def run(self) -> list[RowT]:
        """Publish a complete report, refusing to replace it with an empty result."""
        rows = list(self._fetch_rows())
        if not rows:
            raise RuntimeError("local mirror returned no fiscal-year rows; refusing to publish")
        write_csv(self.output_path, rows, column_labels=self._column_labels)
        return rows
