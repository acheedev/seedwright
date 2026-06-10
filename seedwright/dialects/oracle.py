"""Oracle dialect: introspect a live schema from the data dictionary.

This is the second `Dialect`, and it follows the rule the ABC promised — it
adds Oracle support by implementing three methods and touching nothing else in
the pipeline.

Design note: the live-connection code (opening `oracledb`, running queries) is
kept thin and pushes every row into pure, module-level builder functions
(`normalize_oracle_type`, `assemble_schema`). Those have no database dependency,
so the type-mapping and schema-assembly logic is unit-tested with hand-fed rows
and no Oracle instance — which is most of the logic and all of the risk.

Introspection reads the USER_* dictionary views (the connected schema). For a
cross-schema variant, swap USER_* for ALL_* and add `WHERE owner = :owner`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional

from ..model import Column, ForeignKey, Schema, Table
from .base import Dialect

try:  # the driver is an optional dependency; the module imports fine without it
    import oracledb
except ImportError:  # pragma: no cover
    oracledb = None


# --- pure logic, no database required (this is what the tests exercise) -------

def normalize_oracle_type(
    data_type: str,
    precision: Optional[int],
    scale: Optional[int],
    char_length: Optional[int],
) -> tuple[str, Optional[int]]:
    """Map an Oracle column type to seedwright's normalized vocabulary."""
    d = (data_type or "").upper()

    if d.startswith("NUMBER") or d in ("INTEGER", "INT", "SMALLINT", "NUMERIC", "DECIMAL"):
        # NUMBER(p,0) and INTEGER are whole numbers; a positive scale (or an
        # unconstrained NUMBER, scale None) can hold decimals.
        if scale == 0:
            return "integer", None
        return "numeric", None
    if d in ("FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "REAL", "DOUBLE PRECISION"):
        return "real", None
    if d in ("VARCHAR2", "NVARCHAR2", "VARCHAR", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG"):
        return "text", char_length
    if d.startswith("TIMESTAMP"):
        return "datetime", None
    if d == "DATE":  # Oracle DATE carries a time component, so it's a datetime
        return "datetime", None
    if d in ("BLOB", "RAW", "LONG RAW", "BFILE"):
        return "blob", None
    if d == "BOOLEAN":  # native boolean exists in Oracle 23ai+
        return "boolean", None
    if d in ("ROWID", "UROWID"):
        return "text", None
    return "text", char_length


def oracle_numeric_metadata(
    data_type: str,
    precision: Optional[int],
    scale: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """Return NUMBER precision/scale metadata when Oracle provides it."""
    d = (data_type or "").upper()
    if not (d.startswith("NUMBER") or d in ("INTEGER", "INT", "SMALLINT", "NUMERIC", "DECIMAL")):
        return None, None
    return precision, scale


def should_treat_number_as_integer_id(
    data_type: str,
    precision: Optional[int],
    scale: Optional[int],
    column_name: str,
    is_pk: bool,
    is_fk: bool,
) -> bool:
    """Oracle bare NUMBER is numeric, except ID-shaped key columns.

    USER_TAB_COLUMNS reports unconstrained NUMBER with precision/scale NULL.
    That is decimal-capable in general, but primary keys, foreign keys, and
    common ID names should generate integer values.
    """
    d = (data_type or "").upper()
    name = column_name.upper()
    if not d.startswith("NUMBER") or precision is not None or scale is not None:
        return False
    return is_pk or is_fk or name == "ID" or name.endswith("_ID")


def should_treat_number_as_boolean(
    data_type: str,
    precision: Optional[int],
    scale: Optional[int],
    column_name: str,
) -> bool:
    """Treat common NUMBER(1) flag columns as boolean-like.

    Oracle schemas often use NUMBER(1) for booleans, but NUMBER(1) is also a
    valid single-digit code. Keep this contextual and name-driven so columns
    like PRIORITY_CODE continue to generate 0-9 values.
    """
    d = (data_type or "").upper()
    name = column_name.upper()
    if not d.startswith("NUMBER") or precision != 1 or scale not in (None, 0):
        return False

    boolean_prefixes = ("IS_", "HAS_", "CAN_", "SHOULD_")
    boolean_names = {
        "ACTIVE",
        "ENABLED",
        "DISABLED",
        "DELETED",
        "VALID",
        "VISIBLE",
        "LOCKED",
        "ARCHIVED",
        "REQUIRED",
        "VERIFIED",
    }
    return name.startswith(boolean_prefixes) or name in boolean_names


def assemble_schema(
    table_names: list[str],
    column_rows: list[tuple],   # (table, column, data_type, precision, scale, nullable, char_length)
    pk_rows: list[tuple],       # (table, column)
    unique_single_rows: list[tuple],  # (table, column) for single-column UNIQUE constraints
    fk_rows: list[tuple],       # (constraint, position, child_table, child_column, parent_table, parent_column)
) -> Schema:
    """Build a Schema from raw dictionary rows. No database access."""
    pks = {(t, c) for t, c in pk_rows}
    uniques = {(t, c) for t, c in unique_single_rows}
    fks: dict[tuple[str, str], ForeignKey] = {}
    for row in fk_rows:
        if len(row) == 4:  # compatibility with older unit tests / callers
            ct, cc, pt, pc = row
            cname = None
            pos = 1
        else:
            cname, pos, ct, cc, pt, pc = row
        fks[(ct, cc)] = ForeignKey(
            ref_table=pt,
            ref_column=pc,
            constraint_name=cname,
            position=int(pos),
        )

    by_table: dict[str, list[Column]] = {name: [] for name in table_names}
    for table, col, data_type, precision, scale, nullable, char_length in column_rows:
        if table not in by_table:
            continue
        is_pk = (table, col) in pks
        fk = fks.get((table, col))
        ntype, length = normalize_oracle_type(data_type, precision, scale, char_length)
        numeric_precision, numeric_scale = oracle_numeric_metadata(
            data_type, precision, scale
        )
        if should_treat_number_as_boolean(data_type, precision, scale, col):
            ntype = "boolean"
        elif should_treat_number_as_integer_id(
            data_type,
            precision,
            scale,
            col,
            is_pk,
            fk is not None,
        ):
            ntype = "integer"
            numeric_scale = 0
        by_table[table].append(
            Column(
                name=col,
                type=ntype,
                nullable=(nullable == "Y") and not is_pk,
                primary_key=is_pk,
                unique=(table, col) in uniques or is_pk,
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


# --- the dialect ---------------------------------------------------------------

_TABLES_SQL = "SELECT table_name FROM user_tables ORDER BY table_name"

_COLUMNS_SQL = """
    SELECT table_name, column_name, data_type,
           data_precision, data_scale, nullable, char_length
    FROM user_tab_columns
    ORDER BY table_name, column_id
"""

_PK_SQL = """
    SELECT cc.table_name, cc.column_name
    FROM user_constraints c
    JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
    WHERE c.constraint_type = 'P'
"""

_UNIQUE_SQL = """
    SELECT cc.constraint_name, cc.table_name, cc.column_name
    FROM user_constraints c
    JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
    WHERE c.constraint_type = 'U'
"""

_FK_SQL = """
    SELECT c.constraint_name AS constraint_name,
           cc.position    AS position,
           cc.table_name   AS child_table,
           cc.column_name  AS child_column,
           rc.table_name   AS parent_table,
           rcc.column_name AS parent_column
    FROM user_constraints c
    JOIN user_cons_columns cc  ON cc.constraint_name = c.constraint_name
    JOIN user_constraints rc   ON rc.constraint_name = c.r_constraint_name
    JOIN user_cons_columns rcc ON rcc.constraint_name = rc.constraint_name
                              AND rcc.position = cc.position
    WHERE c.constraint_type = 'R'
    ORDER BY cc.table_name, c.constraint_name, cc.position
"""


class OracleDialect(Dialect):
    name = "oracle"

    def __init__(self, user: str, password: str, dsn: str) -> None:
        self.user = user
        self.password = password
        self.dsn = dsn

    def introspect(self) -> Schema:
        if oracledb is None:
            raise RuntimeError(
                "the Oracle dialect needs python-oracledb: "
                "pip install seedwright[oracle]"
            )
        conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
        try:
            cur = conn.cursor()
            table_names = [r[0] for r in cur.execute(_TABLES_SQL)]
            column_rows = list(cur.execute(_COLUMNS_SQL))
            pk_rows = list(cur.execute(_PK_SQL))

            # keep only single-column UNIQUE constraints
            uq_by_constraint: dict[str, list[tuple]] = {}
            for cname, table, col in cur.execute(_UNIQUE_SQL):
                uq_by_constraint.setdefault(cname, []).append((table, col))
            unique_single = [
                cols[0] for cols in uq_by_constraint.values() if len(cols) == 1
            ]

            fk_rows = list(cur.execute(_FK_SQL))
        finally:
            conn.close()

        return assemble_schema(
            table_names, column_rows, pk_rows, unique_single, fk_rows
        )

    def quote_identifier(self, identifier: str) -> str:
        # Dictionary names are uppercased; quoting the uppercase form is safe.
        return '"' + identifier.replace('"', '""') + '"'

    def quote_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "HEXTORAW('" + bytes(value).hex().upper() + "')"
        if isinstance(value, bool):
            return "1" if value else "0"  # works for NUMBER(1)-style conventions
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        if isinstance(value, dt.datetime):  # must precede dt.date — datetime subclasses it
            return (
                "TO_DATE('"
                + value.strftime("%Y-%m-%d %H:%M:%S")
                + "', 'YYYY-MM-DD HH24:MI:SS')"
            )
        if isinstance(value, dt.date):
            return "DATE '" + value.strftime("%Y-%m-%d") + "'"
        return "'" + str(value).replace("'", "''") + "'"

    def quote_column_literal(self, value: Any, column: Column) -> str:
        if isinstance(value, bool) and (column.native_type or "").upper() == "BOOLEAN":
            return "TRUE" if value else "FALSE"
        return self.quote_literal(value)
