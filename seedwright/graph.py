"""Order tables so every parent is generated before its children.

A child row's foreign key can only point at a parent row that already exists.
So before generating anything we sort the tables into dependency order using
Kahn's algorithm.

Two wrinkles real schemas throw at you:

* Self-references (``employees.manager_id -> employees.id``). A table that
  depends on itself isn't a real ordering problem — you just generate its rows
  and let later rows point at earlier ones. We strip self-edges before sorting.

* True cycles across tables (A -> B -> A). These cannot be satisfied by row
  order alone; one side must allow NULL and be filled on a second pass.
  ``topological_order`` remains strict and reports the cycle. ``dependency_plan``
  chooses nullable FK groups to defer, returning INSERT order plus UPDATE work.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import DeferredForeignKey, Schema, Table


class CyclicDependencyError(Exception):
    def __init__(self, remaining: list[str]) -> None:
        self.remaining = remaining
        super().__init__(
            "foreign-key cycle among tables: "
            + ", ".join(sorted(remaining))
            + " — break it by making one FK nullable and generating in two passes"
        )


@dataclass(frozen=True)
class DependencyPlan:
    order: list[str]
    deferred_fks: list[DeferredForeignKey]


def build_dependency_edges(schema: Schema) -> dict[str, set[str]]:
    """Map each table to the set of parent tables it depends on (self-edges removed)."""
    return _build_dependency_edges(schema, set())


def _build_dependency_edges(
    schema: Schema,
    deferred_keys: set[tuple[str, tuple[str, ...]]],
) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {t.name: set() for t in schema}
    for table in schema:
        for group in table.foreign_key_groups:
            fk = group[0].foreign_key
            assert fk is not None
            parent = fk.ref_table
            if parent == table.name:
                continue  # self-reference: not an ordering constraint
            if parent not in edges:
                raise KeyError(
                    f"{table.name}.{_group_name(group)} references unknown table {parent!r}"
                )
            if _group_key(table, group) in deferred_keys:
                continue
            edges[table.name].add(parent)
    return edges


def topological_order(schema: Schema) -> list[str]:
    """Return table names so that every table appears after all its parents."""
    deps = build_dependency_edges(schema)
    return _topological_order_from_edges(deps)


def dependency_plan(schema: Schema) -> DependencyPlan:
    """Return load order, deferring nullable FK groups until cycles are broken."""
    deferred_keys: set[tuple[str, tuple[str, ...]]] = set()
    deferred_fks: list[DeferredForeignKey] = []

    while True:
        deps = _build_dependency_edges(schema, deferred_keys)
        try:
            return DependencyPlan(
                order=_topological_order_from_edges(deps),
                deferred_fks=deferred_fks,
            )
        except CyclicDependencyError as exc:
            candidate = _choose_deferred_fk(schema, set(exc.remaining), deferred_keys)
            if candidate is None:
                raise
            table, group = candidate
            deferred_keys.add(_group_key(table, group))
            deferred_fks.append(_deferred_fk(table, group))


def _topological_order_from_edges(deps: dict[str, set[str]]) -> list[str]:
    indegree = {name: len(parents) for name, parents in deps.items()}

    # Children-of map: parent -> tables that depend on it.
    children: dict[str, set[str]] = {name: set() for name in deps}
    for name, parents in deps.items():
        for parent in parents:
            children[parent].add(name)

    # Sorted() keeps the output stable/deterministic across runs.
    ready = sorted(name for name, d in indegree.items() if d == 0)
    order: list[str] = []

    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in sorted(children[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(deps):
        unresolved = [n for n in deps if n not in order]
        raise CyclicDependencyError(unresolved)
    return order


def _choose_deferred_fk(
    schema: Schema,
    cycle_tables: set[str],
    deferred_keys: set[tuple[str, tuple[str, ...]]],
):
    candidates = []
    for table in schema:
        if table.name not in cycle_tables:
            continue
        for group in table.foreign_key_groups:
            fk = group[0].foreign_key
            assert fk is not None
            if fk.ref_table not in cycle_tables or fk.ref_table == table.name:
                continue
            if _group_key(table, group) in deferred_keys:
                continue
            if not all(col.nullable for col in group):
                continue
            candidates.append((table.name, fk.ref_table, _group_name(group), table, group))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3], candidates[0][4]


def _deferred_fk(table: Table, group) -> DeferredForeignKey:
    fk = group[0].foreign_key
    assert fk is not None
    return DeferredForeignKey(
        table=table.name,
        columns=tuple(col.name for col in group),
        ref_table=fk.ref_table,
        ref_columns=tuple(col.foreign_key.ref_column for col in group if col.foreign_key),
        constraint_name=fk.constraint_name,
    )


def _group_key(table: Table, group) -> tuple[str, tuple[str, ...]]:
    return table.name, tuple(col.name for col in group)


def _group_name(group) -> str:
    return ", ".join(col.name for col in group)
