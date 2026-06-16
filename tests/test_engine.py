import sqlite3
import unittest

from seedwright.dialects import SQLiteDialect
from seedwright.emit import to_sql
from seedwright.engine import GenerationEngine

DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    full_name VARCHAR(80) NOT NULL,
    email VARCHAR(120) NOT NULL UNIQUE
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status VARCHAR(20) NOT NULL
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    quantity INTEGER NOT NULL
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    full_name VARCHAR(80) NOT NULL,
    manager_id INTEGER REFERENCES employees(id)
);
"""

COMPOSITE_DDL = """
CREATE TABLE accounts (
    region_code TEXT NOT NULL,
    account_no INTEGER NOT NULL,
    label VARCHAR(80) NOT NULL,
    PRIMARY KEY (region_code, account_no)
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    region_code TEXT NOT NULL,
    account_no INTEGER NOT NULL,
    invoice_no INTEGER NOT NULL,
    FOREIGN KEY (region_code, account_no)
        REFERENCES accounts(region_code, account_no)
);
CREATE TABLE invoice_notes (
    region_code TEXT NOT NULL,
    account_no INTEGER NOT NULL,
    invoice_no INTEGER NOT NULL,
    note VARCHAR(80) NOT NULL,
    PRIMARY KEY (region_code, account_no, invoice_no),
    FOREIGN KEY (region_code, account_no)
        REFERENCES accounts(region_code, account_no)
);
"""

UNIQUE_TYPES_DDL = """
CREATE TABLE unique_values (
    id INTEGER PRIMARY KEY,
    serial_no INTEGER NOT NULL UNIQUE,
    opened_on DATE NOT NULL UNIQUE,
    is_primary BOOLEAN NOT NULL UNIQUE
);
"""

LENGTH_DDL = """
CREATE TABLE constrained_text (
    id INTEGER PRIMARY KEY,
    priority_code VARCHAR(4) NOT NULL UNIQUE CHECK(length(priority_code) <= 4),
    email VARCHAR(16) NOT NULL UNIQUE CHECK(length(email) <= 16),
    status VARCHAR(6) NOT NULL CHECK(length(status) <= 6),
    note VARCHAR(5) NOT NULL CHECK(length(note) <= 5)
);
"""

BLOB_DDL = """
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    payload BLOB NOT NULL
);
"""

REQUIRED_SELF_FK_DDL = """
CREATE TABLE employees_strict (
    id INTEGER PRIMARY KEY,
    full_name VARCHAR(80) NOT NULL,
    manager_id INTEGER NOT NULL REFERENCES employees_strict(id)
);
"""

COMPOSITE_SELF_FK_DDL = """
CREATE TABLE category_paths (
    tenant_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    parent_tenant_id INTEGER NOT NULL,
    parent_category_id INTEGER NOT NULL,
    label VARCHAR(80) NOT NULL,
    PRIMARY KEY (tenant_id, category_id),
    FOREIGN KEY (parent_tenant_id, parent_category_id)
        REFERENCES category_paths(tenant_id, category_id)
);
"""

CYCLE_DDL = """
CREATE TABLE a (
    id INTEGER PRIMARY KEY,
    b_id INTEGER REFERENCES b(id),
    label VARCHAR(80) NOT NULL
);
CREATE TABLE b (
    id INTEGER PRIMARY KEY,
    a_id INTEGER NOT NULL REFERENCES a(id),
    label VARCHAR(80) NOT NULL
);
"""


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.src = self._connect()
        self.src.executescript(DDL)
        # introspect from a file-less db by pointing the dialect at a temp file
        self.path = ":memory:"

    def _connect(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        return conn

    def _schema_from(self, conn):
        # reuse the dialect's table reader against an existing connection
        d = SQLiteDialect.__new__(SQLiteDialect)
        from seedwright.model import Schema
        schema = Schema()
        conn.row_factory = sqlite3.Row
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for n in names:
            schema.add(d._read_table(conn, n))
        return schema

    def test_generated_data_loads_with_fk_enforcement(self):
        schema = self._schema_from(self.src)
        engine = GenerationEngine(
            schema,
            per_table={"customers": 10, "orders": 25,
                       "order_items": 40, "employees": 6},
            seed=11,
        )
        data = engine.generate()
        sql = to_sql(schema, data, SQLiteDialect(self.path))

        sink = self._connect()
        sink.execute("PRAGMA foreign_keys = ON")
        sink.executescript(DDL)
        sink.executescript(sql)
        sink.commit()

        violations = sink.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(violations, [])
        self.assertEqual(
            sink.execute("SELECT COUNT(*) FROM order_items").fetchone()[0], 40)

    def test_row_counts_respected(self):
        schema = self._schema_from(self.src)
        data = GenerationEngine(schema, default_rows=7, seed=1).generate()
        self.assertEqual(len(data["customers"]), 7)
        self.assertEqual(len(data["employees"]), 7)

    def test_nonnull_fk_always_points_at_real_parent(self):
        schema = self._schema_from(self.src)
        data = GenerationEngine(
            schema, per_table={"customers": 5, "orders": 50}, seed=2).generate()
        customer_ids = {r["id"] for r in data["customers"]}
        for o in data["orders"]:
            self.assertIn(o["customer_id"], customer_ids)

    def test_composite_foreign_keys_draw_from_same_parent_row(self):
        src = self._connect()
        src.executescript(COMPOSITE_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(
            schema,
            per_table={"accounts": 8, "invoices": 30, "invoice_notes": 20},
            seed=12,
        ).generate()
        sql = to_sql(schema, data, SQLiteDialect(self.path))

        sink = self._connect()
        sink.execute("PRAGMA foreign_keys = ON")
        sink.executescript(COMPOSITE_DDL)
        sink.executescript(sql)

        violations = sink.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(violations, [])

        account_keys = {
            (r["region_code"], r["account_no"]) for r in data["accounts"]
        }
        for row in data["invoices"]:
            self.assertIn((row["region_code"], row["account_no"]), account_keys)

    def test_composite_primary_key_values_are_unique(self):
        src = self._connect()
        src.executescript(COMPOSITE_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(
            schema,
            per_table={"accounts": 8, "invoice_notes": 25},
            seed=15,
        ).generate()

        note_keys = [
            (r["region_code"], r["account_no"], r["invoice_no"])
            for r in data["invoice_notes"]
        ]
        self.assertEqual(len(note_keys), len(set(note_keys)))

    def test_unique_columns_are_enforced_for_non_text_types(self):
        src = self._connect()
        src.executescript(UNIQUE_TYPES_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(schema, default_rows=2, seed=6).generate()
        rows = data["unique_values"]

        for col in ("serial_no", "opened_on", "is_primary"):
            values = [r[col] for r in rows]
            self.assertEqual(len(values), len(set(values)))

        sql = to_sql(schema, data, SQLiteDialect(self.path))
        sink = self._connect()
        sink.executescript(UNIQUE_TYPES_DDL)
        sink.executescript(sql)

    def test_unique_tracking_is_scoped_per_table_column(self):
        from seedwright.model import Column, Schema, Table

        schema = Schema()
        schema.add(Table("left_codes", [
            Column("id", type="integer", primary_key=True, nullable=False),
            Column("code", type="integer", nullable=False, unique=True),
        ]))
        schema.add(Table("right_codes", [
            Column("id", type="integer", primary_key=True, nullable=False),
            Column("code", type="integer", nullable=False, unique=True),
        ]))

        class RowIndexFactory:
            def one(self, column, row_index):
                return row_index

            def batch(self, column, n):
                return list(range(n))

            def distinct_values(self, column, n):
                return list(range(n))

        engine = GenerationEngine(schema, default_rows=3, seed=1)
        engine.factory = RowIndexFactory()
        data = engine.generate()

        self.assertEqual([r["code"] for r in data["left_codes"]], [0, 1, 2])
        self.assertEqual([r["code"] for r in data["right_codes"]], [0, 1, 2])

    def test_sqlite_decimal_precision_and_scale_are_introspected(self):
        src = self._connect()
        src.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, price DECIMAL(6,2) NOT NULL)")
        schema = self._schema_from(src)

        price = schema["products"].column("price")
        self.assertEqual(price.type, "numeric")
        self.assertEqual(price.numeric_precision, 6)
        self.assertEqual(price.numeric_scale, 2)

    def test_generated_text_respects_declared_lengths(self):
        src = self._connect()
        src.executescript(LENGTH_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(schema, default_rows=3, seed=9).generate()
        for row in data["constrained_text"]:
            for col in schema["constrained_text"].columns:
                if col.max_length is not None:
                    self.assertLessEqual(len(row[col.name]), col.max_length)

        sql = to_sql(schema, data, SQLiteDialect(self.path))
        sink = self._connect()
        sink.executescript(LENGTH_DDL)
        sink.executescript(sql)

    def test_not_null_blob_loads_successfully(self):
        src = self._connect()
        src.executescript(BLOB_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(schema, default_rows=3, seed=10).generate()
        self.assertTrue(all(isinstance(r["payload"], bytes) for r in data["attachments"]))

        sql = to_sql(schema, data, SQLiteDialect(self.path))
        sink = self._connect()
        sink.executescript(BLOB_DDL)
        sink.executescript(sql)
        self.assertEqual(
            sink.execute("SELECT COUNT(*) FROM attachments WHERE payload IS NOT NULL").fetchone()[0],
            3,
        )

    def test_required_self_fk_first_row_references_itself(self):
        src = self._connect()
        src.executescript(REQUIRED_SELF_FK_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(schema, default_rows=4, seed=13).generate()
        first = data["employees_strict"][0]
        self.assertEqual(first["manager_id"], first["id"])

        sql = to_sql(schema, data, SQLiteDialect(self.path))
        sink = self._connect()
        sink.execute("PRAGMA foreign_keys = ON")
        sink.executescript(REQUIRED_SELF_FK_DDL)
        sink.executescript(sql)
        self.assertEqual(sink.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_required_composite_self_fk_first_row_references_itself(self):
        src = self._connect()
        src.executescript(COMPOSITE_SELF_FK_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(schema, default_rows=4, seed=14).generate()
        first = data["category_paths"][0]
        self.assertEqual(first["parent_tenant_id"], first["tenant_id"])
        self.assertEqual(first["parent_category_id"], first["category_id"])

        sql = to_sql(schema, data, SQLiteDialect(self.path))
        sink = self._connect()
        sink.execute("PRAGMA foreign_keys = ON")
        sink.executescript(COMPOSITE_SELF_FK_DDL)
        sink.executescript(sql)
        self.assertEqual(sink.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_nullable_cross_table_cycle_is_resolved_with_second_pass_update(self):
        src = self._connect()
        src.executescript(CYCLE_DDL)
        schema = self._schema_from(src)

        data = GenerationEngine(
            schema,
            per_table={"a": 4, "b": 4},
            seed=16,
        ).generate()
        sql = to_sql(schema, data, SQLiteDialect(self.path))

        self.assertEqual(sql.count("UPDATE "), 4)
        self.assertTrue(all(row["b_id"] is None for row in data["a"]))

        sink = self._connect()
        sink.execute("PRAGMA foreign_keys = ON")
        sink.executescript(CYCLE_DDL)
        sink.executescript(sql)

        violations = sink.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(violations, [])
        self.assertEqual(
            sink.execute("SELECT COUNT(*) FROM a WHERE b_id IS NOT NULL").fetchone()[0],
            4,
        )


if __name__ == "__main__":
    unittest.main()
