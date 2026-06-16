"""The internal, database-agnostic schema model.

Everything downstream of introspection works against these objects, never
against a live database. That boundary is the whole design: a dialect's only
job is to turn a real schema into one `Schema`, and to quote/emit values on
the way out. Add Postgres or Oracle by writing one introspector that returns
a `Schema` — the generator, dependency graph, and emitter never change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Normalized type vocabulary. Every dialect maps its native types into one of
# these so the generators only ever reason about a small, closed set.
NORMALIZED_TYPES = {
    "integer",
    "real",
    "numeric",
    "text",
    "boolean",
    "date",
    "datetime",
    "blob",
}


@dataclass(frozen=True)
class ForeignKey:
    """A reference from one child column to one parent column.

    Columns that share a constraint name belong to the same composite FK.
    Single-column callers can keep using ForeignKey("parent", "id").
    """

    ref_table: str
    ref_column: str
    constraint_name: Optional[str] = None
    position: int = 1


@dataclass
class Column:
    name: str
    type: str  # one of NORMALIZED_TYPES
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    foreign_key: Optional[ForeignKey] = None
    max_length: Optional[int] = None  # for text columns, when known
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    native_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.type not in NORMALIZED_TYPES:
            raise ValueError(
                f"column {self.name!r} has un-normalized type {self.type!r}; "
                f"dialects must map native types into {sorted(NORMALIZED_TYPES)}"
            )


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)

    @property
    def primary_keys(self) -> list[Column]:
        return [c for c in self.columns if c.primary_key]

    @property
    def foreign_keys(self) -> list[Column]:
        return [c for c in self.columns if c.foreign_key is not None]

    @property
    def foreign_key_groups(self) -> list[list[Column]]:
        """Return FK columns grouped by constraint, preserving column order."""
        groups: dict[tuple[str, str], list[Column]] = {}
        for c in self.foreign_keys:
            fk = c.foreign_key
            assert fk is not None
            key = (fk.ref_table, fk.constraint_name or c.name)
            groups.setdefault(key, []).append(c)
        return [
            sorted(cols, key=lambda c: c.foreign_key.position if c.foreign_key else 1)
            for cols in groups.values()
        ]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name} not found")


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)

    def add(self, table: Table) -> None:
        self.tables[table.name] = table

    def __iter__(self):
        return iter(self.tables.values())

    def __getitem__(self, name: str) -> Table:
        return self.tables[name]

    def __len__(self) -> int:
        return len(self.tables)


@dataclass(frozen=True)
class DeferredForeignKey:
    """A cross-table FK group inserted as NULL and populated after inserts."""

    table: str
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    constraint_name: Optional[str] = None


@dataclass(frozen=True)
class DeferredUpdate:
    """A second-pass UPDATE that fills a deferred foreign-key group."""

    table: str
    key_columns: tuple[str, ...]
    key_values: tuple[Any, ...]
    assignments: tuple[tuple[str, Any], ...]


class GeneratedData(dict[str, list[dict[str, Any]]]):
    """Generated rows plus optional second-pass updates for deferred FKs."""

    def __init__(self) -> None:
        super().__init__()
        self.deferred_updates: list[DeferredUpdate] = []
