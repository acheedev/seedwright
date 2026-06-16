"""The engine: schema in, rows out, referential integrity guaranteed.

For each table, in dependency order:

1. Decide row count (per-table override or the global default).
2. Generate primary keys. Integer PKs get a clean 1..n sequence; everything else
   is generated and de-duplicated. UNIQUE columns are also de-duplicated per
   table, across all generated value types.
3. Fill non-key columns from the `ValueFactory`.
4. For every non-deferred foreign-key column, draw a value from the *actual* primary keys
   already generated for the parent table. That draw is the referential
   integrity: a child can only ever point at a parent row that exists.
   Self-referencing FKs draw from earlier rows of the same table. Nullable
   self-FKs can become NULL; required first-row self-FKs point at the row itself.
5. For nullable FK groups chosen to break cross-table cycles, insert NULL first
   and record second-pass UPDATEs once both sides of the cycle exist.

The result is an ordered dict of table -> list[row dict], ready for any emitter.
"""

from __future__ import annotations

from typing import Any, Optional

from .generators import ValueFactory
from .graph import dependency_plan
from .model import Column, DeferredForeignKey, DeferredUpdate, GeneratedData, Schema, Table

Row = dict[str, Any]


class GenerationEngine:
    def __init__(
        self,
        schema: Schema,
        default_rows: int = 25,
        per_table: Optional[dict[str, int]] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.schema = schema
        self.default_rows = default_rows
        self.per_table = per_table or {}
        self.factory = ValueFactory(seed=seed)
        # parent table -> list of generated PK values, for FK draws
        self._pk_pool: dict[str, list[Any]] = {}
        # parent table -> generated rows, for composite FK draws
        self._rows_by_table: dict[str, list[Row]] = {}
        self._deferred_fk_keys: set[tuple[str, tuple[str, ...]]] = set()

    def generate(self) -> GeneratedData:
        plan = dependency_plan(self.schema)
        self._deferred_fk_keys = {
            (fk.table, fk.columns) for fk in plan.deferred_fks
        }
        out = GeneratedData()
        for tname in plan.order:
            table = self.schema[tname]
            rows = self._generate_table(table)
            out[tname] = rows
            self._pk_pool[tname] = [self._pk_value(table, r) for r in rows]
            self._rows_by_table[tname] = rows
        out.deferred_updates = self._build_deferred_updates(plan.deferred_fks)
        return out

    def _rows_for(self, tname: str) -> int:
        return self.per_table.get(tname, self.default_rows)

    def _generate_table(self, table: Table) -> list[Row]:
        n = self._rows_for(table.name)
        pks = table.primary_keys
        single_int_pk = (
            pks[0] if len(pks) == 1 and pks[0].type == "integer" else None
        )

        self._check_unique_fk_capacity(table, n)

        # Pre-allocate distinct values for UNIQUE non-FK columns up front. This
        # runs the fail-fast feasibility check before any row is built, and means
        # these columns never enter the collision-retry loop below.
        prealloc: dict[str, list[Any]] = {}
        for col in table.columns:
            if (
                self._is_individually_unique(table, col)
                and col.foreign_key is None
                and col is not single_int_pk
            ):
                prealloc[col.name] = self.factory.distinct_values(col, n)

        # Pre-compute vectorized batches for plain numeric columns.
        batched: dict[str, list[Any]] = {}
        for col in table.columns:
            if (
                col.foreign_key is None
                and not col.primary_key
                and not col.unique
                and col.type in ("integer", "real", "numeric")
            ):
                batched[col.name] = self.factory.batch(col, n)

        rows: list[Row] = []
        seen_pk: set[tuple[Any, ...]] = set()
        seen_unique: dict[str, set[Any]] = {
            col.name: set() for col in table.columns if col.unique and not col.primary_key
        }
        for i in range(n):
            for attempt in range(1000):
                row = self._generate_base_row(table, i, single_int_pk, batched, prealloc)
                self._fill_foreign_keys(table, row, rows)
                pk = self._pk_tuple(table, row)
                unique_values = self._unique_values(table, row, seen_unique)
                if (pk is None or pk not in seen_pk) and unique_values is not None:
                    if pk is not None:
                        seen_pk.add(pk)
                    for col_name, value in unique_values:
                        seen_unique[col_name].add(value)
                    rows.append(row)
                    break
            else:
                # Unique non-FK columns are pre-allocated, so reaching here means
                # a composite-PK or UNIQUE foreign-key combination couldn't be
                # satisfied from the available parent rows.
                raise ValueError(
                    f"could not satisfy primary-key / unique foreign-key uniqueness "
                    f"for {table.name} after 1000 attempts (raise parent row counts)"
                )
        return rows

    @staticmethod
    def _is_individually_unique(table: Table, col: Column) -> bool:
        """True if THIS column alone must hold unique values.

        A single-column PK or a single-column UNIQUE constraint qualifies. A
        member of a *composite* PK does not — the combination is unique, the
        column is not — even though introspection flags PK members unique.
        """
        if col.primary_key:
            return len(table.primary_keys) == 1
        return col.unique

    def _check_unique_fk_capacity(self, table: Table, n: int) -> None:
        """A UNIQUE NOT NULL FK is one-to-one, so the parent needs >= n rows."""
        for col in table.foreign_keys:
            fk = col.foreign_key
            if (
                self._is_individually_unique(table, col)
                and not col.nullable
                and fk.ref_table != table.name
            ):
                available = len(self._rows_by_table.get(fk.ref_table, []))
                if available < n:
                    raise ValueError(
                        f"{table.name}.{col.name} is a UNIQUE NOT NULL foreign key, "
                        f"but parent {fk.ref_table} has only {available} rows for "
                        f"{n} requested (raise {fk.ref_table}'s row count)"
                    )

    def _generate_base_row(
        self,
        table: Table,
        row_index: int,
        single_int_pk: Column | None,
        batched: dict[str, list[Any]],
        prealloc: dict[str, list[Any]],
    ) -> Row:
        row: Row = {}
        for col in table.columns:
            if col.foreign_key is not None:
                continue
            if col is single_int_pk:
                row[col.name] = row_index + 1
            elif col.name in prealloc:
                row[col.name] = prealloc[col.name][row_index]
            elif col.name in batched:
                row[col.name] = batched[col.name][row_index]
            else:
                row[col.name] = self.factory.one(col, row_index)
        return row

    def _fill_foreign_keys(
        self,
        table: Table,
        row: Row,
        rows_so_far: list[Row],
    ) -> None:
        for group in table.foreign_key_groups:
            self._fill_foreign_key_group(table, row, rows_so_far, group)

    def _fill_foreign_key_group(
        self,
        table: Table,
        row: Row,
        rows_so_far: list[Row],
        group: list[Column],
    ) -> None:
        fk = group[0].foreign_key
        assert fk is not None
        if (table.name, tuple(col.name for col in group)) in self._deferred_fk_keys:
            self._set_fk_group_null(row, group)
            return

        can_be_null = any(c.nullable for c in group)

        if fk.ref_table == table.name:
            donor_rows = rows_so_far
            null_rate = 0.3
        else:
            donor_rows = self._rows_by_table.get(fk.ref_table, [])
            null_rate = 0.05

        if not donor_rows:
            if can_be_null:
                self._set_fk_group_null(row, group)
                return
            if fk.ref_table == table.name:
                self._fill_self_reference_from_current_row(table, row, group)
                return
            raise ValueError(
                f"{table.name}.{self._group_name(group)} requires a parent in "
                f"{fk.ref_table}, but no parent rows were generated"
            )

        if can_be_null and self.factory.rng.random() < null_rate:
            self._set_fk_group_null(row, group)
            return

        donor = self.factory.rng.choice(donor_rows)
        for col in group:
            col_fk = col.foreign_key
            assert col_fk is not None
            try:
                row[col.name] = donor[col_fk.ref_column]
            except KeyError as exc:
                raise KeyError(
                    f"{table.name}.{col.name} references missing parent column "
                    f"{col_fk.ref_table}.{col_fk.ref_column}"
                ) from exc

    def _fill_self_reference_from_current_row(
        self,
        table: Table,
        row: Row,
        group: list[Column],
    ) -> None:
        for col in group:
            fk = col.foreign_key
            assert fk is not None
            try:
                row[col.name] = row[fk.ref_column]
            except KeyError as exc:
                raise KeyError(
                    f"{table.name}.{col.name} cannot self-reference missing "
                    f"column {fk.ref_column!r}"
                ) from exc

    @staticmethod
    def _set_fk_group_null(row: Row, group: list[Column]) -> None:
        for col in group:
            row[col.name] = None

    def _build_deferred_updates(
        self,
        deferred_fks: list[DeferredForeignKey],
    ) -> list[DeferredUpdate]:
        updates: list[DeferredUpdate] = []
        for deferred_fk in deferred_fks:
            child = self.schema[deferred_fk.table]
            parent_rows = self._rows_by_table.get(deferred_fk.ref_table, [])
            child_rows = self._rows_by_table.get(deferred_fk.table, [])
            if not child_rows or not parent_rows:
                continue
            if not child.primary_keys:
                raise ValueError(
                    f"{child.name}.{', '.join(deferred_fk.columns)} is deferred, "
                    "but second-pass updates require a primary key on the child table"
                )

            assignments = self._choose_deferred_assignments(
                child,
                deferred_fk,
                child_rows,
                parent_rows,
            )
            for row, assigned in assignments:
                if not assigned:
                    continue
                updates.append(
                    DeferredUpdate(
                        table=child.name,
                        key_columns=tuple(col.name for col in child.primary_keys),
                        key_values=tuple(row[col.name] for col in child.primary_keys),
                        assignments=tuple(assigned),
                    )
                )
        return updates

    def _choose_deferred_assignments(
        self,
        child: Table,
        deferred_fk: DeferredForeignKey,
        child_rows: list[Row],
        parent_rows: list[Row],
    ) -> list[tuple[Row, list[tuple[str, Any]]]]:
        columns = [child.column(name) for name in deferred_fk.columns]
        single_unique = len(columns) == 1 and self._is_individually_unique(child, columns[0])
        if single_unique:
            donors = parent_rows[:]
            self.factory.rng.shuffle(donors)
            paired = zip(child_rows, donors)
        else:
            paired = ((row, self.factory.rng.choice(parent_rows)) for row in child_rows)

        out: list[tuple[Row, list[tuple[str, Any]]]] = []
        for row, donor in paired:
            assigned: list[tuple[str, Any]] = []
            for child_col, parent_col in zip(deferred_fk.columns, deferred_fk.ref_columns):
                assigned.append((child_col, donor[parent_col]))
            out.append((row, assigned))
        return out

    @staticmethod
    def _group_name(group: list[Column]) -> str:
        return ", ".join(c.name for c in group)

    @staticmethod
    def _pk_tuple(table: Table, row: Row) -> tuple[Any, ...] | None:
        pks = table.primary_keys
        if not pks:
            return None
        return tuple(row[c.name] for c in pks)

    @staticmethod
    def _unique_values(
        table: Table,
        row: Row,
        seen_unique: dict[str, set[Any]],
    ) -> list[tuple[str, Any]] | None:
        values: list[tuple[str, Any]] = []
        for col in table.columns:
            if col.name not in seen_unique:
                continue
            value = row[col.name]
            if value is None:
                continue
            if value in seen_unique[col.name]:
                return None
            values.append((col.name, value))
        return values

    @staticmethod
    def _pk_value(table: Table, row: Row) -> Any:
        pks = table.primary_keys
        if len(pks) == 1:
            return row[pks[0].name]
        # composite key: return a tuple (sufficient for FK pools that target it)
        return tuple(row[c.name] for c in pks)
