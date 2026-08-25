"""The merge step publishes terminations.csv from the parts, offline.

Also the empty-output tripwires: every door and the merge itself refuse to
publish nothing, because none of these sources can legitimately go to zero.
"""

import json
from argparse import Namespace

import pytest

from nasatrack import api, cli, doge, mirror
from nasatrack.cli import merge_step, run_api, run_doge
from nasatrack.schema import (
    CancellationAwardsByFiscalYearRow,
    PopChangeRow,
    TerminationRow,
    read_csv,
    write_csv,
)
from tests.test_accept_award import txn
from tests.test_descope import DESCOPE_NOTICE
from tests.test_merge import row
from tests.test_overrides import write_overrides
from tests.test_schema import a_pop_change


def test_merge_step_publishes_from_the_parts(tmp_path, capsys):
    api_part = tmp_path / "parts" / "api_terminations.csv"
    mirror_part = tmp_path / "parts" / "mirror_terminations.csv"
    write_csv(
        api_part,
        [
            row("CONT_AWD_A", source="api", award_id="A", day="2025-06-01"),
            row("CONT_AWD_B", source="api", award_id="B", day="2026-01-15"),
        ],
    )
    write_csv(
        mirror_part,
        [
            row("CONT_AWD_A", source="mirror", award_id="A", day="2025-06-01"),
            row("CONT_AWD_C", source="mirror", award_id="C", day="2025-02-01"),
            row(
                "CONT_AWD_D",
                source="mirror",
                award_id="D",
                day="2025-04-01",
                action_type="M",
                transaction_description=DESCOPE_NOTICE,
            ),
        ],
    )
    mirror_run = tmp_path / "parts" / "mirror_run.json"
    mirror_run.write_text(json.dumps({"ran_at": "2026-08-01T12:00:00+00:00", "rows": 2}))
    overrides = write_overrides(
        tmp_path / "dropped_award_status.csv",
        [("C", "vacated"), ("B", "still_terminated"), ("GONE", "continued")],
    )
    output = tmp_path / "terminations.csv"
    descoped_output = tmp_path / "descoped.csv"

    merge_step(
        api_part=api_part,
        mirror_part=mirror_part,
        mirror_run=mirror_run,
        overrides=overrides,
        output=output,
        descoped_output=descoped_output,
    )

    published = read_csv(output, TerminationRow)
    assert [(r.award_key, r.sources, r.override_status) for r in published] == [
        ("CONT_AWD_B", "api", "still_terminated"),
        ("CONT_AWD_A", "api;mirror", ""),
    ]
    # The de-scope leaves terminations.csv entirely and publishes on its own.
    assert [r.award_key for r in read_csv(descoped_output, TerminationRow)] == ["CONT_AWD_D"]

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "terminations.csv: 2 rows (api 2, mirror 3, both 1)",
        "descoped.csv: 1 rows",
    ]
    assert "unmatched override: GONE (continued)" in captured.err
    assert "mirror part from 2026-08-01T12:00:00+00:00, 2 rows" in captured.err


def test_merge_step_with_no_parts_at_all_refuses_to_publish(tmp_path):
    # Both parts missing means the read broke, not that every termination was
    # rescinded overnight. The published file is left exactly as it was.
    output = tmp_path / "terminations.csv"
    with pytest.raises(SystemExit, match="0 rows"):
        merge_step(
            api_part=tmp_path / "api.csv",
            mirror_part=tmp_path / "mirror.csv",
            mirror_run=tmp_path / "mirror_run.json",
            overrides=tmp_path / "overrides.csv",
            output=output,
        )
    assert not output.exists()


def test_mirror_door_writes_its_three_outputs(monkeypatch, tmp_path, capsys):
    # The fiscal-year counts are the mirror's third artifact, written from the
    # same run as the part file and pop_changes.csv, with plain field-name
    # headers like every output. The merge is stubbed: it reads the committed
    # parts, which this test has no business touching.
    monkeypatch.setattr(cli, "MIRROR_PART", tmp_path / "mirror_terminations.csv")
    monkeypatch.setattr(cli, "MIRROR_RUN", tmp_path / "mirror_run.json")
    monkeypatch.setattr(cli, "POP_CHANGES_CSV", tmp_path / "pop_changes.csv")
    monkeypatch.setattr(cli, "FISCAL_YEAR_CSV", tmp_path / "by_fiscal_year.csv")
    monkeypatch.setattr(cli, "merge_step", lambda: None)

    monkeypatch.setattr(mirror, "is_configured", lambda: True)
    monkeypatch.setattr(
        mirror, "fetch_terminated_awards", lambda: [txn("2025-06-01", action_type="F")]
    )
    monkeypatch.setattr(mirror, "fetch_pop_changes", lambda: [a_pop_change()])
    # The FY report's history fetch: capture its bounds, serve one termination.
    history_calls = []

    def fake_history(**kwargs):
        history_calls.append(kwargs)
        return [txn("2025-06-01", action_type="F")]

    monkeypatch.setattr(mirror, "fetch_termination_txns", fake_history)

    cli.run_mirror(Namespace())

    assert history_calls == [
        {"window_start": "2009-10-01", "timeout_s": mirror.FY_HISTORY_TIMEOUT_S}
    ]
    counts = read_csv(tmp_path / "by_fiscal_year.csv", CancellationAwardsByFiscalYearRow)
    assert counts[0] == CancellationAwardsByFiscalYearRow(fiscal_year=2010, terminated_awards=0)
    assert {r.fiscal_year: r.terminated_awards for r in counts}[2025] == 1
    assert len(read_csv(tmp_path / "mirror_terminations.csv", TerminationRow)) == 1
    assert len(read_csv(tmp_path / "pop_changes.csv", PopChangeRow)) == 1
    assert capsys.readouterr().out.splitlines() == [
        "mirror_terminations.csv: 1 rows",
        "pop_changes.csv: 1 rows",
        "by_fiscal_year.csv: 17 rows",
    ]


def test_an_empty_api_door_refuses_to_publish(monkeypatch):
    # ~170 awards stand in the window, so zero is never real news - it is a
    # broken sweep. Nothing is written and nothing is merged.
    monkeypatch.setattr(api, "fetch_terminations", lambda *a, **k: [])
    with pytest.raises(SystemExit, match="0 awards"):
        run_api(Namespace(lookback_days=120))


def test_an_empty_doge_fetch_refuses_to_publish(monkeypatch):
    # doge_claims.csv is its own cache, so writing an empty result would blank
    # the corpus AND the cache the next run reads.
    monkeypatch.setattr(doge, "fetch_claims", list)
    monkeypatch.setattr(doge, "enrich", lambda *a, **k: [])
    with pytest.raises(SystemExit, match="0 NASA claims"):
        run_doge(Namespace(refresh_days=14))
