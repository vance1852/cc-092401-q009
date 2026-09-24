from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from robot_trials.storage import connect, initialize, inspect_schema, transaction


class StorageTests(unittest.TestCase):
    def test_initialize_is_repeatable(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
        finally:
            connection.close()
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(summary["schema_version"], "3")

    def test_transaction_rolls_back_on_error(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with self.assertRaises(RuntimeError):
            with transaction(connection):
                connection.execute("INSERT INTO items(value) VALUES('x')")
                raise RuntimeError("stop")
        count = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_connect_enables_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "test.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()

    def test_migrates_v2_database_without_data_loss(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '2');
            CREATE TABLE protocol_catalog(
                protocol_id TEXT NOT NULL, version INTEGER NOT NULL,
                title TEXT NOT NULL, task_family TEXT NOT NULL,
                canonical_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(protocol_id, version), UNIQUE(content_sha256)
            );
            """
        )
        connection.execute(
            "INSERT INTO protocol_catalog VALUES('p1', 1, 't', 'f', '{}', ?, '2026-01-01T00:00:00Z')",
            ("a" * 64,),
        )
        try:
            initialize(connection)
            summary = inspect_schema(connection)
            row = connection.execute(
                "SELECT status,retire_reason FROM protocol_catalog WHERE protocol_id='p1'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(summary["schema_version"], "3")
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["retire_reason"])


if __name__ == "__main__":
    unittest.main()
