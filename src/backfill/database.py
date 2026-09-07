import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    key TEXT PRIMARY KEY, policy TEXT NOT NULL, observation TEXT
);
CREATE TABLE IF NOT EXISTS workloads (
    key TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(key),
    priority INTEGER NOT NULL, paused INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY, workload TEXT NOT NULL REFERENCES workloads(key),
    request_id TEXT NOT NULL, request TEXT NOT NULL, costs TEXT NOT NULL,
    consumed TEXT NOT NULL, epochs TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, state TEXT NOT NULL,
    UNIQUE(workload, request_id)
);
CREATE TABLE IF NOT EXISTS reports (
    grant_id TEXT NOT NULL REFERENCES grants(id), report_id TEXT NOT NULL,
    payload TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(grant_id, report_id)
);
CREATE TABLE IF NOT EXISTS debits (
    id INTEGER PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id),
    account TEXT NOT NULL, window TEXT NOT NULL, epoch REAL NOT NULL,
    amount REAL NOT NULL, at REAL NOT NULL, estimated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS debits_account ON debits(account, window, epoch, at);
CREATE TABLE IF NOT EXISTS demand (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, window TEXT NOT NULL,
    unit TEXT NOT NULL, capacity REAL NOT NULL, at REAL NOT NULL,
    duration REAL NOT NULL, amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS demand_account ON demand(account, window, at);
CREATE TABLE IF NOT EXISTS meters (
    account TEXT PRIMARY KEY REFERENCES accounts(key), provider TEXT NOT NULL,
    checked_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS meter_readings (
    account TEXT PRIMARY KEY REFERENCES accounts(key), observation TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS projects (key TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_budgets (
    workload TEXT PRIMARY KEY REFERENCES workloads(key),
    project TEXT REFERENCES projects(key), config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS native_runs (
    id TEXT PRIMARY KEY, workload TEXT NOT NULL REFERENCES workloads(key),
    provider TEXT NOT NULL, request_id TEXT NOT NULL,
    allocated INTEGER NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, cost_limit REAL NOT NULL,
    state TEXT NOT NULL, reason TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT -1,
    created_at REAL NOT NULL, heartbeat_at REAL NOT NULL, expires_at REAL NOT NULL,
    session_id TEXT, last_report TEXT,
    UNIQUE(workload, request_id)
);
CREATE INDEX IF NOT EXISTS native_runs_workload ON native_runs(workload, state);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._active: ContextVar[sqlite3.Connection | None] = ContextVar(
            "transaction", default=None
        )
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("unsupported quota database version")
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version=2")

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        with self.transaction() as connection:
            token = self._active.set(connection)
            try:
                yield connection
            finally:
                self._active.reset(token)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = self._active.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
