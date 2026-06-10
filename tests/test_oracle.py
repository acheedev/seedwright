import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

import seedwright.dialects.oracle as oracle_module
from seedwright.dialects.oracle import (
    _COLUMNS_SQL,
    _FK_SQL,
    _PK_SQL,
    _TABLES_SQL,
    _UNIQUE_SQL,
    OracleDialect,
    assemble_schema,
    normalize_oracle_type,
    should_treat_number_as_boolean,
    should_treat_number_as_integer_id,
)
from seedwright.engine import GenerationEngine
from seedwright.emit import to_sql
from seedwright.model import Column, Schema, Table


class OracleTypeMappingTests(unittest.TestCase):
    def test_number_scale_zero_is_integer(self):
        self.assertEqual(normalize_oracle_type("NUMBER", 10, 0, None), ("integer", None))

    def test_number_with_scale_is_numeric(self):
        self.assertEqual(normalize_oracle_type("NUMBER", 10, 2, None), ("numeric", None))

    def test_unconstrained_number_is_numeric(self):
        self.assertEqual(normalize_oracle_type("NUMBER", None, None, None), ("numeric", None))

    def test_unconstrained_number_ids_are_integerish_after_context(self):
        self.assertTrue(
            should_treat_number_as_integer_id("NUMBER", None, None, "ID", True, False)
        )
        self.assertTrue(
            should_treat_number_as_integer_id("NUMBER", None, None, "CUSTOMER_ID", False, True)
        )
        self.assertFalse(
            should_treat_number_as_integer_id("NUMBER", None, None, "AMOUNT", False, False)
        )

    def test_number_one_boolean_flag_names_are_booleanish_after_context(self):
        self.assertTrue(should_treat_number_as_boolean("NUMBER", 1, 0, "IS_ACTIVE"))
        self.assertTrue(should_treat_number_as_boolean("NUMBER", 1, None, "HAS_ATTACHMENTS"))
        self.assertTrue(should_treat_number_as_boolean("NUMBER", 1, 0, "ENABLED"))
        self.assertFalse(should_treat_number_as_boolean("NUMBER", 1, 0, "PRIORITY_CODE"))
        self.assertFalse(should_treat_number_as_boolean("NUMBER", 2, 0, "IS_ACTIVE"))

    def test_varchar2_keeps_length(self):
        self.assertEqual(normalize_oracle_type("VARCHAR2", None, None, 40), ("text", 40))

    def test_date_is_datetime(self):
        # Oracle DATE carries a time component
        self.assertEqual(normalize_oracle_type("DATE", None, None, None), ("datetime", None))

    def test_timestamp_variants_are_datetime(self):
        self.assertEqual(
            normalize_oracle_type("TIMESTAMP(6)", None, None, None), ("datetime", None))

    def test_float_is_real(self):
        self.assertEqual(normalize_oracle_type("BINARY_DOUBLE", None, None, None), ("real", None))

    def test_blob_is_blob(self):
        self.assertEqual(normalize_oracle_type("BLOB", None, None, None), ("blob", None))

    def test_boolean_is_boolean(self):
        self.assertEqual(normalize_oracle_type("BOOLEAN", None, None, None), ("boolean", None))


class OracleAssembleTests(unittest.TestCase):
    def test_builds_columns_with_pk_fk_unique(self):
        tables = ["CUSTOMERS", "ORDERS"]
        columns = [
            ("CUSTOMERS", "ID", "NUMBER", 38, 0, "N", None),
            ("CUSTOMERS", "EMAIL", "VARCHAR2", None, None, "N", 120),
            ("ORDERS", "ID", "NUMBER", 38, 0, "N", None),
            ("ORDERS", "CUSTOMER_ID", "NUMBER", 38, 0, "N", None),
            ("ORDERS", "OPENED_AT", "DATE", None, None, "Y", None),
        ]
        pks = [("CUSTOMERS", "ID"), ("ORDERS", "ID")]
        uniques = [("CUSTOMERS", "EMAIL")]
        fks = [("ORDERS", "CUSTOMER_ID", "CUSTOMERS", "ID")]

        schema = assemble_schema(tables, columns, pks, uniques, fks)

        cust = schema["CUSTOMERS"]
        self.assertTrue(cust.column("ID").primary_key)
        self.assertFalse(cust.column("ID").nullable)  # PKs forced not-null
        self.assertEqual(cust.column("ID").numeric_precision, 38)
        self.assertEqual(cust.column("ID").numeric_scale, 0)
        self.assertTrue(cust.column("EMAIL").unique)
        self.assertEqual(cust.column("EMAIL").max_length, 120)

        orders = schema["ORDERS"]
        fk = orders.column("CUSTOMER_ID").foreign_key
        self.assertIsNotNone(fk)
        self.assertEqual((fk.ref_table, fk.ref_column), ("CUSTOMERS", "ID"))
        self.assertEqual(orders.column("OPENED_AT").type, "datetime")
        self.assertEqual(orders.column("OPENED_AT").native_type, "DATE")

    def test_boolean_native_type_is_preserved(self):
        schema = assemble_schema(
            ["FEATURE_FLAGS"],
            [("FEATURE_FLAGS", "ENABLED", "BOOLEAN", None, None, "N", None)],
            [],
            [],
            [],
        )

        col = schema["FEATURE_FLAGS"].column("ENABLED")
        self.assertEqual(col.type, "boolean")
        self.assertEqual(col.native_type, "BOOLEAN")

    def test_number_precision_and_scale_are_preserved(self):
        tables = ["PRODUCTS"]
        columns = [
            ("PRODUCTS", "PRICE", "NUMBER", 5, 2, "N", None),
            ("PRODUCTS", "QTY", "NUMBER", 3, 0, "N", None),
        ]

        schema = assemble_schema(tables, columns, [], [], [])

        price = schema["PRODUCTS"].column("PRICE")
        self.assertEqual(price.type, "numeric")
        self.assertEqual(price.numeric_precision, 5)
        self.assertEqual(price.numeric_scale, 2)

        qty = schema["PRODUCTS"].column("QTY")
        self.assertEqual(qty.type, "integer")
        self.assertEqual(qty.numeric_precision, 3)
        self.assertEqual(qty.numeric_scale, 0)

    def test_bare_number_primary_and_foreign_keys_are_integer_columns(self):
        schema = assemble_schema(
            ["CUSTOMERS", "ORDERS"],
            [
                ("CUSTOMERS", "ID", "NUMBER", None, None, "N", None),
                ("ORDERS", "ID", "NUMBER", None, None, "N", None),
                ("ORDERS", "CUSTOMER_ID", "NUMBER", None, None, "N", None),
                ("ORDERS", "AMOUNT", "NUMBER", None, None, "N", None),
            ],
            [("CUSTOMERS", "ID"), ("ORDERS", "ID")],
            [],
            [("ORDERS", "CUSTOMER_ID", "CUSTOMERS", "ID")],
        )

        self.assertEqual(schema["CUSTOMERS"].column("ID").type, "integer")
        self.assertEqual(schema["ORDERS"].column("ID").type, "integer")
        self.assertEqual(schema["ORDERS"].column("CUSTOMER_ID").type, "integer")
        self.assertEqual(schema["ORDERS"].column("AMOUNT").type, "numeric")

    def test_bare_number_key_columns_generate_integer_values(self):
        schema = assemble_schema(
            ["CUSTOMERS", "ORDERS"],
            [
                ("CUSTOMERS", "ID", "NUMBER", None, None, "N", None),
                ("ORDERS", "ID", "NUMBER", None, None, "N", None),
                ("ORDERS", "CUSTOMER_ID", "NUMBER", None, None, "N", None),
            ],
            [("CUSTOMERS", "ID"), ("ORDERS", "ID")],
            [],
            [("ORDERS", "CUSTOMER_ID", "CUSTOMERS", "ID")],
        )

        data = GenerationEngine(schema, default_rows=3, seed=1).generate()

        self.assertEqual([r["ID"] for r in data["CUSTOMERS"]], [1, 2, 3])
        self.assertTrue(all(type(r["CUSTOMER_ID"]) is int for r in data["ORDERS"]))

    def test_number_one_flag_columns_generate_booleans(self):
        schema = assemble_schema(
            ["SUPPORT_GROUPS"],
            [
                ("SUPPORT_GROUPS", "ID", "NUMBER", None, None, "N", None),
                ("SUPPORT_GROUPS", "IS_ACTIVE", "NUMBER", 1, 0, "N", None),
                ("SUPPORT_GROUPS", "PRIORITY_CODE", "NUMBER", 1, 0, "N", None),
            ],
            [("SUPPORT_GROUPS", "ID")],
            [],
            [],
        )

        self.assertEqual(schema["SUPPORT_GROUPS"].column("IS_ACTIVE").type, "boolean")
        self.assertEqual(schema["SUPPORT_GROUPS"].column("PRIORITY_CODE").type, "integer")

        data = GenerationEngine(schema, default_rows=20, seed=1).generate()

        self.assertTrue(
            all(type(row["IS_ACTIVE"]) is bool for row in data["SUPPORT_GROUPS"])
        )
        self.assertTrue(
            all(0 <= row["PRIORITY_CODE"] <= 9 for row in data["SUPPORT_GROUPS"])
        )

    def test_builds_composite_foreign_key_group(self):
        tables = ["ACCOUNTS", "INVOICES"]
        columns = [
            ("ACCOUNTS", "REGION_CODE", "VARCHAR2", None, None, "N", 10),
            ("ACCOUNTS", "ACCOUNT_NO", "NUMBER", 38, 0, "N", None),
            ("INVOICES", "REGION_CODE", "VARCHAR2", None, None, "N", 10),
            ("INVOICES", "ACCOUNT_NO", "NUMBER", 38, 0, "N", None),
            ("INVOICES", "INVOICE_NO", "NUMBER", 38, 0, "N", None),
        ]
        pks = [
            ("ACCOUNTS", "REGION_CODE"),
            ("ACCOUNTS", "ACCOUNT_NO"),
            ("INVOICES", "REGION_CODE"),
            ("INVOICES", "ACCOUNT_NO"),
            ("INVOICES", "INVOICE_NO"),
        ]
        fks = [
            ("INVOICES_ACCOUNT_FK", 1, "INVOICES", "REGION_CODE", "ACCOUNTS", "REGION_CODE"),
            ("INVOICES_ACCOUNT_FK", 2, "INVOICES", "ACCOUNT_NO", "ACCOUNTS", "ACCOUNT_NO"),
        ]

        schema = assemble_schema(tables, columns, pks, [], fks)
        groups = schema["INVOICES"].foreign_key_groups

        self.assertEqual(len(groups), 1)
        self.assertEqual([c.name for c in groups[0]], ["REGION_CODE", "ACCOUNT_NO"])
        self.assertEqual(
            [c.foreign_key.ref_column for c in groups[0]],
            ["REGION_CODE", "ACCOUNT_NO"],
        )


class OracleLiteralTests(unittest.TestCase):
    def setUp(self):
        self.d = OracleDialect("u", "p", "configured-dsn")

    def test_none_is_null(self):
        self.assertEqual(self.d.quote_literal(None), "NULL")

    def test_string_is_escaped(self):
        self.assertEqual(self.d.quote_literal("O'Brien"), "'O''Brien'")

    def test_datetime_uses_to_date(self):
        v = dt.datetime(2023, 5, 1, 13, 45, 0)
        self.assertEqual(
            self.d.quote_literal(v),
            "TO_DATE('2023-05-01 13:45:00', 'YYYY-MM-DD HH24:MI:SS')",
        )

    def test_date_uses_ansi_literal(self):
        self.assertEqual(self.d.quote_literal(dt.date(2023, 5, 1)), "DATE '2023-05-01'")

    def test_decimal_is_numeric_literal(self):
        self.assertEqual(self.d.quote_literal(Decimal("123.45")), "123.45")

    def test_blob_is_hex_literal(self):
        self.assertEqual(self.d.quote_literal(b"\x00\xff"), "HEXTORAW('00FF')")

    def test_plain_bool_keeps_numeric_literal(self):
        self.assertEqual(self.d.quote_literal(True), "1")
        self.assertEqual(self.d.quote_literal(False), "0")

    def test_native_boolean_column_uses_boolean_literal(self):
        col = Column("ENABLED", type="boolean", native_type="BOOLEAN")
        self.assertEqual(self.d.quote_column_literal(True, col), "TRUE")
        self.assertEqual(self.d.quote_column_literal(False, col), "FALSE")

    def test_to_sql_uses_native_boolean_literals(self):
        schema = Schema()
        schema.add(Table("FEATURE_FLAGS", [
            Column("ID", type="integer", primary_key=True, nullable=False, native_type="NUMBER"),
            Column("ENABLED", type="boolean", nullable=False, native_type="BOOLEAN"),
        ]))
        data = {"FEATURE_FLAGS": [{"ID": 1, "ENABLED": True}]}

        sql = to_sql(schema, data, self.d)

        self.assertIn('VALUES (1, TRUE);', sql)

    def test_identifier_quoting(self):
        self.assertEqual(self.d.quote_identifier("INCIDENTS"), '"INCIDENTS"')


class FakeOracleCursor:
    def __init__(self, rows_by_sql):
        self.rows_by_sql = rows_by_sql
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        return iter(self.rows_by_sql[sql])


class FakeOracleConnection:
    def __init__(self, rows_by_sql):
        self.cursor_obj = FakeOracleCursor(rows_by_sql)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class FakeOracleModule:
    def __init__(self, conn):
        self.conn = conn
        self.connect_calls = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self.conn


class OracleIntrospectionTests(unittest.TestCase):
    def test_introspect_consumes_dictionary_rows_into_schema_contract(self):
        rows_by_sql = {
            _TABLES_SQL: [("ACCOUNTS",), ("FEATURE_FLAGS",), ("INVOICES",)],
            _COLUMNS_SQL: [
                ("ACCOUNTS", "REGION_CODE", "VARCHAR2", None, None, "N", 10),
                ("ACCOUNTS", "ACCOUNT_NO", "NUMBER", None, None, "N", None),
                ("ACCOUNTS", "LABEL", "VARCHAR2", None, None, "N", 80),
                ("FEATURE_FLAGS", "ID", "NUMBER", None, None, "N", None),
                ("FEATURE_FLAGS", "ENABLED", "BOOLEAN", None, None, "N", None),
                ("FEATURE_FLAGS", "IS_ACTIVE", "NUMBER", 1, 0, "N", None),
                ("FEATURE_FLAGS", "PAYLOAD", "BLOB", None, None, "Y", None),
                ("INVOICES", "REGION_CODE", "VARCHAR2", None, None, "N", 10),
                ("INVOICES", "ACCOUNT_NO", "NUMBER", None, None, "N", None),
                ("INVOICES", "INVOICE_NO", "NUMBER", None, None, "N", None),
                ("INVOICES", "AMOUNT", "NUMBER", 8, 2, "N", None),
                ("INVOICES", "STATUS", "VARCHAR2", None, None, "Y", 12),
            ],
            _PK_SQL: [
                ("ACCOUNTS", "REGION_CODE"),
                ("ACCOUNTS", "ACCOUNT_NO"),
                ("FEATURE_FLAGS", "ID"),
                ("INVOICES", "REGION_CODE"),
                ("INVOICES", "ACCOUNT_NO"),
                ("INVOICES", "INVOICE_NO"),
            ],
            _UNIQUE_SQL: [
                ("ACCOUNTS_LABEL_UQ", "ACCOUNTS", "LABEL"),
                ("INVOICES_STATUS_ACCOUNT_UQ", "INVOICES", "STATUS"),
                ("INVOICES_STATUS_ACCOUNT_UQ", "INVOICES", "ACCOUNT_NO"),
            ],
            _FK_SQL: [
                (
                    "INVOICES_ACCOUNT_FK",
                    1,
                    "INVOICES",
                    "REGION_CODE",
                    "ACCOUNTS",
                    "REGION_CODE",
                ),
                (
                    "INVOICES_ACCOUNT_FK",
                    2,
                    "INVOICES",
                    "ACCOUNT_NO",
                    "ACCOUNTS",
                    "ACCOUNT_NO",
                ),
            ],
        }
        conn = FakeOracleConnection(rows_by_sql)
        fake_oracledb = FakeOracleModule(conn)

        with patch.object(oracle_module, "oracledb", fake_oracledb):
            schema = OracleDialect("scott", "tiger", "configured-dsn").introspect()

        self.assertTrue(conn.closed)
        self.assertEqual(
            fake_oracledb.connect_calls,
            [{"user": "scott", "password": "tiger", "dsn": "configured-dsn"}],
        )
        self.assertEqual(
            conn.cursor_obj.executed,
            [_TABLES_SQL, _COLUMNS_SQL, _PK_SQL, _UNIQUE_SQL, _FK_SQL],
        )

        accounts = schema["ACCOUNTS"]
        self.assertEqual([c.name for c in accounts.primary_keys], ["REGION_CODE", "ACCOUNT_NO"])
        self.assertEqual(accounts.column("REGION_CODE").max_length, 10)
        self.assertEqual(accounts.column("ACCOUNT_NO").type, "integer")
        self.assertIsNone(accounts.column("ACCOUNT_NO").numeric_precision)
        self.assertEqual(accounts.column("ACCOUNT_NO").numeric_scale, 0)
        self.assertTrue(accounts.column("LABEL").unique)

        flags = schema["FEATURE_FLAGS"]
        self.assertTrue(flags.column("ID").primary_key)
        self.assertEqual(flags.column("ID").type, "integer")
        self.assertEqual(flags.column("ENABLED").type, "boolean")
        self.assertEqual(flags.column("ENABLED").native_type, "BOOLEAN")
        self.assertEqual(flags.column("IS_ACTIVE").type, "boolean")
        self.assertEqual(flags.column("IS_ACTIVE").native_type, "NUMBER")
        self.assertEqual(flags.column("PAYLOAD").type, "blob")
        self.assertTrue(flags.column("PAYLOAD").nullable)

        invoices = schema["INVOICES"]
        self.assertEqual(
            [c.name for c in invoices.primary_keys],
            ["REGION_CODE", "ACCOUNT_NO", "INVOICE_NO"],
        )
        self.assertEqual(invoices.column("ACCOUNT_NO").type, "integer")
        self.assertEqual(invoices.column("INVOICE_NO").type, "integer")
        self.assertFalse(invoices.column("STATUS").unique)
        self.assertEqual(invoices.column("STATUS").max_length, 12)
        self.assertEqual(invoices.column("AMOUNT").type, "numeric")
        self.assertEqual(invoices.column("AMOUNT").numeric_precision, 8)
        self.assertEqual(invoices.column("AMOUNT").numeric_scale, 2)

        groups = invoices.foreign_key_groups
        self.assertEqual(len(groups), 1)
        self.assertEqual([c.name for c in groups[0]], ["REGION_CODE", "ACCOUNT_NO"])
        self.assertEqual(
            [c.foreign_key.ref_column for c in groups[0]],
            ["REGION_CODE", "ACCOUNT_NO"],
        )
        self.assertEqual(
            [c.foreign_key.constraint_name for c in groups[0]],
            ["INVOICES_ACCOUNT_FK", "INVOICES_ACCOUNT_FK"],
        )


class OracleIsConcreteTests(unittest.TestCase):
    def test_can_be_instantiated(self):
        # implements all abstract methods -> not abstract -> constructs fine
        OracleDialect("u", "p", "d")


if __name__ == "__main__":
    unittest.main()
