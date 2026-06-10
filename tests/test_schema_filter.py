import os
import tempfile
import unittest

from seedwright.model import Column, ForeignKey, Schema, Table
from seedwright.schema_filter import read_table_list, restrict_schema


class SchemaFilterTests(unittest.TestCase):
    def _schema(self):
        schema = Schema()
        schema.add(Table("customers", [
            Column("id", type="integer", primary_key=True, nullable=False),
        ]))
        schema.add(Table("orders", [
            Column("id", type="integer", primary_key=True, nullable=False),
            Column(
                "customer_id",
                type="integer",
                nullable=False,
                foreign_key=ForeignKey("customers", "id"),
            ),
        ]))
        return schema

    def test_read_table_list_ignores_blanks_and_comments(self):
        fd, path = tempfile.mkstemp()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(fd, "w") as fh:
            fh.write("\n# comment\ncustomers\norders # include dependent table\n")

        self.assertEqual(read_table_list(path), ["customers", "orders"])

    def test_restrict_schema_keeps_requested_tables(self):
        filtered = restrict_schema(self._schema(), ["customers"])

        self.assertEqual(list(filtered.tables), ["customers"])

    def test_restrict_schema_requires_referenced_parent_tables(self):
        with self.assertRaisesRegex(ValueError, "customers.id"):
            restrict_schema(self._schema(), ["orders"])

    def test_restrict_schema_rejects_unknown_tables(self):
        with self.assertRaisesRegex(ValueError, "ghost"):
            restrict_schema(self._schema(), ["ghost"])


if __name__ == "__main__":
    unittest.main()
