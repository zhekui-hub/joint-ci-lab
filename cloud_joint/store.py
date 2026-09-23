"""Author: zhekui. Single-host durable SQLite transactions, not a distributed DB.

Store.transaction: serialize mutations across processes; audit: append evidence.
All agents for one live coordinator must use the same local database filesystem.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from .model import canonical


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, gid TEXT NOT NULL,
                    body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY, gid TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_runs(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_group ON tasks(gid);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(json_extract(body,'$.status'));
                CREATE INDEX IF NOT EXISTS tasks_clean ON tasks(json_extract(body,'$.clean'));
            ''')
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (canonical(
                dict(max_running=8, per_group=2, max_queued=10000, max_groups=2000)),))

    def configure(self, **limits):
        allowed = {"max_running", "per_group", "max_queued", "max_groups"}
        if not limits.keys() <= allowed or any(type(v) is not int or v < 1 for v in limits.values()):
            raise ValueError("limits must be positive integers")
        with self.transaction() as db:
            values = self.limits(db)
            values.update(limits)
            db.execute("UPDATE settings SET body=? WHERE id=1", (canonical(values),))

    @staticmethod
    def limits(db):
        return json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def get(db, table, key):
        if table not in {"groups", "tasks", "intents"}:
            raise ValueError("unknown table")
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, table, key, value):
        if table not in {"groups", "tasks", "intents"}:
            raise ValueError("unknown table")
        if table == "tasks":
            db.execute("INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (key, value["group"], canonical(value)))
        else:
            db.execute(f"INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (key, canonical(value)))

    @staticmethod
    def tasks(db, gid):
        return [json.loads(r[0]) for r in db.execute("SELECT body FROM tasks WHERE gid=? ORDER BY id", (gid,))]

    @staticmethod
    def audit(db, kind, body):
        db.execute("INSERT INTO audit(at,kind,body) VALUES(?,?,?)", (time.time(), kind, canonical(body)))

    def export(self):
        with self.transaction() as db:
            result = {}
            for name in ("groups", "tasks", "intents"):
                result[name] = [json.loads(r[0]) for r in db.execute(f"SELECT body FROM {name} ORDER BY id")]
            result["audit"] = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY seq")]
            return result
