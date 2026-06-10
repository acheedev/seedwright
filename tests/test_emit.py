import unittest

from seedwright.dialects import SQLiteDialect
from seedwright.emit import to_csv, to_sql
from seedwright.model import Column, Schema, Table


class EmitTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.schema.add(Table("t", [
            Column("id", type="integer", primary_key=True, nullable=False),
            Column("label", type="text", nullable=True),
        ]))
        self.dialect = SQLiteDialect(":memory:")

    def test_sql_has_insert_per_row(self):
        data = {"t": [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]}
        sql = to_sql(self.schema, data, self.dialect)
        self.assertEqual(sql.count("INSERT INTO"), 2)

    def test_single_quote_is_escaped(self):
        data = {"t": [{"id": 1, "label": "O'Brien"}]}
        sql = to_sql(self.schema, data, self.dialect)
        self.assertIn("'O''Brien'", sql)

    def test_none_becomes_null(self):
        data = {"t": [{"id": 1, "label": None}]}
        sql = to_sql(self.schema, data, self.dialect)
        self.assertIn("NULL", sql)
        self.assertNotIn("'None'", sql)

    def test_sqlite_blob_is_hex_literal(self):
        self.schema["t"].columns.append(Column("payload", type="blob", nullable=False))
        data = {"t": [{"id": 1, "label": "a", "payload": b"\x00\xff"}]}
        sql = to_sql(self.schema, data, self.dialect)
        self.assertIn("X'00ff'", sql)

    def test_csv_roundtrip_headers(self):
        rows = [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
        out = to_csv("t", rows)
        self.assertTrue(out.startswith("id,label"))
        self.assertEqual(out.strip().count("\n"), 2)  # header + 2 rows - 1


if __name__ == "__main__":
    unittest.main()
