import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

import seedwright.dialects.postgres as postgres_module
from seedwright.dialects.postgres import (
    _COLUMNS_SQL,
    _FK_SQL,
    _PK_SQL,
    _TABLES_SQL,
    _UNIQUE_SQL,
    PostgresDialect,
    assemble_schema,
    normalize_postgres_type,
)


class PostgresTypeMappingTests(unittest.TestCase):
    def test_integer_types(self):
        self.assertEqual(normalize_postgres_type("integer", "int4", None, None), ("integer", None))
        self.assertEqual(normalize_postgres_type("bigint", "int8", None, None), ("integer", None))

    def test_numeric_scale_zero_is_integer(self):
        self.assertEqual(normalize_postgres_type("numeric", "numeric", 0, None), ("integer", None))

    def test_numeric_with_scale_is_numeric(self):
        self.assertEqual(normalize_postgres_type("numeric", "numeric", 2, None), ("numeric", None))

    def test_text_types_keep_length(self):
        self.assertEqual(
            normalize_postgres_type("character varying", "varchar", None, 40),
            ("text", 40),
        )

    def test_temporal_boolean_and_blob_types(self):
        self.assertEqual(normalize_postgres_type("boolean", "bool", None, None), ("boolean", None))
        self.assertEqual(normalize_postgres_type("date", "date", None, None), ("date", None))
        self.assertEqual(
            normalize_postgres_type("timestamp without time zone", "timestamp", None, None),
            ("datetime", None),
        )
        self.assertEqual(normalize_postgres_type("bytea", "bytea", None, None), ("blob", None))


class PostgresAssembleTests(unittest.TestCase):
    def test_builds_columns_with_pk_fk_unique(self):
        schema = assemble_schema(
            ["customers", "orders"],
            [
                ("customers", "id", "integer", "int4", "NO", None, 32, 0),
                ("customers", "email", "character varying", "varchar", "NO", 120, None, None),
                ("orders", "id", "integer", "int4", "NO", None, 32, 0),
                ("orders", "customer_id", "integer", "int4", "NO", None, 32, 0),
                ("orders", "opened_at", "timestamp without time zone", "timestamp", "YES", None, None, None),
            ],
            [("customers", "id"), ("orders", "id")],
            [("customers", "email")],
            [("orders_customer_id_fkey", 1, "orders", "customer_id", "customers", "id")],
        )

        self.assertTrue(schema["customers"].column("id").primary_key)
        self.assertFalse(schema["customers"].column("id").nullable)
        self.assertTrue(schema["customers"].column("email").unique)
        self.assertEqual(schema["customers"].column("email").max_length, 120)
        fk = schema["orders"].column("customer_id").foreign_key
        self.assertEqual((fk.ref_table, fk.ref_column), ("customers", "id"))
        self.assertEqual(schema["orders"].column("opened_at").type, "datetime")

    def test_builds_composite_foreign_key_group(self):
        schema = assemble_schema(
            ["accounts", "invoices"],
            [
                ("accounts", "region_code", "text", "text", "NO", None, None, None),
                ("accounts", "account_no", "integer", "int4", "NO", None, 32, 0),
                ("invoices", "region_code", "text", "text", "NO", None, None, None),
                ("invoices", "account_no", "integer", "int4", "NO", None, 32, 0),
            ],
            [("accounts", "region_code"), ("accounts", "account_no")],
            [],
            [
                ("invoices_account_fkey", 1, "invoices", "region_code", "accounts", "region_code"),
                ("invoices_account_fkey", 2, "invoices", "account_no", "accounts", "account_no"),
            ],
        )

        groups = schema["invoices"].foreign_key_groups
        self.assertEqual(len(groups), 1)
        self.assertEqual([c.name for c in groups[0]], ["region_code", "account_no"])


class PostgresLiteralTests(unittest.TestCase):
    def setUp(self):
        self.d = PostgresDialect("configured-dsn")

    def test_literals(self):
        self.assertEqual(self.d.quote_literal(None), "NULL")
        self.assertEqual(self.d.quote_literal("O'Brien"), "'O''Brien'")
        self.assertEqual(self.d.quote_literal(True), "TRUE")
        self.assertEqual(self.d.quote_literal(False), "FALSE")
        self.assertEqual(self.d.quote_literal(Decimal("123.45")), "123.45")
        self.assertEqual(self.d.quote_literal(dt.date(2023, 5, 1)), "DATE '2023-05-01'")
        self.assertEqual(
            self.d.quote_literal(dt.datetime(2023, 5, 1, 13, 45)),
            "TIMESTAMP '2023-05-01 13:45:00'",
        )
        self.assertEqual(self.d.quote_literal(b"\x00\xff"), "'\\x00ff'::bytea")

    def test_identifier_quoting(self):
        self.assertEqual(self.d.quote_identifier('weird"name'), '"weird""name"')


class FakePostgresCursor:
    def __init__(self, rows_by_call):
        self.rows_by_call = rows_by_call
        self.calls = []
        self.fetchone_values = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return iter(self.rows_by_call.get((sql, params), []))

    def fetchone(self):
        return self.fetchone_values.pop(0)


class FakePostgresConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakePostgresModule:
    def __init__(self, conn):
        self.conn = conn
        self.connect_calls = []

    def connect(self, dsn):
        self.connect_calls.append(dsn)
        return self.conn


class PostgresIntrospectionTests(unittest.TestCase):
    def test_introspect_consumes_catalog_rows_into_schema_contract(self):
        rows_by_call = {
            (_TABLES_SQL, ("app",)): [("customers",), ("orders",)],
            (_COLUMNS_SQL, ("app",)): [
                ("customers", "id", "integer", "int4", "NO", None, 32, 0),
                ("customers", "email", "character varying", "varchar", "NO", 120, None, None),
                ("orders", "id", "integer", "int4", "NO", None, 32, 0),
                ("orders", "customer_id", "integer", "int4", "NO", None, 32, 0),
            ],
            (_PK_SQL, ("app",)): [("customers", "id"), ("orders", "id")],
            (_UNIQUE_SQL, ("app",)): [("customers_email_key", "customers", "email")],
            (_FK_SQL, ("app",)): [
                ("orders_customer_id_fkey", 1, "orders", "customer_id", "customers", "id"),
            ],
        }
        cursor = FakePostgresCursor(rows_by_call)
        conn = FakePostgresConnection(cursor)
        fake_psycopg = FakePostgresModule(conn)

        with patch.object(postgres_module, "psycopg", fake_psycopg):
            schema = PostgresDialect("configured-dsn", schema="app").introspect()

        self.assertTrue(conn.closed)
        self.assertEqual(fake_psycopg.connect_calls, ["configured-dsn"])
        self.assertTrue(schema["customers"].column("id").primary_key)
        self.assertTrue(schema["customers"].column("email").unique)
        fk = schema["orders"].column("customer_id").foreign_key
        self.assertEqual((fk.ref_table, fk.ref_column), ("customers", "id"))


if __name__ == "__main__":
    unittest.main()
