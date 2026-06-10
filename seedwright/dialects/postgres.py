"""Postgres dialect: introspect a schema from information_schema/pg_catalog."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional

from ..model import Column, ForeignKey, Schema, Table
from .base import Dialect, ValidationResult

try:  # optional dependency; importing the module should not require psycopg
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def normalize_postgres_type(
    data_type: str,
    udt_name: str,
    numeric_precision: Optional[int],
    numeric_scale: Optional[int],
    char_length: Optional[int],
) -> tuple[str, Optional[int]]:
    """Map a Postgres column type to seedwright's normalized vocabulary."""
    d = (data_type or "").lower()
    udt = (udt_name or "").lower()

    if udt in ("int2", "int4", "int8") or d in ("smallint", "integer", "bigint"):
        return "integer", None
    if d == "numeric" or d == "decimal":
        if numeric_scale == 0:
            return "integer", None
        return "numeric", None
    if udt in ("float4", "float8") or d in ("real", "double precision"):
        return "real", None
    if d in ("character varying", "character", "text", "citext") or udt in (
        "varchar",
        "bpchar",
        "text",
        "citext",
    ):
        return "text", char_length
    if d == "boolean" or udt == "bool":
        return "boolean", None
    if d == "date":
        return "date", None
    if "timestamp" in d or udt in ("timestamp", "timestamptz"):
        return "datetime", None
    if d == "bytea" or udt == "bytea":
        return "blob", None
    if d == "time" or udt in ("time", "timetz"):
        return "datetime", None
    return "text", char_length


def postgres_numeric_metadata(
    data_type: str,
    precision: Optional[int],
    scale: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    d = (data_type or "").lower()
    if d not in ("numeric", "decimal"):
        return None, None
    return precision, scale


def assemble_schema(
    table_names: list[str],
    column_rows: list[tuple],
    pk_rows: list[tuple],
    unique_single_rows: list[tuple],
    fk_rows: list[tuple],
) -> Schema:
    """Build a Schema from Postgres catalog rows. No database access."""
    pks = {(t, c) for t, c in pk_rows}
    uniques = {(t, c) for t, c in unique_single_rows}
    fks: dict[tuple[str, str], ForeignKey] = {}
    for cname, pos, child_table, child_column, parent_table, parent_column in fk_rows:
        fks[(child_table, child_column)] = ForeignKey(
            ref_table=parent_table,
            ref_column=parent_column,
            constraint_name=cname,
            position=int(pos),
        )

    by_table: dict[str, list[Column]] = {name: [] for name in table_names}
    for (
        table,
        column,
        data_type,
        udt_name,
        nullable,
        char_length,
        precision,
        scale,
    ) in column_rows:
        if table not in by_table:
            continue
        is_pk = (table, column) in pks
        fk = fks.get((table, column))
        ntype, length = normalize_postgres_type(
            data_type,
            udt_name,
            precision,
            scale,
            char_length,
        )
        numeric_precision, numeric_scale = postgres_numeric_metadata(
            data_type, precision, scale
        )
        by_table[table].append(
            Column(
                name=column,
                type=ntype,
                nullable=(nullable == "YES") and not is_pk,
                primary_key=is_pk,
                unique=(table, column) in uniques or is_pk,
                foreign_key=fk,
                max_length=length,
                numeric_precision=numeric_precision,
                numeric_scale=numeric_scale,
                native_type=data_type,
            )
        )

    schema = Schema()
    for name in table_names:
        schema.add(Table(name=name, columns=by_table[name]))
    return schema


_TABLES_SQL = """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = %s
      AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""

_COLUMNS_SQL = """
    SELECT table_name, column_name, data_type, udt_name, is_nullable,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = %s
    ORDER BY table_name, ordinal_position
"""

_PK_SQL = """
    SELECT tc.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
     AND kcu.table_name = tc.table_name
    WHERE tc.table_schema = %s
      AND tc.constraint_type = 'PRIMARY KEY'
    ORDER BY tc.table_name, kcu.ordinal_position
"""

_UNIQUE_SQL = """
    SELECT tc.constraint_name, kcu.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
     AND kcu.table_name = tc.table_name
    WHERE tc.table_schema = %s
      AND tc.constraint_type = 'UNIQUE'
    ORDER BY tc.constraint_name, kcu.ordinal_position
"""

_FK_SQL = """
    SELECT tc.constraint_name,
           kcu.ordinal_position AS position,
           kcu.table_name AS child_table,
           kcu.column_name AS child_column,
           pkcu.table_name AS parent_table,
           pkcu.column_name AS parent_column
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
     AND kcu.table_name = tc.table_name
    JOIN information_schema.referential_constraints rc
      ON rc.constraint_schema = tc.constraint_schema
     AND rc.constraint_name = tc.constraint_name
    JOIN information_schema.key_column_usage pkcu
      ON pkcu.constraint_schema = rc.unique_constraint_schema
     AND pkcu.constraint_name = rc.unique_constraint_name
     AND pkcu.ordinal_position = kcu.position_in_unique_constraint
    WHERE tc.table_schema = %s
      AND tc.constraint_type = 'FOREIGN KEY'
    ORDER BY kcu.table_name, tc.constraint_name, kcu.ordinal_position
"""


class PostgresDialect(Dialect):
    name = "postgres"

    def __init__(self, dsn: str, schema: str = "public") -> None:
        self.dsn = dsn
        self.schema = schema

    def introspect(self) -> Schema:
        if psycopg is None:
            raise RuntimeError(
                "the Postgres dialect needs psycopg: "
                "pip install seedwright[postgres]"
            )
        conn = psycopg.connect(self.dsn)
        try:
            with conn.cursor() as cur:
                table_names = [r[0] for r in cur.execute(_TABLES_SQL, (self.schema,))]
                column_rows = list(cur.execute(_COLUMNS_SQL, (self.schema,)))
                pk_rows = list(cur.execute(_PK_SQL, (self.schema,)))

                uq_by_constraint: dict[str, list[tuple]] = {}
                for cname, table, col in cur.execute(_UNIQUE_SQL, (self.schema,)):
                    uq_by_constraint.setdefault(cname, []).append((table, col))
                unique_single = [
                    cols[0] for cols in uq_by_constraint.values() if len(cols) == 1
                ]

                fk_rows = list(cur.execute(_FK_SQL, (self.schema,)))
        finally:
            conn.close()

        return assemble_schema(
            table_names, column_rows, pk_rows, unique_single, fk_rows
        )

    def quote_identifier(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "'\\x" + bytes(value).hex() + "'::bytea"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        if isinstance(value, dt.datetime):
            return "TIMESTAMP '" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
        if isinstance(value, dt.date):
            return "DATE '" + value.strftime("%Y-%m-%d") + "'"
        return "'" + str(value).replace("'", "''") + "'"

    def validate_script(
        self,
        validation_target: str,
        table_names: list[str],
        sql: str,
    ) -> ValidationResult:
        if psycopg is None:
            raise RuntimeError(
                "the Postgres dialect needs psycopg: "
                "pip install seedwright[postgres]"
            )
        conn = psycopg.connect(validation_target)
        try:
            with conn.cursor() as cur:
                self._set_search_path(cur)
                before = self._count_rows(cur, table_names)
                for statement in _split_sql_script(sql):
                    cur.execute(statement)
                cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                after = self._count_rows(cur, table_names)
                conn.rollback()
                return ValidationResult(tables=len(table_names), rows=after - before)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def apply_script(self, sql: str) -> None:
        if psycopg is None:
            raise RuntimeError(
                "the Postgres dialect needs psycopg: "
                "pip install seedwright[postgres]"
            )
        conn = psycopg.connect(self.dsn)
        try:
            with conn.cursor() as cur:
                self._set_search_path(cur)
                for statement in _split_sql_script(sql):
                    cur.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _set_search_path(self, cur) -> None:
        cur.execute("SET search_path TO " + self.quote_identifier(self.schema))

    def _count_rows(self, cur, table_names: list[str]) -> int:
        total = 0
        for name in table_names:
            cur.execute("SELECT COUNT(*) FROM " + self.quote_identifier(name))
            total += cur.fetchone()[0]
        return total


def _split_sql_script(sql: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        pending += line + "\n"
        if stripped.endswith(";"):
            statements.append(pending.strip().rstrip(";"))
            pending = ""
    if pending.strip():
        raise RuntimeError("SQL script ended with an incomplete statement")
    return statements
