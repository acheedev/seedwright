import os
import sqlite3
import tempfile
import unittest

from seedwright.dialects.sqlite import SQLiteDialect


DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id)
);
"""


class ApplyTests(unittest.TestCase):
    def _db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(DDL)
        conn.close()
        return path

    def test_validate_sqlite_script_rolls_back_throwaway_changes(self):
        path = self._db()
        sql = """
        INSERT INTO "customers" ("id", "email") VALUES (1, 'a@example.test');
        INSERT INTO "orders" ("id", "customer_id") VALUES (1, 1);
        """

        result = SQLiteDialect(":memory:").validate_script(
            path, ["customers", "orders"], sql
        )

        self.assertEqual(result.rows, 2)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        finally:
            conn.close()

    def test_validate_sqlite_script_reports_fk_failures(self):
        path = self._db()
        sql = 'INSERT INTO "orders" ("id", "customer_id") VALUES (1, 999);'

        with self.assertRaisesRegex(RuntimeError, "foreign-key violations"):
            SQLiteDialect(":memory:").validate_script(path, ["orders"], sql)

    def test_apply_sqlite_script_commits_to_target(self):
        path = self._db()
        sql = 'INSERT INTO "customers" ("id", "email") VALUES (1, "a@example.test");'

        SQLiteDialect(path).apply_script(sql)

        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
