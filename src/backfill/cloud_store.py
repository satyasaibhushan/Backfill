"""Durable hosted inbox and worker snapshots. Provider credentials stay on workers."""

import hashlib
import json
import secrets
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, text

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY, password TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires DOUBLE PRECISION NOT"
        " NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL,"
        " expires DOUBLE PRECISION NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS pairings (hash TEXT PRIMARY KEY, expires DOUBLE PRECISION NOT"
        " NULL, machine TEXT)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, slot INTEGER UNIQUE, name TEXT"
        " NOT NULL, hash TEXT NOT NULL UNIQUE, seen DOUBLE PRECISION NOT NULL, snapshot TEXT NOT "
        "NULL, revoked INTEGER NOT NULL DEFAULT 0)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, machine TEXT NOT NULL, payload"
        " TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, response TEXT, status INTEGER)"
    ),
    "CREATE INDEX IF NOT EXISTS commands_machine ON commands(machine, created)",
]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + value.hex()


class CloudStore:
    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1)
        with self.transaction() as db:
            for statement in SCHEMA:
                db.execute(text(statement))

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            # SQLite is only a local test/development backend.
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection

    def one(self, query: str, **params):
        with self.engine.connect() as db:
            row = db.execute(text(query), params).mappings().first()
            return dict(row) if row else None

    def rows(self, query: str, **params):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(text(query), params).mappings()]

    def throttle(self, key: str) -> bool:
        now = time.time()
        with self.transaction() as db:
            db.execute(text("DELETE FROM login_attempts WHERE expires<:now"), {"now": now})
            row = db.execute(
                text(
                    "INSERT INTO login_attempts(key,count,expires) VALUES (:key,1,:expires) "
                    "ON CONFLICT(key) DO UPDATE SET count=login_attempts.count+1 RETURNING "
                    "count"
                ),
                {"key": key, "expires": now + 300},
            ).first()
            return row[0] <= 20

    def queue(self, machine: str, command_id: str, payload: dict):
        encoded = json.dumps(payload, sort_keys=True)
        with self.transaction() as db:
            db.execute(
                text(
                    "INSERT INTO commands(id,machine,payload,created) VALUES "
                    "(:id,:machine,:payload,:created) ON CONFLICT(id) DO NOTHING"
                ),
                {"id": command_id, "machine": machine, "payload": encoded, "created": time.time()},
            )
            row = dict(
                db.execute(text("SELECT * FROM commands WHERE id=:id"), {"id": command_id})
                .mappings()
                .one()
            )
            if row["machine"] != machine or row["payload"] != encoded:
                raise ValueError("Request ID already belongs to another change")
            return row
