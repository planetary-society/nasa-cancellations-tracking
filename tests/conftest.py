import csv
import os

import pytest


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An isolated repo-shaped cwd.

    Every module under test uses paths relative to the working directory, so
    tests must never run against the real consolidated/ or verification/.
    """
    monkeypatch.chdir(tmp_path)
    os.makedirs("consolidated")
    os.makedirs("verification")
    return tmp_path


@pytest.fixture
def write_csv():
    def _write(path, fieldnames, rows):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    return _write
