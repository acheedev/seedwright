"""Order tables so every parent is generated before its children.

A child row's foreign key can only point at a parent row that already exists.
So before generating anything we sort the tables into dependency order using
Kahn's algorithm.

Two wrinkles real schemas throw at you:

* Self-references (``employees.manager_id -> employees.id``). A table that
  depends on itself isn't a real ordering problem — you just generate its rows
  and let later rows point at earlier ones. We strip self-edges before sorting.

* True cycles across tables (A -> B -> A). These cannot be satisfied by row
  order alone; one side must allow NULL and be filled on a second pass. The MVP
  does not silently guess — it detects the cycle and reports it, naming the
  tables involved, so you fix the schema or extend the engine deliberately.
"""

from __future__ import annotations

from .model import Schema


class CyclicDependencyError(Exception):
    def __init__(self, remaining: list[str]) -> None:
        self.remaining = remaining
        super().__init__(
            "foreign-key cycle among tables: "
            + ", ".join(sorted(remaining))
            + " — break it by making one FK nullable and generating in two passes"
        )


def build_dependency_edges(schema: Schema) -> dict[str, set[str]]:
    """Map each table to the set of parent tables it depends on (self-edges removed)."""
    edges: dict[str, set[str]] = {t.name: set() for t in schema}
    for table in schema:
        for col in table.foreign_keys:
            parent = col.foreign_key.ref_table
            if parent == table.name:
                continue  # self-reference: not an ordering constraint
            if parent not in edges:
                raise KeyError(
                    f"{table.name}.{col.name} references unknown table {parent!r}"
                )
            edges[table.name].add(parent)
    return edges


def topological_order(schema: Schema) -> list[str]:
    """Return table names so that every table appears after all its parents."""
    deps = build_dependency_edges(schema)
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
