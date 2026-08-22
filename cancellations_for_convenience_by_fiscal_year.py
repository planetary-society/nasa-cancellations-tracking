"""Export NASA action-code, keyword, and union cancellation-award counts by FY."""

from nasatrack.cancellations_by_fiscal_year import CancellationsByFiscalYearReport


def main() -> int:
    report = CancellationsByFiscalYearReport()
    rows = report.run()
    print(f"{report.output_path}: {len(rows)} fiscal years")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
