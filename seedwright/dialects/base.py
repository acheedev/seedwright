"""The one interface you implement to support a new database.

A dialect owns all database-specific behavior:

* `introspect()` — read a live schema and return our `Schema` model.
* quoting methods - render identifiers and Python values for that engine.
* optional validation/apply hooks - execute generated SQL safely for that engine.

That is the whole contract. The generator, dependency sort, row builder, CLI
workflow, and emitters stay dialect-free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..model import Column, Schema


@dataclass(frozen=True)
class ValidationResult:
    tables: int
    rows: int


class Dialect(ABC):
    name: str = "abstract"

    @abstractmethod
    def introspect(self) -> Schema:
        """Read the connected database and return the internal schema model."""

    @abstractmethod
    def quote_identifier(self, identifier: str) -> str:
        """Quote a table or column name for this engine."""

    @abstractmethod
    def quote_literal(self, value: Any) -> str:
        """Render a Python value as a SQL literal for this engine."""

    def quote_column_literal(self, value: Any, column: Column) -> str:
        """Render a value with access to the originating column metadata."""
        return self.quote_literal(value)

    def validate_script(
        self,
        validation_target: str,
        table_names: list[str],
        sql: str,
    ) -> ValidationResult:
        """Run generated SQL against a user-provided validation target.

        Dialects that support guarded apply should execute the script in a
        transaction and leave the validation target unchanged.
        """
        raise NotImplementedError(
            f"{self.name} dialect does not support validation/apply yet"
        )

    def apply_script(self, sql: str) -> None:
        """Apply generated SQL to the dialect's configured target."""
        raise NotImplementedError(
            f"{self.name} dialect does not support validation/apply yet"
        )
