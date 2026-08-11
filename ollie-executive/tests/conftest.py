import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest

from ollie_executive import ExecutiveLedger, connect, migrate


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "executive.db")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def ledger(conn):
    return ExecutiveLedger(conn, clock=lambda: "2026-07-10T12:00:00Z")

