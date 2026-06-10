import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from seedwright.cli import (
    _make_dialect,
    _log_level,
    _parse_per_table,
    _safe_csv_filename,
    build_parser,
    main,
)
from seedwright.config import load_config
from seedwright.model import Column, Schema, Table


class ConfiguredOracleCliTests(unittest.TestCase):
    def _config_file(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".ini")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    def test_oracle_connection_comes_from_dialect_config_section(self):
        path = self._config_file(
            "[oracle]\n"
            "user = scott\n"
            "password = tiger\n"
            "host = oracle.example.test\n"
            "port = 1521\n"
            "service_name = FREEPDB1\n"
        )
        args = build_parser().parse_args(["--dialect", "oracle", "--config", path])

        dialect = _make_dialect(args)

        self.assertEqual(dialect.user, "scott")
        self.assertEqual(dialect.password, "tiger")
        self.assertEqual(dialect.dsn, "oracle.example.test:1521/FREEPDB1")

    def test_oracle_user_and_password_can_override_config(self):
        path = self._config_file(
            "[oracle]\n"
            "user = ignored\n"
            "password = ignored\n"
            "host = oracle.example.test\n"
            "service_name = FREEPDB1\n"
        )
        args = build_parser().parse_args([
            "--dialect",
            "oracle",
            "--config",
            path,
            "--oracle-user",
            "scott",
            "--oracle-password",
            "tiger",
        ])

        dialect = _make_dialect(args)

        self.assertEqual(dialect.user, "scott")
        self.assertEqual(dialect.password, "tiger")
        self.assertEqual(dialect.dsn, "oracle.example.test:1521/FREEPDB1")

    def test_oracle_requires_host_and_service_name_from_config(self):
        path = self._config_file("[oracle]\nuser = scott\npassword = tiger\n")
        args = build_parser().parse_args(["--dialect", "oracle", "--config", path])

        with self.assertRaises(SystemExit) as ctx:
            _make_dialect(args)

        self.assertIn("[oracle].host", str(ctx.exception))
        self.assertIn("[oracle].service_name", str(ctx.exception))


class CliValidationTests(unittest.TestCase):
    def _config_file(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".ini")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    def test_negative_default_rows_is_rejected_by_parser(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--db", "demo.db", "--rows", "-1"])

    def test_malformed_table_rows_count_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_per_table("customers=lots")

    def test_negative_table_rows_count_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_per_table("customers=-1")

    def test_unknown_table_rows_override_is_rejected_after_introspection(self):
        config_path = self._config_file("")
        schema = Schema()
        schema.add(Table("customers", [
            Column("id", type="integer", primary_key=True, nullable=False),
        ]))

        class FakeDialect:
            def introspect(self):
                return schema

        with patch("seedwright.cli._make_dialect", return_value=FakeDialect()):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main([
                        "--db",
                        "ignored.db",
                        "--config",
                        config_path,
                        "--table-rows",
                        "orders=5",
                    ])

        self.assertIn("unknown table(s)", str(ctx.exception))
        self.assertIn("orders", str(ctx.exception))
        self.assertIn("customers", str(ctx.exception))

    def test_safe_csv_filename_removes_path_parts_and_unsafe_chars(self):
        used = set()
        filename = _safe_csv_filename("../bad/table:name", used)
        self.assertEqual(filename, "bad_table_name.csv")

    def test_safe_csv_filename_handles_collisions_case_insensitively(self):
        used = set()
        self.assertEqual(_safe_csv_filename("Orders", used), "Orders.csv")
        self.assertEqual(_safe_csv_filename("orders", used), "orders_2.csv")

    def test_info_log_level_reports_progress_when_writing_file(self):
        config_path = self._config_file("[seedwright]\nlog_level = info\n")
        fd, out_path = tempfile.mkstemp(suffix=".sql")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(out_path) and os.remove(out_path))

        schema = Schema()
        schema.add(Table("customers", [
            Column("id", type="integer", primary_key=True, nullable=False),
        ]))

        class FakeDialect:
            def introspect(self):
                return schema

            def quote_identifier(self, identifier):
                return '"' + identifier + '"'

            def quote_literal(self, value):
                return str(value)

            def quote_column_literal(self, value, column):
                return self.quote_literal(value)

        with patch("seedwright.cli._make_dialect", return_value=FakeDialect()):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = main([
                    "--db",
                    "ignored.db",
                    "--config",
                    config_path,
                    "--rows",
                    "2",
                    "--out",
                    out_path,
                ])

        self.assertEqual(rc, 0)
        self.assertIn("seedwright: introspecting schema", stdout.getvalue())
        self.assertIn("seedwright: row plan: customers=2", stdout.getvalue())
        self.assertIn("seedwright: generated 2 row(s)", stdout.getvalue())
        self.assertIn("wrote 2 rows", stderr.getvalue())

    def test_invalid_log_level_is_rejected(self):
        config_path = self._config_file("[seedwright]\nlog_level = noisy\n")
        config = load_config(config_path)

        with self.assertRaises(SystemExit):
            _log_level(config)


if __name__ == "__main__":
    unittest.main()
