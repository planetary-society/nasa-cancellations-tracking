"""The mirror's fail-loud contract: only the CONNECT is "unavailable".

`psycopg.errors.QueryCanceled` - what a `statement_timeout` raises - subclasses
`OperationalError`, so catching the query phase alongside the connect would
report a timed-out 10-minute query as an absent mirror and exit the run 0. These
tests run against a fake psycopg installed into `sys.modules`, which is reachable
because `mirror._cursor` imports the driver at call time rather than on import.
"""

import sys
from types import ModuleType

import pytest

from nasatrack import mirror


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.raises is not None:
            raise self.conn.raises


class FakeConnection:
    def __init__(self, raises=None):
        self.raises = raises
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def install_fake_psycopg(monkeypatch, connect):
    """A psycopg whose exception hierarchy matches the real one."""
    psycopg = ModuleType("psycopg")

    class OperationalError(Exception):
        pass

    class QueryCanceled(OperationalError):
        """SQLSTATE 57014, exactly as psycopg models it: an OperationalError."""

    psycopg.OperationalError = OperationalError
    psycopg.connect = connect
    rows = ModuleType("psycopg.rows")
    rows.dict_row = dict
    psycopg.rows = rows
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    # Credentials, so `_cursor` gets as far as connecting.
    monkeypatch.setenv(
        mirror.DSN_ENV_VAR, "postgresql://user:pass@mirror.local:5432/data_store_api"
    )
    return QueryCanceled


def test_an_unreachable_host_is_reported_as_unavailable(monkeypatch):
    def connect(*args, **kwargs):
        raise sys.modules["psycopg"].OperationalError("could not connect to server")

    install_fake_psycopg(monkeypatch, connect)
    with pytest.raises(mirror.LocalMirrorUnavailableError), mirror._cursor(120):
        pass


def test_a_statement_timeout_is_not_reported_as_unavailable(monkeypatch):
    # The whole point: a query that blew its timeout must fail the run, not
    # quietly skip the door and let CI publish yesterday's part as today's.
    conn = FakeConnection()
    query_canceled = install_fake_psycopg(monkeypatch, lambda *a, **k: conn)
    conn.raises = query_canceled("canceling statement due to statement timeout")

    with pytest.raises(query_canceled), mirror._cursor(120):
        pass
    assert conn.closed


def test_a_healthy_connection_yields_a_cursor(monkeypatch):
    conn = FakeConnection()
    install_fake_psycopg(monkeypatch, lambda *a, **k: conn)
    with mirror._cursor(600) as cur:
        assert cur.conn is conn
    assert conn.closed
