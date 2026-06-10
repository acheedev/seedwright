"""SQLite dialect: the reference implementation.

Introspection uses SQLite's PRAGMA functions:
  PRAGMA table_info(t)        -> columns, types, notnull, pk
  PRAGMA foreign_key_list(t)  -> foreign keys
  PRAGMA index_list / index_info -> unique constraints

SQLite stores types loosely (type affinity), so declared types like
``VARCHAR(40)`` or ``DECIMAL(10,2)`` are mapped to our normalized vocabulary by
inspecting substrings, following SQLite's own affinity rules.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from decimal import Decimal
from typing import Any

from ..model import Column, ForeignKey, Schema, Table
from .base import Dialect, ValidationResult

_TYPE_ARGS_RE = re.compile(r"\((\d+)(?:\s*,\s*(-?\d+))?")


def normalize_type(declared: str) -> tuple[str, int | None]:
    """Map a SQLite declared type to (normalized_type, max_length)."""
    d = (declared or "").upper()
    length = None
    m = _TYPE_ARGS_RE.search(d)
    if m:
        length = int(m.group(1))
    if "INT" in d:
        return "integer", None
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "text", length
    if "BLOB" in d or d == "":
        return "blob", None
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "real", None
    if any(k in d for k in ("NUMERIC", "DECIMAL", "NUMBER")):
        return "numeric", None
    if "BOOL" in d:
        return "boolean", None
    if "DATETIME" in d or "TIMESTAMP" in d:
        return "datetime", None
    if "DATE" in d:
        return "date", None
    if "TIME" in d:
        return "datetime", None
    return "text", length  # SQLite's fallback affinity is effectively text


def numeric_metadata(declared: str) -> tuple[int | None, int | None]:
    """Extract precision/scale from DECIMAL(10,2)-style declarations."""
    d = (declared or "").upper()
    if not any(k in d for k in ("NUMERIC", "DECIMAL", "NUMBER")):
        return None, None
    m = _TYPE_ARGS_RE.search(d)
    if not m:
        return None, None
    precision = int(m.group(1))
    scale = int(m.group(2)) if m.group(2) is not None else None
    return precision, scale


class SQLiteDialect(Dialect):
    name = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path

    def introspect(self) -> Schema:
        schema = Schema()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for tname in tables:
                schema.add(self._read_table(conn, tname))
        finally:
            conn.close()
        return schema

    def _read_table(self, conn: sqlite3.Connection, tname: str) -> Table:
        # foreign keys: column -> (parent table, parent column), grouped by
        # PRAGMA foreign_key_list.id for composite constraints.
        fks: dict[str, ForeignKey] = {}
        for fk in conn.execute(f"PRAGMA foreign_key_list({_q(tname)})"):
            fks[fk["from"]] = ForeignKey(
                ref_table=fk["table"],
                ref_column=fk["to"],
                constraint_name=f"sqlite_fk_{fk['id']}",
                position=int(fk["seq"]) + 1,
            )

        # unique columns: single-column unique indexes
        unique_cols: set[str] = set()
        for idx in conn.execute(f"PRAGMA index_list({_q(tname)})"):
            if idx["unique"]:
                cols = list(conn.execute(f"PRAGMA index_info({_q(idx['name'])})"))
                if len(cols) == 1:
                    unique_cols.add(cols[0]["name"])

        columns: list[Column] = []
        for row in conn.execute(f"PRAGMA table_info({_q(tname)})"):
            ntype, length = normalize_type(row["type"])
            numeric_precision, numeric_scale = numeric_metadata(row["type"])
            is_pk = bool(row["pk"])
            columns.append(
                Column(
                    name=row["name"],
                    type=ntype,
                    nullable=not row["notnull"] and not is_pk,
                    primary_key=is_pk,
                    unique=row["name"] in unique_cols or is_pk,
                    foreign_key=fks.get(row["name"]),
                    max_length=length,
                    numeric_precision=numeric_precision,
                    numeric_scale=numeric_scale,
                    native_type=row["type"],
                )
            )
        return Table(name=tname, columns=columns)

    def quote_identifier(self, identifier: str) -> str:
        return _q(identifier)

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "X'" + bytes(value).hex() + "'"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        if isinstance(value, (dt.date, dt.datetime)):
            value = value.isoformat()
        return "'" + str(value).replace("'", "''") + "'"

    def validate_script(
        self,
        validation_target: str,
        table_names: list[str],
        sql: str,
    ) -> ValidationResult:
        """Run generated SQL against an existing SQLite validation DB and roll it back."""
        conn = sqlite3.connect(validation_target)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            before = _count_rows(conn, table_names)
            conn.execute("BEGIN")
            for statement in _split_sql_script(sql):
                try:
                    conn.execute(statement)
                except sqlite3.IntegrityError as exc:
                    if "FOREIGN KEY" in str(exc).upper():
                        raise RuntimeError(
                            f"foreign-key violations in validation database: {exc}"
                        ) from exc
                    raise RuntimeError(
                        f"validation database rejected generated SQL: {exc}"
                    ) from exc
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"foreign-key violations in validation database: {violations}"
                )
            after = _count_rows(conn, table_names)
            return ValidationResult(tables=len(table_names), rows=after - before)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.rollback()
            conn.close()

    def apply_script(self, sql: str) -> None:
        """Run generated SQL against the configured SQLite database in one transaction."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN")
            for statement in _split_sql_script(sql):
                conn.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _count_rows(conn: sqlite3.Connection, table_names: list[str]) -> int:
    return sum(
        conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
        for name in table_names
    )


def _split_sql_script(sql: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip().rstrip(";"))
            pending = ""
    if pending.strip():
        raise RuntimeError("SQL script ended with an incomplete statement")
    return statements
