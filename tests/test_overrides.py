"""Human overrides remove or annotate published rows, and never invent one."""

from pathlib import Path

import pytest

from nasatrack.terminations import (
    EXCLUDING_STATUSES,
    OVERRIDE_ID_COLUMN,
    OVERRIDE_STATUS_COLUMN,
    apply_overrides,
    load_overrides,
)
from tests.test_merge import row

# The file this project actually keeps, so a header change here fails a test
# rather than silently un-applying every human judgement.
REAL_OVERRIDES = Path(__file__).resolve().parents[1] / "verification" / "dropped_award_status.csv"


def write_overrides(path, records):
    lines = [f"{OVERRIDE_ID_COLUMN},{OVERRIDE_STATUS_COLUMN},Verified Date,Evidence"]
    lines += [f"{award_id},{status},2026-08-06,because" for award_id, status in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("status", sorted(EXCLUDING_STATUSES))
def test_excluding_statuses_remove_the_row(status):
    rows = [row("CONT_AWD_80NSSC25C0001", source="api", award_id="80NSSC25C0001")]
    kept, warnings = apply_overrides(rows, {"80NSSC25C0001": status})
    assert kept == []
    assert warnings == []


@pytest.mark.parametrize(
    "status", ["termination_confirmed", "closed_out", "descoped", "needs_manual_review"]
)
def test_other_statuses_annotate_and_keep(status):
    rows = [row("CONT_AWD_80NSSC25C0001", source="api", award_id="80NSSC25C0001")]
    kept, warnings = apply_overrides(rows, {"80NSSC25C0001": status})
    assert [r.override_status for r in kept] == [status]
    assert warnings == []


def test_an_override_matches_the_award_key_too():
    # The doge/verification files name awards by PIID or FAIN; a mirror-only
    # IDV row can be keyed on the generated id instead.
    rows = [row("CONT_AWD_80NSSC25C0001", source="mirror", award_id="")]
    kept, _ = apply_overrides(rows, {"CONT_AWD_80NSSC25C0001": "termination_confirmed"})
    assert [r.override_status for r in kept] == ["termination_confirmed"]


def test_rows_without_an_override_are_untouched():
    rows = [row("CONT_AWD_A", source="api"), row("CONT_AWD_B", source="mirror")]
    kept, warnings = apply_overrides(rows, {})
    assert kept == rows
    assert warnings == []


def test_an_unmatched_override_warns_and_never_synthesises_a_row():
    rows = [row("CONT_AWD_A", source="api", award_id="A")]
    kept, warnings = apply_overrides(rows, {"NNK14CA65T": "excluded_by_design"})
    assert kept == rows
    assert warnings == ["unmatched override: NNK14CA65T (excluded_by_design)"]


def test_load_overrides_reads_the_human_columns(tmp_path):
    path = write_overrides(
        tmp_path / "dropped_award_status.csv",
        [("80NSSC19K0326", "vacated"), ("80HQTR24F0072", "termination_confirmed")],
    )
    assert load_overrides(path) == {
        "80NSSC19K0326": "vacated",
        "80HQTR24F0072": "termination_confirmed",
    }


def test_a_missing_overrides_file_is_a_no_op(tmp_path):
    overrides = load_overrides(tmp_path / "absent.csv")
    assert overrides == {}
    rows = [row("CONT_AWD_A", source="api")]
    assert apply_overrides(rows, overrides) == (rows, [])


def test_the_committed_overrides_file_still_parses():
    overrides = load_overrides(REAL_OVERRIDES)
    assert overrides["80NSSC19K0326"] == "vacated"
    assert overrides["NNK14CA65T"] == "excluded_by_design"
    assert overrides["80NSSC24K1264"] == "closed_out"
    # Every status is one this module knows how to act on.
    assert set(overrides.values()) <= EXCLUDING_STATUSES | {
        "termination_confirmed",
        "closed_out",
        "descoped",
        "needs_manual_review",
    }
