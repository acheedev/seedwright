"""Restrict an introspected schema to an explicit table allow-list."""

from __future__ import annotations

from .model import Schema, Table


def read_table_list(path: str) -> list[str]:
    """Read one table name per line, ignoring blanks and ``#`` comments."""
    names: list[str] = []
    with open(path) as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                names.append(line)
    return names


def restrict_schema(schema: Schema, allowed_tables: list[str]) -> Schema:
    """Return a schema containing only the allowed tables.

    The selected set must be FK-closed. If ``orders`` is included and it
    references ``customers``, ``customers`` must also be included. That keeps
    generation honest instead of silently dropping referential constraints.
    """
    allowed = set(allowed_tables)
    unknown = sorted(allowed - set(schema.tables))
    if unknown:
        known = ", ".join(sorted(schema.tables))
        raise ValueError(
            "unknown table(s) in table list: "
            + ", ".join(unknown)
            + (f". known tables: {known}" if known else "")
        )

    missing_parents: list[str] = []
    for tname in sorted(allowed):
        table = schema[tname]
        for col in table.foreign_keys:
            fk = col.foreign_key
            assert fk is not None
            if fk.ref_table != table.name and fk.ref_table not in allowed:
                missing_parents.append(
                    f"{table.name}.{col.name} references {fk.ref_table}.{fk.ref_column}"
                )
    if missing_parents:
        raise ValueError(
            "table list must include referenced parent tables: "
            + "; ".join(missing_parents)
        )

    out = Schema()
    for tname in schema.tables:
        if tname in allowed:
            source = schema[tname]
            out.add(Table(source.name, list(source.columns)))
    return out
