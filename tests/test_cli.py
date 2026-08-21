"""The merge step publishes terminations.csv from the parts, offline.

Also the empty-output tripwires: every door and the merge itself refuse to
publish nothing, because none of these sources can legitimately go to zero.
"""

import json
from argparse import Namespace

import pytest

from nasatrack import api, doge
from nasatrack.cli import merge_step, run_api, run_doge
from nasatrack.schema import TerminationRow, read_csv, write_csv
from tests.test_merge import row
from tests.test_overrides import write_overrides


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
        ],
    )
    mirror_run = tmp_path / "parts" / "mirror_run.json"
    mirror_run.write_text(json.dumps({"ran_at": "2026-08-01T12:00:00+00:00", "rows": 2}))
    overrides = write_overrides(
        tmp_path / "dropped_award_status.csv",
        [("C", "vacated"), ("B", "still_terminated"), ("GONE", "continued")],
    )
    output = tmp_path / "terminations.csv"

    merge_step(
        api_part=api_part,
        mirror_part=mirror_part,
        mirror_run=mirror_run,
        overrides=overrides,
        output=output,
    )

    published = read_csv(output, TerminationRow)
    assert [(r.award_key, r.sources, r.override_status) for r in published] == [
        ("CONT_AWD_B", "api", "still_terminated"),
        ("CONT_AWD_A", "api;mirror", ""),
    ]

    captured = capsys.readouterr()
    assert captured.out.strip() == "terminations.csv: 2 rows (api 2, mirror 2, both 1)"
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
